"""Hybrid retrieval over the athlete's beliefs and discoveries.

Signals (every one is computed per candidate and reported in `Hit.why`):
  bm25     SQLite FTS5 BM25 (porter stemming) over the original query and a lexicon-expanded copy (Hinglish / abbreviation bridge); for `as_of`
           queries, where the FTS index (which only holds CURRENT beliefs) cannot answer, an in-memory BM25 over the valid-then candidates.
  dense    cosine between the query vector and the item vector (embed.py: Gemini when reachable, the deterministic hash model otherwise; vectors
           of different models are never compared), gated by a per-model noise floor.
  graph    personalised PageRank on the entity graph (mem_entity / mem_edge) seeded by the entities named in the query (or passed in), so
           "knee friendly alternative to squats" reaches the leg-press belief two hops away.
  recency  exponential prior relative to now (or to `as_of`), faster for episodes than for stable facts.
  conf     effective confidence (store.confidence formula: Beta mean relaxed toward 0.5 by neglect) and salience; pinned beliefs get a small lift.
Relevance signals are fused (weighted score fusion or reciprocal-rank fusion, see WEIGHTS), the priors multiply the result, and MMR then
diversifies the top of the list. Superseded / retracted / denied beliefs are never candidates, except that `as_of` in the past answers from what
was valid then (denied beliefs stay excluded: the athlete said they were wrong).
"""
from __future__ import annotations

import functools
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from .. import clock
from . import embed, store
from .types import Hit, predicate as _pred

SENSITIVITY_RANK = {"normal": 0, "body": 1, "mental": 2, "health": 2}

# Fusion weights, tuned on the labelled benchmark (bench_retrieval.py --tune: 2/3 of the queries to tune, 1/3 held out). The offline hash model is a weaker
# semantic signal than a real embedding model, so a real model gets a larger dense weight (MODEL_WEIGHTS overlays these when it is the active model).
WEIGHTS: dict[str, Any] = {
    "bm25": 0.7, "dense": 0.7, "graph": 0.6,        # relevance signal weights
    "recency": 0.25, "conf": 0.20, "salience": 0.06, "pinned": 0.05,   # multiplicative prior strengths
    "mmr": 1.0,                                     # 1.0 = no diversification
    "fusion": "rrf",                                # "linear" (calibrated scores) or "rrf" (reciprocal-rank fusion)
    "rrf_k": 8.0,
    "expand": 0.85,                                 # weight of the lexicon-expanded BM25 query relative to the original
    "ppr_alpha": 0.35,
}
MODEL_WEIGHTS: dict[str, dict[str, Any]] = {"real": {"dense": 1.6, "graph": 1.0, "mmr": 0.7, "rrf_k": 4.0}}
_FACT_COLS = ("id, subject_id, predicate, object_entity, kind, body, source, sensitivity, alpha, beta, salience, pinned, status, user_state, "
              "valid_from, valid_to, last_reinforced, polarity, hits")


# ====================================================== tokens (cached) ======================================================
@functools.lru_cache(maxsize=30000)
def _toks(text: str) -> tuple[tuple[str, ...], frozenset, frozenset]:
    words = [embed.stem(w) for w in re.findall(r"[\wऀ-ॿ]+", text.lower())]
    return tuple(words), frozenset(words), frozenset(f"{a} {b}" for a, b in zip(words, words[1:]))


def _content(text: str) -> frozenset:
    return frozenset(embed.stem(t) for t in embed.tokenize(text))


# ====================================================== candidates ======================================================
class _C:
    """A retrievable item with everything the ranker needs (plain attributes: fast to build for thousands of rows)."""
    __slots__ = ("type", "id", "text", "kind", "ts", "sens", "conf", "sal", "pinned", "age", "tau", "subject", "obj_ent", "meta", "key",
                 "bm", "cos", "dense", "graph", "gnb", "rel", "score", "why", "vec")

    def __init__(self, type: str, id: int, text: str, kind: str, ts: str | None, sens: str, conf: float, sal: float, pinned: bool, age: float, tau: float,
                 subject: int | None, obj_ent: int | None, meta: dict) -> None:
        self.type, self.id, self.text, self.kind, self.ts, self.sens, self.conf, self.sal, self.pinned = type, id, text, kind, ts, sens, conf, sal, pinned
        self.age, self.tau, self.subject, self.obj_ent, self.meta = age, tau, subject, obj_ent, meta
        self.key = (type, id)
        self.bm = self.cos = self.dense = self.graph = self.gnb = self.rel = self.score = 0.0
        self.why = {}
        self.vec = None


def _ref_time(as_of: str | None) -> datetime:
    return clock.parse(as_of) if as_of else clock.now()


@functools.lru_cache(maxsize=40000)
def _parse(ts: str) -> datetime:
    return clock.parse(ts)


def _days(ref: datetime, ts: str | None) -> float:
    if not ts:
        return 365.0
    try:
        return max(0.0, (ref - _parse(ts)).total_seconds() / 86400.0)
    except ValueError:
        return 365.0


def _conf(r: Any, ref: datetime) -> float:
    """store.confidence, inlined so one search does not call clock.now() thousands of times (identical formula)."""
    base = r["alpha"] / (r["alpha"] + r["beta"])
    hl = _pred(r["predicate"])["half_life"]
    if r["pinned"] or not hl:
        return base
    return 0.5 + (base - 0.5) * 0.5 ** (_days(ref, r["last_reinforced"]) / hl)


def _load(as_of: str | None, item_types: tuple[str, ...], kinds, min_conf: float, max_sens: str | None) -> list[_C]:
    ref = _ref_time(as_of)
    maxrank = SENSITIVITY_RANK.get(max_sens, 99) if max_sens else 99
    out: list[_C] = []
    _tau_cache: dict = {}
    with store._c() as c:
        if "fact" in item_types:
            q, a = f"SELECT {_FACT_COLS} FROM mem_fact WHERE user_state != 'denied'", []
            if as_of:
                q += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) AND status NOT IN ('retracted','disputed','archived')"
                a += [as_of, as_of]
            else:
                q += " AND status = 'active'"
            if kinds:
                q += f" AND kind IN ({','.join('?' * len(kinds))})"
                a += list(kinds)
            for r in c.execute(q, a):
                if SENSITIVITY_RANK.get(r["sensitivity"], 0) > maxrank:
                    continue
                cf = _conf(r, ref)
                if cf < min_conf:
                    continue
                tk = (r["predicate"], r["kind"] == "episodic")
                tau = _tau_cache.get(tk)
                if tau is None:
                    spec = _pred(r["predicate"])
                    tau = _tau_cache[tk] = 21.0 if tk[1] else 90.0 if spec["half_life"] is None or spec["half_life"] < 150 else 150.0
                out.append(_C("fact", r["id"], r["body"], r["kind"], r["valid_from"], r["sensitivity"], cf, r["salience"], bool(r["pinned"]),
                              _days(ref, r["last_reinforced"] if not as_of else r["valid_from"]), tau, r["subject_id"], r["object_entity"],
                              {"predicate": r["predicate"], "source": r["source"], "user_state": r["user_state"], "pinned": bool(r["pinned"]), "polarity": r["polarity"]}))
        if "discovery" in item_types and maxrank >= 0:
            q = "SELECT * FROM mem_discovery WHERE status IN ('candidate','confirmed','weakening') AND user_state != 'denied'"
            a = []
            if as_of:
                q += " AND first_seen <= ?"
                a.append(as_of)
            import json
            for r in c.execute(q, a):
                try:
                    eff = json.loads(r["effect"]) if r["effect"] else {}
                except ValueError:
                    eff = {}
                eff = eff if isinstance(eff, dict) else {}
                cf = eff.get("confidence")
                cf = float(cf) if isinstance(cf, (int, float)) else (0.85 if r["status"] == "confirmed" else 0.6 if r["status"] == "weakening" else 0.5)
                if cf < min_conf:
                    continue
                out.append(_C("discovery", r["id"], f"{r['title']}. {r['statement']}", "procedural", r["last_tested"], "normal", cf, 0.7 if r["status"] == "confirmed" else 0.5,
                              False, _days(ref, r["last_tested"]), 120.0, None, None,
                              {"family": r["family"], "status": r["status"], "n": eff.get("n"), "recommendation": r["recommendation"]}))
    return out


# ====================================================== signals ======================================================
def _stopless(q: str) -> str:
    toks = embed.tokenize(q)
    return " ".join(toks) if toks else q


def _bm25_fts(q: str, w: dict, item_types: tuple[str, ...], cands: dict[tuple[str, int], _C]) -> None:
    orig = store.fts_search(_stopless(q), limit=120, item_types=item_types)
    exp_terms = embed.expand_query_terms(q)
    exp = store.fts_search(" ".join([_stopless(q), *exp_terms]), limit=120, item_types=item_types) if exp_terms else []
    for lst, mult in ((orig, 1.0), (exp, float(w["expand"]))):
        if not lst:
            continue
        top = max(s for _, _, s in lst) or 1.0
        for t, i, s in lst:
            c = cands.get((t, i))
            if c is not None:
                c.bm = max(c.bm, mult * s / top)


def _bm25_memory(q: str, w: dict, cands: list[_C]) -> None:
    """In-memory BM25 (k1 1.2, b 0.75) for as_of queries; query terms are stems, expansion terms count at `expand` weight."""
    base = [embed.stem(t) for t in embed.tokenize(q)]
    exp = [embed.stem(t) for t in embed.expand_query_terms(q) if embed.stem(t) not in base]
    if not base or not cands:
        return
    docs = {c.key: _toks(c.text)[0] for c in cands}
    n = len(docs)
    avg = sum(len(d) for d in docs.values()) / n or 1.0
    df: dict[str, int] = {}
    for d in docs.values():
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    scores = {}
    for c in cands:
        d = docs[c.key]
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for terms, mult in ((base, 1.0), (exp, float(w["expand"]) * 0.7)):
            for t in terms:
                f = tf.get(t, 0)
                if f:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    s += mult * idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(d) / avg))
        scores[c.key] = s
    top = max(scores.values()) or 1.0
    for c in cands:
        c.bm = scores[c.key] / top if scores[c.key] > 0 else 0.0


_qcache: dict[tuple[str, str], np.ndarray] = {}


def _dense(q: str, cands: list[_C]) -> tuple[str, float]:
    """Fills c.cos for every candidate that has (or can cheaply get) a vector under ONE model. -> (model, noise floor)."""
    model = embed.model_name()
    items = [(c.type, c.id, c.text) for c in cands]
    cap = 48 if model != embed.HASH_MODEL else None          # a foreground search embeds only a few missing vectors (the nightly reindex does the rest)
    vecs, model = embed.ensure_vectors(items, model, max_new=cap)
    if model != embed.HASH_MODEL and len(vecs) < 0.9 * len(items):
        vecs, model = embed.ensure_vectors(items, embed.HASH_MODEL)    # too few real vectors yet: use the offline model for everything, never a mixture
    qv_arr, used = embed.embed_texts_ex([q], "query")
    if used != model:
        qv_arr, used = embed.hash_embed([q]), embed.HASH_MODEL
        if model != embed.HASH_MODEL:        # the document side is on the real model but the query fell back: use the fallback for both
            vecs, model = embed.ensure_vectors(items, embed.HASH_MODEL)
    qv = qv_arr[0]
    for c in cands:
        v = vecs.get((c.type, c.id))
        if v is not None and v.shape == qv.shape:
            c.vec = v
            c.cos = float(v @ qv)
    return model, embed.dense_floor(model)


def _entity_tables() -> tuple[list[dict], dict[int, list[tuple[str, ...]]]]:
    ents, names = [], {}
    with store._c() as c:
        import json
        for r in c.execute("SELECT id, kind, name, canonical, aliases FROM mem_entity WHERE kind != 'athlete'"):
            al = json.loads(r["aliases"] or "[]")
            ents.append({"id": r["id"], "name": r["name"]})
            names[r["id"]] = [tuple(_toks(n)[0]) for n in {r["canonical"], *al} if n]
    return ents, names


_SUBST = re.compile(r"\b(?:replac\w*|alternativ\w*|instead|swap\w*|substitut\w*|friendly|safe|safer|options?|work ?around|badle|vikalp)\b", re.I)
_SUBST_RELS = {"alternative_to", "substitute_for", "replaces", "safer_than"}


def _graph(q: str, w: dict, seeds_in: list[int] | None, cands: list[_C]) -> tuple[int, bool]:
    """Personalised PageRank on the entity graph from the query's entities; sets c.graph. -> (number of seeds found, substitution intent).
    With substitution intent ("what can replace squats because of my knee") the walk favours alternative_to edges and the seed entities' own
    beliefs get little graph credit: the answer is the neighbour, not the thing the athlete already named."""
    ents, names = _entity_tables()
    subst = bool(_SUBST.search(q))
    if not ents:
        return 0, subst
    qwords = list(_toks(q)[0])
    qexp = qwords + [embed.stem(t) for t in embed.expand_query_terms(q)]
    qset, qpairs = set(qexp), {f"{a} {b}" for a, b in zip(qwords, qwords[1:])}
    seeds: dict[int, float] = {int(e): 1.0 for e in (seeds_in or []) if int(e) in names}
    for eid, forms in names.items():
        for f in forms:
            if (len(f) == 1 and f[0] in qset) or (len(f) == 2 and " ".join(f) in qpairs) or (len(f) > 2 and all(x in qset for x in f)):
                seeds[eid] = 1.0
                break
    if not seeds:
        return 0, subst
    adj: dict[int, dict[int, float]] = {}
    with store._c() as c:
        for r in c.execute("SELECT src, dst, weight, rel FROM mem_edge WHERE valid_to IS NULL"):
            wt = float(r["weight"]) * (4.0 if subst and r["rel"] in _SUBST_RELS else 1.0)
            adj.setdefault(r["src"], {})[r["dst"]] = adj.get(r["src"], {}).get(r["dst"], 0.0) + wt
            adj.setdefault(r["dst"], {})[r["src"]] = adj.get(r["dst"], {}).get(r["src"], 0.0) + wt
    tot = sum(seeds.values())
    p0 = {k: v / tot for k, v in seeds.items()}
    p, a = dict(p0), float(w["ppr_alpha"])
    for _ in range(14):
        nxt = {k: a * v for k, v in p0.items()}
        for u, pu in p.items():
            nb = adj.get(u)
            if not nb:
                nxt[u] = nxt.get(u, 0.0) + (1 - a) * pu
                continue
            z = sum(nb.values())
            for v, wt in nb.items():
                nxt[v] = nxt.get(v, 0.0) + (1 - a) * pu * wt / z
        p = nxt
    top = max(p.values()) or 1.0
    act = {e: v / top * (0.3 if (subst and e in seeds) else 1.0) for e, v in p.items() if v / top >= 0.04 and e in names}
    for c in cands:
        best = nb = 0.0
        if c.subject in act:
            best = act[c.subject]
        if c.obj_ent in act:
            best = max(best, act[c.obj_ent])
        if subst and ((c.subject in act and c.subject not in seeds) or (c.obj_ent in act and c.obj_ent not in seeds)):
            nb = max(act.get(c.subject, 0.0) if c.subject not in seeds else 0.0, act.get(c.obj_ent, 0.0) if c.obj_ent not in seeds else 0.0)
        stems, bigr = _toks(c.text)[1], _toks(c.text)[2]
        for e, av in act.items():
            if av <= best and not (subst and e not in seeds and av > nb):
                continue
            for f in names[e]:
                if (len(f) == 1 and f[0] in stems) or (len(f) == 2 and " ".join(f) in bigr) or (len(f) > 2 and all(x in stems for x in f)):
                    best = max(best, av)
                    if subst and e not in seeds:
                        nb = max(nb, av)
                    break
        c.graph, c.gnb = best, nb
    return len(seeds), subst


# ====================================================== fusion + diversification ======================================================
def _fuse(cands: list[_C], w: dict, floor: float, model: str) -> list[_C]:
    hi = embed.dense_high(model)
    for c in cands:
        c.dense = min(1.0, max(0.0, (c.cos - floor) / max(1e-6, hi - floor))) if c.cos else 0.0
    wb, wd, wg = float(w["bm25"]), float(w["dense"]), float(w["graph"])
    if w["fusion"] == "rrf":
        K = float(w["rrf_k"])
        ranks: dict[str, dict] = {}
        for name in ("bm", "dense", "graph"):
            ordered = sorted((c for c in cands if getattr(c, name) > 0), key=lambda c: -getattr(c, name))
            rk: dict = {}
            for r, c in enumerate(ordered):             # equal scores share a rank (a tie is not a preference)
                rk[c.key] = rk[ordered[r - 1].key] if r and abs(getattr(c, name) - getattr(ordered[r - 1], name)) < 1e-9 else r
            ranks[name] = rk
        denom = (wb + wd + wg) / (K + 1.0) or 1.0
        for c in cands:
            s = sum(wt / (K + ranks[n][c.key] + 1.0) for n, wt in (("bm", wb), ("dense", wd), ("graph", wg)) if c.key in ranks[n])
            c.rel = s / denom + 0.5 * c.gnb
    else:
        for c in cands:
            # a noisy-OR style blend: a strong single signal is enough, agreement between signals lifts the item further
            c.rel = (wb * c.bm + wd * c.dense + wg * c.graph) / (wb + wd + wg) + 0.25 * min(c.bm, c.dense) * (wb + wd) / (wb + wd + wg) + 0.5 * c.gnb
    for c in cands:
        rec = 0.5 ** (c.age / c.tau)
        mult = 1.0 + w["conf"] * (c.conf - 0.5) + w["recency"] * (rec - 0.5) + w["salience"] * ((c.sal or 0.5) - 0.5) + (w["pinned"] if c.pinned else 0.0)
        c.score = c.rel * max(0.2, mult)
        c.why = rec                                   # expanded into the per-signal explanation only for the hits that are returned (_hit)
    return cands


def _mmr(pool: list[_C], k: int, lam: float, floor: float) -> list[_C]:
    pool = sorted(pool, key=lambda c: -c.score)[: max(k * 4, 24)]
    if lam >= 0.999 or len(pool) <= 1:
        return pool[:k]
    top = pool[0].score or 1.0
    sel: list[_C] = []
    rest = pool[:]
    toks = {c.key: _content(c.text) for c in pool}

    def sim(a: _C, b: _C) -> float:
        if a.vec is not None and b.vec is not None and a.vec.shape == b.vec.shape:
            return max(0.0, (float(a.vec @ b.vec) - floor) / (1.0 - floor))
        ta, tb = toks[a.key], toks[b.key]
        return len(ta & tb) / len(ta | tb) if ta and tb else 0.0

    while rest and len(sel) < k:
        best, bs = None, -1e9
        for c in rest:
            red = max((sim(c, s) for s in sel), default=0.0)
            m = lam * (c.score / top) - (1 - lam) * red
            if m > bs:
                best, bs = c, m
        sel.append(best)
        rest.remove(best)
    return sel


# ====================================================== public API ======================================================
def _hit(c: _C, rank_score: float) -> Hit:
    if not isinstance(c.why, dict):
        c.why = {"bm25": round(c.bm, 4), "dense": round(c.dense, 4), "cosine": round(c.cos, 4), "graph": round(c.graph, 4), "recency": round(float(c.why), 4),
                 "confidence": round(c.conf, 4), "relevance": round(c.rel, 4)}
    return Hit(c.type, c.id, c.text, round(rank_score, 6), round(c.conf, 4) if c.conf is not None else None, c.kind, c.ts, c.sens, dict(c.why), dict(c.meta or {}))


def search(query: str, k: int = 8, *, kinds: tuple[str, ...] | None = None, item_types: tuple[str, ...] | None = None, as_of: str | None = None,
           min_confidence: float = 0.0, max_sensitivity: str | None = None, entities: list[int] | None = None, weights: dict | None = None) -> list[Hit]:
    """Top-k memory items for `query`, best first (hybrid: BM25 + dense + graph + temporal/confidence priors, fused, MMR-diversified).

    kinds            restrict facts to these kinds (semantic, episodic, procedural, preference, constraint, trait, goal)
    item_types       restrict to 'fact' / 'discovery' (default both)
    as_of            ISO time: answer from what was valid THEN (superseded facts included, later ones excluded)
    min_confidence   drop beliefs whose effective confidence is below this
    max_sensitivity  'normal' excludes health/body/mental facts (for surfaces that must not show them); None = no limit
    entities         entity ids the query is about (graph seeds)
    weights          optional overrides of retrieve.WEIGHTS (used by the benchmark for ablations); None = the tuned defaults
    Every Hit carries `why`, the per-signal scores behind its rank. Never raises: on an internal error it degrades to plain BM25.
    """
    try:
        return _search(query, int(k), kinds, tuple(item_types) if item_types else None, as_of, float(min_confidence or 0.0), max_sensitivity, entities, weights)
    except Exception:
        try:
            return _fallback(query, int(k), kinds, item_types, as_of, min_confidence, max_sensitivity)
        except Exception:
            return []


def _search(query: str, k: int, kinds, item_types, as_of, min_conf, max_sens, entities, user_w: dict | None) -> list[Hit]:
    q = (query or "").strip()
    if (not q and not entities) or k <= 0:
        return []
    user_w = user_w or {}
    w = {**WEIGHTS, **user_w}
    types = item_types or ("fact", "discovery")
    cands = _load(as_of, types, kinds, min_conf, max_sens)
    if not cands:
        return []
    by_key = {c.key: c for c in cands}
    floor, model = 0.2, embed.HASH_MODEL
    if q:
        if w["bm25"] > 0:
            if as_of:
                _bm25_memory(q, w, cands)
            else:
                _bm25_fts(q, w, types, by_key)
        if w["dense"] > 0:
            model, floor = _dense(q, cands)
            if model != embed.HASH_MODEL:
                w = {**WEIGHTS, **MODEL_WEIGHTS["real"], **user_w}
    if w["graph"] > 0:
        _, subst = _graph(q, w, entities, cands)
        if subst:
            w = {**w, "graph": float(w["graph"]) * 2.5}
    _fuse(cands, w, floor, model)
    pool = [c for c in cands if c.bm > 0 or c.dense > 0 or c.graph > 0]
    if w["bm25"] <= 0 and w["dense"] > 0 and w["graph"] <= 0:       # pure dense ablation: rank by cosine even below the floor
        pool = sorted(cands, key=lambda c: -c.cos)[: max(k * 4, 24)]
        for c in pool:
            c.score = c.cos
    if not pool:
        return []
    chosen = _mmr(pool, k, float(w["mmr"]), floor)
    return [_hit(c, c.score) for c in chosen]


def _fallback(query, k, kinds, item_types, as_of, min_conf, max_sens) -> list[Hit]:
    """Last resort: plain BM25 + confidence (the original baseline behaviour)."""
    raw = store.fts_search(query, limit=k * 5, item_types=item_types or ("fact", "discovery"))
    if not raw:
        return []
    cands = {c.key: c for c in _load(as_of, tuple(item_types or ("fact", "discovery")), kinds, float(min_conf or 0.0), max_sens)}
    top = max(s for _, _, s in raw) or 1.0
    out = []
    for t, i, s in raw:
        c = cands.get((t, i))
        if c is not None:
            c.bm = s / top
            c.why = {"bm25": round(c.bm, 4), "confidence": round(c.conf, 4)}
            out.append(_hit(c, 0.8 * c.bm + 0.2 * c.conf))
    out.sort(key=lambda h: -h.score)
    return out[:k]


# ====================================================== time phrases ======================================================
_MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}
_MONTHS.update({m[:3]: v for m, v in list(_MONTHS.items())})
_UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365, "years": 365}
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def parse_as_of(text: str, now: datetime | None = None) -> str | None:
    """Turn a time phrase in `text` into an `as_of` ISO timestamp, or None. Understands: "in January [2026]", "last month", "last week",
    "yesterday", "N days/weeks/months ago", "a year ago", "last year", "at the start of the year". Month names resolve to the middle of the
    month (so a belief valid for part of it is found); a month later than the current one means last year's."""
    now = now or clock.now()
    low = (text or "").lower()
    fmt = lambda d: d.astimezone(timezone.utc).isoformat(timespec="seconds")
    m = re.search(r"\b(\d+|a|an|one|two|three|four|five|six)\s+(day|days|week|weeks|month|months|year|years)\s+ago\b", low)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM[m.group(1)]
        return fmt(now - timedelta(days=n * _UNIT_DAYS[m.group(2)]))
    if re.search(r"\byesterday\b", low):
        return fmt(now - timedelta(days=1))
    if re.search(r"\blast week\b", low):
        return fmt(now - timedelta(days=7))
    if re.search(r"\blast month\b", low):
        return fmt(now - timedelta(days=30))
    if re.search(r"\blast year\b", low):
        return fmt(now - timedelta(days=365))
    if re.search(r"\b(start|beginning) of (the |this )?year\b", low):
        return fmt(now.replace(month=1, day=2, hour=12, minute=0, second=0, microsecond=0))
    m = re.search(r"\b(?:in|during|back in|since|as of|around)\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b(?:\s+(\d{4}))?", low)
    if m:
        mon = _MONTHS.get(m.group(1)) or _MONTHS.get(m.group(1)[:3])
        year = int(m.group(2)) if m.group(2) else (now.year if mon <= now.month else now.year - 1)
        return fmt(datetime(year, mon, 15, 12, tzinfo=timezone.utc))
    return None


def search_natural(query: str, k: int = 8, **kw: Any) -> list[Hit]:
    """`search` that also understands time phrases in the query ("what was my goal in January?" -> as_of January)."""
    if kw.get("as_of") is None:
        kw["as_of"] = parse_as_of(query)
    return search(query, k, **kw)
