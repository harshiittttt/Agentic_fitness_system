"""Consolidation: the idempotent "sleep" pass over the belief store.

What it does, in order (each step is isolated; a failing step is reported and the rest still run; the whole pass never raises):
  1. promote   repeated episodic facts (the same thing happening again and again) become one semantic fact (source "inferred", evidence = the episodes);
  2. merge     near-duplicate beliefs (same subject and predicate; lexical + concept + embedding similarity; same numbers and polarity) are merged:
               the stronger one survives, evidence is merged, the other is superseded. Nothing the athlete confirmed or corrected is ever merged away;
  3. resolve   `disputed` beliefs (a data-driven value that contradicts something the athlete confirmed) never override it: the confirmed belief stays,
               a stale weak dispute is archived, a persistent strong one becomes a question for the athlete; a dispute against an unconfirmed belief is
               decided by evidence;
  4. decay     unreinforced beliefs fade (archived below 0.30 effective confidence, or after four half-lives with no support); pinned and
               athlete-confirmed beliefs are exempt;
  5. link      entities and edges are refreshed from fact text (body parts, exercises, foods; co-mention edges), so graph retrieval can walk
               knee -> squat -> leg press;
  6. embed     vectors are refreshed for changed text (embed.reindex).
Idempotent: running it twice in a row changes nothing the second time. It never touches what the athlete confirmed except to reinforce it.
"""
from __future__ import annotations

import json
import re
import sys
import types
from collections import Counter

from .. import clock
from . import embed, retrieve, store
from .types import predicate, register_predicate

register_predicate("recurring", functional=False, half_life=120, sensitivity="normal", cls="behavior")

NEGATION = {"not", "no", "never", "without", "nahi", "dont", "don't", "cant", "can't", "stopped", "avoid", "hate", "dislike", "dislikes"}
_MONTH = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b\d+(?:\.\d+)?\b", re.I)
PROMOTE_MIN = 3                       # episodes needed
PROMOTE_DAYS = 3                      # on at least this many distinct days
_PROMOTED_PRED = {"happened": "recurring", "milestone": "recurring", "setback": "recurring", "skips_exercise": "dislikes_exercise"}

_BODY_PARTS = ("knee", "shoulder", "back", "hip", "ankle", "wrist", "elbow", "neck")
_EXERCISES = ("squat", "deadlift", "bench", "ohp", "row", "pullup", "lunge", "legpress", "burpee")
_FOODS = ("paneer", "milk", "curd", "rice", "roti", "dal", "egg")
_ENTITY_NAME = {"bench": "bench press", "ohp": "overhead press", "legpress": "leg press", "pullup": "pull-up"}
_KIND_OF = {**{c: "body_part" for c in _BODY_PARTS}, **{c: "exercise" for c in _EXERCISES}, **{c: "food" for c in _FOODS}}
_KIND_ORDER = {"body_part": 0, "exercise": 1, "food": 2}


# ====================================================== helpers ======================================================
def _rows() -> list[dict]:
    with store._c() as c:
        rs = [dict(r) for r in c.execute("SELECT * FROM mem_fact WHERE status IN ('active','disputed')")]
    ref = clock.now()
    for r in rs:
        r["conf"] = retrieve._conf(r, ref)
        r["evidence"] = json.loads(r["evidence"]) if r["evidence"] else []
        r["meta"] = json.loads(r["meta"]) if r["meta"] else {}
    return rs


def _sig(text: str) -> frozenset:
    toks = embed.tokenize(text)
    return frozenset([embed.stem(t) for t in toks] + ["§" + c for c in embed.concepts_of(toks)])


def _jac(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _numbers(r: dict) -> frozenset:
    return frozenset(re.findall(r"\d+(?:\.\d+)?", f"{r['object_text']} {r['body']}"))


def _neg(r: dict) -> bool:
    return bool(NEGATION & set(re.findall(r"[a-z']+", r["body"].lower())))


def _strength(r: dict) -> tuple:
    """Who survives a merge: the athlete's word, then pinned, then confidence, evidence count, the older belief (stable)."""
    return (r["user_state"] in ("confirmed", "corrected"), bool(r["pinned"]), r["source"] in ("stated", "corrected"), round(r["conf"], 3), r["hits"], -r["id"])


def _vectors(rows: list[dict]) -> tuple[dict, float]:
    try:
        vecs, model = embed.ensure_vectors([("fact", r["id"], r["body"]) for r in rows], embed.model_name())
        return vecs, embed.dense_floor(model)
    except Exception:
        return {}, 0.2


# ====================================================== 0. canonicalise ======================================================
def canonicalize(_rows_unused=None) -> dict:
    """One canonical predicate per concept (store.canonical): rows stored under an alias, or in a legacy shape (a "Pain or injury: " prefix, a
    skips_exercise row), are merged into the canonical belief when it is already live, otherwise renamed in place (ids, evidence, denials kept).
    Idempotent: a second pass finds nothing to change."""
    rep = {"merged": 0, "renamed": 0, "skipped": 0}
    with store._c() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM mem_fact ORDER BY id")]
    live = {(r["subject_id"], r["predicate"], r["object_key"]): r for r in rows if r["status"] in ("active", "disputed")}
    for r in rows:
        meta = json.loads(r["meta"]) if r["meta"] else None
        np_, no, nm = store.canonical(r["predicate"], r["object_text"], r["valid_from"], meta)
        nk = store.norm(no)
        if (np_, nk) == (r["predicate"], r["object_key"]):
            continue
        key = (r["subject_id"], np_, nk)
        target = live.get(key)
        is_live = r["status"] in ("active", "disputed")
        if target is not None and target["id"] != r["id"] and is_live and r["user_state"] != "denied" and target["user_state"] != "denied":
            ev = store._merge_evidence(json.loads(target["evidence"] or "[]"), [*json.loads(r["evidence"] or "[]"), {"t": "merged", "fact": r["id"]}])
            store.update_fact(target["id"], evidence=ev, salience=max(target["salience"], r["salience"]),
                              **({"pinned": True} if r["pinned"] and not target["pinned"] else {}))
            store.reinforce(target["id"], True, weight=0.5)
            store.update_fact(r["id"], status="superseded", valid_to=store.now(), meta={**(meta or {}), "merged_into": target["id"], "was": r["predicate"]})
            store.drop_embedding("fact", r["id"])
            with store._c() as c:                               # the merged-away row takes the canonical key too (history stays consistent, nothing is left to redo)
                c.execute("UPDATE mem_fact SET predicate = ?, object_text = ?, object_key = ? WHERE id = ?", (np_, no, nk, r["id"]))
            store.audit("fact.canon", r["id"], f"{r['predicate']} -> {np_} (into {target['id']})")
            rep["merged"] += 1
            continue
        if is_live and store.predicate(np_)["functional"] and any(k[0] == r["subject_id"] and k[1] == np_ and k != key for k in live):
            rep["skipped"] += 1                                 # renaming would create a second value of a single-valued predicate; consolidation decides
            continue
        with store._c() as c:
            c.execute("UPDATE mem_fact SET predicate = ?, object_text = ?, object_key = ?, meta = ? WHERE id = ?", (np_, no, nk, store._j(nm) if nm is not None else r["meta"], r["id"]))
        if is_live:
            live.pop((r["subject_id"], r["predicate"], r["object_key"]), None)
            live[key] = {**r, "predicate": np_, "object_text": no, "object_key": nk}
        store.audit("fact.canon", r["id"], f"{r['predicate']} -> {np_}")
        rep["renamed"] += 1
    return rep


# ====================================================== 1. promote ======================================================
def _promote(rows: list[dict]) -> dict:
    rep = {"promoted": 0, "reinforced": 0}
    eps = [r for r in rows if r["kind"] == "episodic" and r["status"] == "active" and r["user_state"] != "denied"]
    groups: dict[tuple, list[dict]] = {}
    for r in eps:
        groups.setdefault((r["subject_id"], r["predicate"]), []).append(r)
    existing = {(r["subject_id"], r["predicate"], r["object_key"]): r for r in rows if r["kind"] != "episodic"}
    for (sid, pred), members in groups.items():
        if len(members) < PROMOTE_MIN:
            continue
        sigs = {m["id"]: _sig(_MONTH.sub(" ", m["body"])) for m in members}
        parent = {m["id"]: m["id"] for m in members}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if _jac(sigs[a["id"]], sigs[b["id"]]) >= 0.5:
                    parent[find(a["id"])] = find(b["id"])
        clusters: dict[int, list[dict]] = {}
        for m in members:
            clusters.setdefault(find(m["id"]), []).append(m)
        for cl in clusters.values():
            days = {m["valid_from"][:10] for m in cl}
            if len(cl) < PROMOTE_MIN or len(days) < PROMOTE_DAYS:
                continue
            common = Counter(t for m in cl for t in sigs[m["id"]] if not t.startswith("§"))
            top = sorted(t for t, n in common.most_common(6) if n >= 0.6 * len(cl))
            if not top:
                continue
            medoid = max(cl, key=lambda m: sum(_jac(sigs[m["id"]], sigs[o["id"]]) for o in cl))
            latest = max(m["valid_from"] for m in cl)[:10]
            new_pred = _PROMOTED_PRED.get(pred, pred)
            obj = "recurring: " + " ".join(top)
            core = re.sub(r"^[A-Za-z]{3,9}\s+\d{1,2}:\s*", "", medoid["body"]).rstrip(".")
            body = f"Recurring pattern ({len(cl)} times, latest {latest}): {core}."
            if pred == "skips_exercise":
                ex = medoid["object_text"]
                obj, body = ex, f"You keep skipping {ex} ({len(cl)} times); you may not like it."
            key = (sid, new_pred, store.norm(obj))
            have = existing.get(key)
            if have is not None:
                prev = int((have["meta"] or {}).get("n_sources", 0))
                if len(cl) > prev:
                    store.reinforce(have["id"], True, weight=0.5 * (len(cl) - prev), evidence=[{"t": "fact", "id": m["id"]} for m in cl[:6]])
                    meta = {**(have["meta"] or {}), "n_sources": len(cl), "latest": latest}
                    store.update_fact(have["id"], meta=meta, body=body)
                    rep["reinforced"] += 1
                continue
            res = store.add_fact(new_pred, obj, subject=sid, body=body, kind="semantic", source="inferred", salience=0.55, weight=min(3.0, 1.0 + 0.25 * len(cl)),
                                 evidence=[{"t": "fact", "id": m["id"], "date": m["valid_from"][:10]} for m in cl[:10]], meta={"n_sources": len(cl), "latest": latest, "promoted": True})
            if res["created"]:
                rep["promoted"] += 1
    return rep


# ====================================================== 2. merge ======================================================
def _merge(rows: list[dict]) -> dict:
    rep = {"merged": 0}
    act = [r for r in rows if r["status"] == "active" and r["kind"] != "episodic" and r["user_state"] != "denied"]
    vecs, floor = _vectors(act)
    groups: dict[tuple, list[dict]] = {}
    for r in act:
        groups.setdefault((r["subject_id"], r["predicate"]), []).append(r)
    gone: set[int] = set()
    for members in groups.values():
        members = sorted(members, key=lambda r: -r["id"])[:400]
        sigs = {r["id"]: _sig(r["body"]) for r in members}
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if a["id"] in gone or b["id"] in gone or a["polarity"] != b["polarity"] or _neg(a) != _neg(b) or _numbers(a) != _numbers(b):
                    continue
                if a["value_num"] is not None and b["value_num"] is not None and abs(a["value_num"] - b["value_num"]) > 1e-9:
                    continue
                lex = _jac(sigs[a["id"]], sigs[b["id"]])
                va, vb = vecs.get(("fact", a["id"])), vecs.get(("fact", b["id"]))
                cosn = max(0.0, (float(va @ vb) - floor) / (1 - floor)) if va is not None and vb is not None and va.shape == vb.shape else 0.0
                if not (lex >= 0.7 or (lex >= 0.4 and cosn >= 0.75)):
                    continue
                win, lose = (a, b) if _strength(a) >= _strength(b) else (b, a)
                if lose["user_state"] in ("confirmed", "corrected"):
                    continue                                    # both confirmed by the athlete: never merge what they vouched for
                ev = store._merge_evidence(win["evidence"], [*lose["evidence"], {"t": "merged", "fact": lose["id"]}])
                store.update_fact(win["id"], evidence=ev, salience=max(win["salience"], lose["salience"]))
                store.reinforce(win["id"], True, weight=0.5)
                store.update_fact(lose["id"], status="superseded", valid_to=store.now(), meta={**(lose["meta"] or {}), "merged_into": win["id"]})
                store.drop_embedding("fact", lose["id"])
                store.audit("fact.merge", lose["id"], f"into {win['id']}")
                gone.add(lose["id"])
                rep["merged"] += 1
    return rep


# ====================================================== 3. resolve ======================================================
def _resolve(rows: list[dict]) -> dict:
    rep = {"disputes_kept_confirmed": 0, "disputes_archived": 0, "disputes_promoted": 0, "questions": 0}
    live = {(r["subject_id"], r["predicate"]): [] for r in rows}
    for r in rows:
        if r["status"] == "active":
            live[(r["subject_id"], r["predicate"])].append(r)
    for d in [r for r in rows if r["status"] == "disputed"]:
        if d["user_state"] == "denied":
            store.update_fact(d["id"], status="retracted")
            continue
        rivals = live.get((d["subject_id"], d["predicate"]), [])
        age = max(0.0, clock.age_days(d["last_reinforced"]))
        confirmed = [r for r in rivals if r["user_state"] in ("confirmed", "corrected")]
        if confirmed:
            keep = confirmed[0]
            rep["disputes_kept_confirmed"] += 1
            if d["conf"] < 0.4 or age > 60:
                store.update_fact(d["id"], status="archived")
                rep["disputes_archived"] += 1
            elif d["hits"] >= 3 and d["conf"] >= 0.75:
                try:
                    q = store.add_question(f"dispute:{d['predicate']}", f"You told me \"{keep['object_text']}\", but your logs keep pointing to \"{d['object_text']}\". Which is right now?",
                                           "Your data and what you told me disagree.", value=0.8, options=[keep["object_text"], d["object_text"]])
                    rep["questions"] += 1 if q else 0
                except Exception:
                    pass
        elif not rivals:
            store.update_fact(d["id"], status="active")           # the belief it was disputing is gone: it stands on its own
            rep["disputes_promoted"] += 1
        else:
            best = max(rivals, key=lambda r: r["conf"])
            if d["conf"] > best["conf"] + 0.10 and best["user_state"] == "none" and not best["pinned"]:
                store.update_fact(best["id"], status="superseded", valid_to=store.now())
                store.update_fact(d["id"], status="active")
                rep["disputes_promoted"] += 1
    return rep


# ====================================================== 4. decay ======================================================
def _decay(rows: list[dict]) -> dict:
    n = 0
    for r in rows:
        if r["status"] != "active" or r["pinned"] or r["user_state"] != "none":
            continue
        hl = predicate(r["predicate"])["half_life"]
        silent = clock.age_days(r["last_reinforced"])
        if r["conf"] < 0.30 or (hl and silent > 4 * hl):
            store.update_fact(r["id"], status="archived")
            store.drop_embedding("fact", r["id"])
            n += 1
    return {"archived": n}


# ====================================================== 5. entities and edges ======================================================
def _entity_for(concept: str, cache: dict) -> int:
    if concept not in cache:
        kind = _KIND_OF[concept]
        forms = [f for f in embed._GROUPS[concept].split() if "-" not in f and f.isascii()][:8]
        cache[concept] = store.entity(kind, _ENTITY_NAME.get(concept, concept), aliases=forms)
    return cache[concept]


def _link(rows: list[dict]) -> dict:
    rep = {"entities": 0, "edges": 0, "linked_objects": 0}
    cache: dict[str, int] = {}
    with store._c() as c:
        have = {(r["src"], r["dst"]) for r in c.execute("SELECT src, dst FROM mem_edge")}
        have |= {(b, a) for a, b in have}
    for r in rows:
        if r["status"] != "active":
            continue
        toks = embed.tokenize(r["body"] + " " + r["object_text"])
        found = [c for c in embed.concepts_of(toks) if c in _KIND_OF]
        if not found:
            continue
        ids = []
        for c in found:
            before = len(cache)
            ids.append((c, _entity_for(c, cache)))
            rep["entities"] += len(cache) - before
        if r["object_entity"] is None and r["predicate"] in ("injury_site", "likes_exercise", "dislikes_exercise", "avoids_movement", "likes_food", "dislikes_food", "allergy",
                                                             "pain_aggravated_by", "pain_relieved_by"):
            store.update_fact(r["id"], object_entity=ids[0][1])
            rep["linked_objects"] += 1
        ids.sort(key=lambda t: _KIND_ORDER[_KIND_OF[t[0]]])
        for i, (ca, ea) in enumerate(ids):
            for cb, eb in ids[i + 1:]:
                if ea == eb or (ea, eb) in have:
                    continue
                rel = "aggravated_by" if r["predicate"] in ("injury_site", "pain_aggravated_by") and _KIND_OF[ca] == "body_part" and _KIND_OF[cb] == "exercise" \
                    else "relieved_by" if r["predicate"] == "pain_relieved_by" else "co_mentioned"
                store.add_edge(ea, rel, eb, weight=1.0, fact_id=r["id"])
                have |= {(ea, eb), (eb, ea)}
                rep["edges"] += 1
    return rep


# ====================================================== run ======================================================
def run() -> dict:
    """The full sleep pass. Idempotent; never raises. `baseline` is kept for the older job test (it asserted the first, archive-only version)."""
    rep: dict = {"baseline": True}
    steps = (("canon", canonicalize), ("promote", _promote), ("merge", _merge), ("resolve", _resolve), ("decay", _decay), ("link", _link))
    for name, fn in steps:
        try:
            rep[name] = fn(_rows())
        except Exception as e:
            rep[name] = {"error": f"{type(e).__name__}: {e}"[:160]}
    rep["archived"] = (rep.get("decay") or {}).get("archived", 0)
    try:
        rep["reindex"] = embed.reindex()
    except Exception as e:
        rep["reindex"] = {"error": f"{type(e).__name__}: {e}"[:160]}
    return rep


class _CallableModule(types.ModuleType):
    """`mem.consolidate()` (the facade) and this module share a name; importing the module replaces the facade function on the package, so the
    module itself is callable."""

    def __call__(self, *a, **kw):
        return run(*a, **kw)


sys.modules[__name__].__class__ = _CallableModule
