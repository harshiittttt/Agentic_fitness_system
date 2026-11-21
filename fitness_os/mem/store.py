"""The athlete memory store: one SQLite file, several models of data (relational + graph + full-text + vectors + time-versioned beliefs).

This is the ONLY module that writes the `mem_*` tables. It is deliberately dumb about meaning (no model calls, no statistics, no ranking
beyond BM25): retrieval, consolidation, mining and synthesis live in their own modules and talk to the data through these functions.

Beliefs, not notes: a fact has a Beta(alpha, beta) posterior (confidence = mean), a valid-time interval (valid_from .. valid_to), a
transaction time (observed_at), provenance (`source`, `evidence`) and a lifecycle (`status`, `user_state`). New evidence reinforces or
contradicts; a better-evidenced value for a single-valued predicate supersedes the old one instead of overwriting it, so "you used to ... but
now ..." is answerable. What the athlete denies stays denied (a denied fact is never resurrected by a lower-trust source).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from typing import Any, Iterable

from backend.memory import db

from .. import clock
from .types import PRIOR, SOURCES, predicate

SCHEMA = """
CREATE TABLE IF NOT EXISTS mem_entity (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL, canonical TEXT NOT NULL, aliases TEXT, created_ts TEXT NOT NULL,
  UNIQUE (kind, canonical)
);
CREATE TABLE IF NOT EXISTS mem_fact (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_id INTEGER NOT NULL, predicate TEXT NOT NULL, object_text TEXT NOT NULL, object_key TEXT NOT NULL, object_entity INTEGER,
  value_num REAL, unit TEXT, polarity INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'semantic', body TEXT NOT NULL,
  source TEXT NOT NULL, sensitivity TEXT NOT NULL DEFAULT 'normal',
  alpha REAL NOT NULL, beta REAL NOT NULL, confidence REAL NOT NULL, salience REAL NOT NULL DEFAULT 0.5, pinned INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', user_state TEXT NOT NULL DEFAULT 'none',
  valid_from TEXT NOT NULL, valid_to TEXT, observed_at TEXT NOT NULL, last_reinforced TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 1,
  supersedes INTEGER, evidence TEXT, meta TEXT
);
CREATE INDEX IF NOT EXISTS mem_fact_lookup ON mem_fact (subject_id, predicate, status);
CREATE INDEX IF NOT EXISTS mem_fact_status ON mem_fact (status, kind);
CREATE TABLE IF NOT EXISTS mem_edge (
  id INTEGER PRIMARY KEY AUTOINCREMENT, src INTEGER NOT NULL, rel TEXT NOT NULL, dst INTEGER NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
  fact_id INTEGER, valid_from TEXT NOT NULL, valid_to TEXT, UNIQUE (src, rel, dst)
);
CREATE TABLE IF NOT EXISTS mem_embedding (
  item_type TEXT NOT NULL, item_id INTEGER NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL, text_hash TEXT NOT NULL,
  PRIMARY KEY (item_type, item_id, model)
);
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(body, item_type UNINDEXED, item_id UNINDEXED, tokenize = 'porter unicode61');
CREATE TABLE IF NOT EXISTS mem_discovery (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, family TEXT NOT NULL, title TEXT NOT NULL, statement TEXT NOT NULL,
  effect TEXT, recommendation TEXT, consumers TEXT, status TEXT NOT NULL DEFAULT 'candidate', evidence TEXT, data_hash TEXT,
  first_seen TEXT NOT NULL, last_tested TEXT NOT NULL, times_tested INTEGER NOT NULL DEFAULT 1, times_replicated INTEGER NOT NULL DEFAULT 0,
  surfaced_ts TEXT, user_state TEXT NOT NULL DEFAULT 'none'
);
CREATE TABLE IF NOT EXISTS mem_prediction (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, target TEXT NOT NULL, subject TEXT, horizon_ts TEXT NOT NULL,
  predicted REAL NOT NULL, lo REAL, hi REAL, model TEXT, features TEXT, actual REAL, error REAL, resolved_ts TEXT
);
CREATE TABLE IF NOT EXISTS mem_question (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, topic TEXT NOT NULL, question TEXT NOT NULL, why TEXT, value REAL NOT NULL DEFAULT 0.5,
  options TEXT, status TEXT NOT NULL DEFAULT 'open', answer TEXT, asked_ts TEXT, fact_id INTEGER
);
CREATE TABLE IF NOT EXISTS mem_dossier (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, version INTEGER NOT NULL, sections TEXT NOT NULL, narrative TEXT, diff TEXT, data_hash TEXT
);
CREATE TABLE IF NOT EXISTS mem_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT NOT NULL, ref TEXT, detail TEXT);
"""
_JSON_FIELDS = ("aliases", "evidence", "meta", "effect", "consumers", "features", "options", "sections", "diff")


_schema_ok: set = set()          # (db path, file creation stamp) whose belief-store schema this process has already created


def _stamp():
    try:
        return (db.DB_PATH, os.stat(db.DB_PATH).st_ctime_ns)
    except OSError:
        return None                  # no file yet (or unreadable): always run the script


@contextmanager
def _c():
    with db.conn() as c:             # db.conn() has just created the file if it was missing
        key = _stamp()
        if key is None or key not in _schema_ok:
            c.executescript(SCHEMA)
            key = _stamp()
            if key is not None:
                _schema_ok.add(key)
        yield c


# ---- one canonical predicate per concept ---------------------------------------------------------------------------------------------------
# The legacy mirror (migrate.py), the regex/LLM ingest paths and the miner all write beliefs. The same fact must land on ONE (predicate, object) key or
# it is stored twice. Aliases are spellings a model or an older module may use; add_fact() and the consolidation pass both route through canonical().
ALIASES = {
    "likes_exercises": "likes_exercise", "likes_workout": "likes_exercise", "enjoys_exercise": "likes_exercise", "favorite_exercise": "likes_exercise", "favourite_exercise": "likes_exercise",
    "dislike_exercise": "dislikes_exercise", "dislikes_exercises": "dislikes_exercise", "dislikes_workout": "dislikes_exercise", "avoids_exercise": "dislikes_exercise",
    "hates_exercise": "dislikes_exercise",
    "likes_foods": "likes_food", "favorite_food": "likes_food", "favourite_food": "likes_food", "dislikes_foods": "dislikes_food", "food_dislike": "dislikes_food",
    "injury": "injury_site", "injuries": "injury_site", "injured_site": "injury_site", "pain_site": "injury_site", "current_injury": "injury_site", "injury_location": "injury_site",
    "allergies": "allergy", "food_allergy": "allergy", "allergic_to": "allergy",
    "equipment": "available_equipment", "equipment_available": "available_equipment", "available_equipments": "available_equipment", "has_equipment": "available_equipment",
    "training_days_per_week": "trains_days_per_week", "days_per_week": "trains_days_per_week", "workout_days_per_week": "trains_days_per_week", "available_days": "trains_days_per_week",
    "session_length": "prefers_session_minutes", "session_minutes": "prefers_session_minutes", "preferred_session_minutes": "prefers_session_minutes",
    "workout_duration": "prefers_session_minutes",
    "diet": "diet_type", "dietary_preference": "diet_type", "diet_preference": "diet_type",
    "goal_weight": "target_weight_kg", "weight_goal": "target_weight_kg", "target_weight": "target_weight_kg",
    "experience": "experience_level", "training_experience": "experience_level", "fitness_level": "experience_level",
    "preferred_training_time": "prefers_training_time", "training_time": "prefers_training_time", "workout_time": "prefers_training_time",
    "fitness_goal": "goal", "primary_goal": "goal", "goals": "goal",
    "bedtime": "sleep_schedule", "sleep_time": "sleep_schedule",
}
_INJ_PREFIX = re.compile(r"^(?:pain|injury)(?:\s*(?:or|and|/)\s*(?:pain|injury))?\s*:\s*", re.I)


def canonical_pred(pred: str) -> str:
    p = re.sub(r"[^a-z0-9_]+", "_", str(pred or "").strip().lower()).strip("_")
    return ALIASES.get(p, p)


def canonical(pred: str, obj: str, valid_from: str | None = None, meta: dict | None = None) -> tuple[str, str, dict | None]:
    """(predicate, object, meta) -> the canonical form. Idempotent. Beyond spelling aliases:
    * injury_site: the legacy "Pain or injury: left knee" prefix is dropped, so it matches ingest's "left knee";
    * skips_exercise (legacy "You skipped X"): becomes the episode ingest writes, `happened` "skipped X on <day>", so one skip is one row."""
    pred, obj = canonical_pred(pred), str(obj).strip()
    if pred == "injury_site":
        obj = _INJ_PREFIX.sub("", obj).rstrip(" .") or obj
    elif pred == "skips_exercise":
        ex, day = norm(obj), str(valid_from or now())[:10]
        return "happened", f"skipped {ex} on {day}", {**(meta or {}), "event": "skip", "exercise": ex, "date": day}
    return pred, obj, meta


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in _JSON_FIELDS:
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    if "pinned" in d:
        d["pinned"] = bool(d["pinned"])
    return d


def _j(x: Any) -> str | None:
    return None if x is None else json.dumps(x, ensure_ascii=False, default=str)


def norm(text: str) -> str:
    """Canonical key for matching the same thing said twice: lowercase, collapsed whitespace, no surrounding punctuation."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower()).strip(" .,;:!?\"'")


def now() -> str:
    return clock.iso()


def audit(action: str, ref: str = "", detail: str = "", c=None) -> None:
    def go(cx):
        cx.execute("INSERT INTO mem_audit (ts, action, ref, detail) VALUES (?,?,?,?)", (now(), action, str(ref)[:80], str(detail)[:300]))
    if c is not None:
        go(c)
    else:
        with _c() as cx:
            go(cx)


# ====================================================== entities and the graph ======================================================
def entity(kind: str, name: str, aliases: Iterable[str] | None = None) -> int:
    """Get or create the entity (kind, name). Names match case-insensitively; aliases are merged in."""
    canon = norm(name)
    if not canon:
        raise ValueError("entity needs a name")
    with _c() as c:
        r = c.execute("SELECT * FROM mem_entity WHERE kind = ? AND canonical = ?", (kind, canon)).fetchone()
        if r:
            if aliases:
                have = set(json.loads(r["aliases"] or "[]"))
                new = have | {norm(a) for a in aliases if norm(a)}
                if new != have:
                    c.execute("UPDATE mem_entity SET aliases = ? WHERE id = ?", (_j(sorted(new)), r["id"]))
            return r["id"]
        return c.execute("INSERT INTO mem_entity (kind, name, canonical, aliases, created_ts) VALUES (?,?,?,?,?)",
                         (kind, name.strip(), canon, _j(sorted({norm(a) for a in (aliases or []) if norm(a)})), now())).lastrowid


def athlete_id() -> int:
    return entity("athlete", "athlete", aliases=("me", "you", "i"))


def get_entity(entity_id: int) -> dict | None:
    with _c() as c:
        return _row(c.execute("SELECT * FROM mem_entity WHERE id = ?", (entity_id,)).fetchone())


def find_entities(text: str, limit: int = 8) -> list[dict]:
    """Entities mentioned in `text` (whole-word match on name or alias). The athlete entity is excluded (it is implicit)."""
    low = " " + norm(text) + " "
    out = []
    with _c() as c:
        for r in c.execute("SELECT * FROM mem_entity WHERE kind != 'athlete'").fetchall():
            names = [r["canonical"], *json.loads(r["aliases"] or "[]")]
            if any(n and f" {n} " in low for n in names):
                out.append(_row(r))
    return out[:limit]


def add_edge(src: int, rel: str, dst: int, weight: float = 1.0, fact_id: int | None = None) -> int:
    with _c() as c:
        r = c.execute("SELECT id, weight FROM mem_edge WHERE src = ? AND rel = ? AND dst = ?", (src, rel, dst)).fetchone()
        if r:
            c.execute("UPDATE mem_edge SET weight = ?, fact_id = COALESCE(?, fact_id) WHERE id = ?", (min(5.0, r["weight"] + 0.25 * weight), fact_id, r["id"]))
            return r["id"]
        return c.execute("INSERT INTO mem_edge (src, rel, dst, weight, fact_id, valid_from) VALUES (?,?,?,?,?,?)", (src, rel, dst, weight, fact_id, now())).lastrowid


def neighbors(entity_id: int, rels: Iterable[str] | None = None, limit: int = 50) -> list[dict]:
    """Edges touching the entity in either direction: [{id, src, rel, dst, weight, fact_id, other}] strongest first."""
    with _c() as c:
        rows = c.execute("SELECT * FROM mem_edge WHERE (src = ? OR dst = ?) AND valid_to IS NULL ORDER BY weight DESC LIMIT ?", (entity_id, entity_id, limit * 2)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["other"] = d["dst"] if d["src"] == entity_id else d["src"]
        if rels is None or d["rel"] in rels:
            out.append(d)
    return out[:limit]


# ====================================================== full text + vectors ======================================================
def _fts_put(c, item_type: str, item_id: int, text: str) -> None:
    c.execute("DELETE FROM mem_fts WHERE item_type = ? AND item_id = ?", (item_type, item_id))
    if text and text.strip():
        c.execute("INSERT INTO mem_fts (body, item_type, item_id) VALUES (?,?,?)", (text, item_type, item_id))


def fts_query(text: str) -> str:
    words = [w for w in re.findall(r"[\wऀ-ॿ]+", text.lower()) if len(w) > 1]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def fts_search(query: str, limit: int = 30, item_types: Iterable[str] | None = None) -> list[tuple[str, int, float]]:
    """BM25 over everything indexed -> [(item_type, item_id, score)] best first. score > 0, higher is better. Empty/odd queries return []."""
    q = fts_query(query)
    if not q:
        return []
    with _c() as c:
        try:
            rows = c.execute("SELECT item_type, item_id, bm25(mem_fts) AS s FROM mem_fts WHERE mem_fts MATCH ? ORDER BY s LIMIT ?", (q, limit * 3)).fetchall()
        except Exception:
            return []
    ok = set(item_types) if item_types else None
    return [(r["item_type"], int(r["item_id"]), float(-r["s"])) for r in rows if ok is None or r["item_type"] in ok][:limit]


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def set_embedding(item_type: str, item_id: int, vec, model: str, thash: str) -> None:
    import numpy as np
    a = np.asarray(vec, dtype="float32")
    with _c() as c:
        c.execute("INSERT OR REPLACE INTO mem_embedding (item_type, item_id, model, dim, vec, text_hash) VALUES (?,?,?,?,?,?)",
                  (item_type, item_id, model, int(a.shape[0]), a.tobytes(), thash))


def get_embeddings(model: str, item_type: str | None = None) -> dict[tuple[str, int], tuple[Any, str]]:
    """{(item_type, item_id): (numpy float32 vector, text_hash)} for one embedding model."""
    import numpy as np
    with _c() as c:
        rows = c.execute("SELECT * FROM mem_embedding WHERE model = ?" + (" AND item_type = ?" if item_type else ""), (model, item_type) if item_type else (model,)).fetchall()
    return {(r["item_type"], r["item_id"]): (np.frombuffer(r["vec"], dtype="float32"), r["text_hash"]) for r in rows}


def drop_embedding(item_type: str, item_id: int) -> None:
    with _c() as c:
        c.execute("DELETE FROM mem_embedding WHERE item_type = ? AND item_id = ?", (item_type, item_id))


# ====================================================== beliefs (facts) ======================================================
def confidence(f: dict, at: str | None = None) -> float:
    """Effective confidence: the Beta mean, relaxing toward 0.5 (don't know) when nothing has reinforced it for a while. Pinned facts never relax."""
    base = f["alpha"] / (f["alpha"] + f["beta"])
    hl = predicate(f["predicate"])["half_life"]
    if f.get("pinned") or not hl:
        return base
    age = max(0.0, (clock.parse(at) if at else clock.now()).timestamp() - clock.parse(f["last_reinforced"]).timestamp()) / 86400.0
    return 0.5 + (base - 0.5) * 0.5 ** (age / hl)


def _subject(subject) -> int:
    if subject is None:
        return athlete_id()
    if isinstance(subject, int):
        return subject
    kind, name = subject
    return entity(kind, name)


def _default_body(subject_id: int, pred: str, obj: str) -> str:
    return f"{pred.replace('_', ' ')}: {obj}"


def get_fact(fact_id: int) -> dict | None:
    with _c() as c:
        return _row(c.execute("SELECT * FROM mem_fact WHERE id = ?", (fact_id,)).fetchone())


def add_fact(pred: str, obj: str, *, subject=None, body: str | None = None, value_num: float | None = None, unit: str | None = None, polarity: int = 0,
             kind: str = "semantic", source: str = "stated", sensitivity: str | None = None, salience: float = 0.5, pinned: bool | None = None,
             valid_from: str | None = None, valid_to: str | None = None, evidence: list | None = None, meta: dict | None = None,
             object_entity: int | None = None, status: str = "active", weight: float = 1.0) -> dict:
    """Record a belief. -> {id, created, reinforced, superseded: [ids], disputed: [ids], blocked: reason|None}

    * the same (subject, predicate, object) already active -> reinforced (Beta alpha += weight), evidence merged, nothing duplicated
    * the athlete previously DENIED this fact and the source is not the athlete -> blocked (what is forgotten stays forgotten)
    * a single-valued predicate with a different active value -> the new value supersedes the old, unless the old one is user-confirmed and
      the new one is not from the athlete, in which case the new one is stored as 'disputed' (consolidation / a question resolves it)
    """
    if source not in SOURCES:
        raise ValueError(f"unknown source {source}")
    obj = str(obj).strip()
    if not obj:
        raise ValueError("a fact needs an object")
    pred, obj, meta = canonical(pred, obj, valid_from, meta)
    spec = predicate(pred)
    sid, okey, ts = _subject(subject), norm(obj), now()
    sens = sensitivity or spec["sensitivity"]
    pin = bool(pinned) if pinned is not None else (spec["half_life"] is None and sens == "health")
    body = (body or _default_body(sid, pred, obj)).strip()
    a0, b0 = PRIOR[source]
    out = {"id": None, "created": False, "reinforced": False, "superseded": [], "disputed": [], "blocked": None}
    with _c() as c:
        same = c.execute("SELECT * FROM mem_fact WHERE subject_id = ? AND predicate = ? AND object_key = ? ORDER BY id DESC", (sid, pred, okey)).fetchall()
        live = next((r for r in same if r["status"] in ("active", "disputed")), None)
        denied = next((r for r in same if r["user_state"] == "denied"), None)
        if denied and source not in ("stated", "corrected"):
            out.update(id=denied["id"], blocked="the athlete denied this")
            return out
        if live:
            ev = _merge_evidence(json.loads(live["evidence"] or "[]"), evidence)
            a, b = live["alpha"] + weight, live["beta"]
            upgrade = "stated" if (source in ("stated", "corrected") and live["source"] not in ("stated", "corrected")) else live["source"]
            c.execute("UPDATE mem_fact SET alpha = ?, confidence = ?, hits = hits + 1, last_reinforced = ?, evidence = ?, source = ?, salience = MAX(salience, ?), "
                      "value_num = COALESCE(?, value_num), body = ? WHERE id = ?", (a, a / (a + b), ts, _j(ev), upgrade, salience, value_num, body if source != "inferred" else live["body"], live["id"]))
            out.update(id=live["id"], reinforced=True)
            return out
        if status == "active" and spec["functional"]:
            olds = c.execute("SELECT * FROM mem_fact WHERE subject_id = ? AND predicate = ? AND status = 'active'", (sid, pred)).fetchall()
            for o in olds:
                if o["user_state"] in ("confirmed", "corrected") and source not in ("stated", "corrected"):
                    status = "disputed"
                    out["disputed"].append(o["id"])
        vf = valid_from or ts
        fid = c.execute(
            "INSERT INTO mem_fact (subject_id, predicate, object_text, object_key, object_entity, value_num, unit, polarity, kind, body, source, sensitivity, alpha, beta, "
            "confidence, salience, pinned, status, user_state, valid_from, valid_to, observed_at, last_reinforced, hits, evidence, meta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'none',?,?,?,?,1,?,?)",
            (sid, pred, obj, okey, object_entity, value_num, unit, polarity, kind, body, source, sens, a0 + (weight - 1.0), b0, (a0 + weight - 1.0) / (a0 + weight - 1.0 + b0),
             salience, int(pin), status, vf, valid_to, ts, ts, _j(evidence or []), _j(meta))).lastrowid
        out.update(id=fid, created=True)
        if status == "active" and spec["functional"]:
            for o in olds:
                c.execute("UPDATE mem_fact SET status = 'superseded', valid_to = ? WHERE id = ?", (vf, o["id"]))
                c.execute("UPDATE mem_fact SET supersedes = ? WHERE id = ?", (o["id"], fid))
                _fts_put(c, "fact", o["id"], "")
                out["superseded"].append(o["id"])
        if status == "active":
            _fts_put(c, "fact", fid, body)
        audit("fact.add", fid, f"{pred}={obj[:60]} ({source})", c)
    return out


def _merge_evidence(old: list, new: list | None) -> list:
    seen, out = set(), []
    for e in [*(old or []), *(new or [])]:
        k = json.dumps(e, sort_keys=True, default=str)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out[-40:]


def facts(*, subject=None, predicate: str | None = None, kind: str | None = None, status: Iterable[str] | None = ("active",), source: str | None = None,
          user_state: str | None = None, sensitivity: str | None = None, as_of: str | None = None, min_confidence: float | None = None,
          limit: int = 200, order: str = "confidence") -> list[dict]:
    """Query beliefs. `as_of` answers 'what did I believe was true on that date' (valid-time): superseded facts valid then are included.
    Returned rows carry `conf` (the effective, decayed confidence) next to the stored `confidence`."""
    q, a = "SELECT * FROM mem_fact WHERE 1=1", []
    if subject is not None:
        q += " AND subject_id = ?"; a.append(_subject(subject))
    for col, val in (("predicate", predicate), ("kind", kind), ("source", source), ("user_state", user_state), ("sensitivity", sensitivity)):
        if val is not None:
            q += f" AND {col} = ?"; a.append(val)
    if as_of:
        q += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) AND status NOT IN ('retracted','disputed')"; a += [as_of, as_of]
    elif status is not None:
        st = list(status)
        q += f" AND status IN ({','.join('?' * len(st))})"; a += st
    order_sql = {"confidence": "confidence DESC", "recent": "last_reinforced DESC", "salience": "salience DESC", "id": "id DESC"}.get(order, "confidence DESC")
    with _c() as c:
        rows = [_row(r) for r in c.execute(q + f" ORDER BY {order_sql} LIMIT ?", (*a, limit * 3 if min_confidence else limit)).fetchall()]
    for r in rows:
        r["conf"] = round(confidence(r, as_of), 4)
    if min_confidence is not None:
        rows = [r for r in rows if r["conf"] >= min_confidence]
    return rows[:limit]


def update_fact(fact_id: int, **fields) -> None:
    allowed = {"body", "salience", "pinned", "status", "user_state", "sensitivity", "valid_to", "evidence", "meta", "value_num", "unit", "kind", "object_entity"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update {sorted(bad)}")
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = [(_j(v) if k in ("evidence", "meta") else int(v) if k == "pinned" else v) for k, v in fields.items()]
    with _c() as c:
        c.execute(f"UPDATE mem_fact SET {cols} WHERE id = ?", (*vals, fact_id))
        r = c.execute("SELECT status, body FROM mem_fact WHERE id = ?", (fact_id,)).fetchone()
        if r:
            _fts_put(c, "fact", fact_id, r["body"] if r["status"] == "active" else "")


def reinforce(fact_id: int, supports: bool = True, weight: float = 1.0, evidence: list | None = None) -> dict | None:
    """New evidence for (supports=True) or against a belief. Moves alpha/beta; returns the updated row."""
    with _c() as c:
        r = c.execute("SELECT * FROM mem_fact WHERE id = ?", (fact_id,)).fetchone()
        if not r:
            return None
        a, b = r["alpha"] + (weight if supports else 0.0), r["beta"] + (0.0 if supports else weight)
        ev = _merge_evidence(json.loads(r["evidence"] or "[]"), evidence)
        c.execute("UPDATE mem_fact SET alpha = ?, beta = ?, confidence = ?, hits = hits + ?, last_reinforced = ?, evidence = ? WHERE id = ?",
                  (a, b, a / (a + b), 1 if supports else 0, now() if supports else r["last_reinforced"], _j(ev), fact_id))
    return get_fact(fact_id)


def supersede(old_id: int, new_id: int, valid_to: str | None = None) -> None:
    with _c() as c:
        c.execute("UPDATE mem_fact SET status = 'superseded', valid_to = ? WHERE id = ?", (valid_to or now(), old_id))
        c.execute("UPDATE mem_fact SET supersedes = ? WHERE id = ?", (old_id, new_id))
        _fts_put(c, "fact", old_id, "")
        audit("fact.supersede", old_id, f"by {new_id}", c)


def user_confirm(fact_id: int) -> dict | None:
    """The athlete says 'yes, that is right': strong evidence, and the fact can no longer be overridden by anything but the athlete."""
    f = reinforce(fact_id, True, weight=4.0)
    update_fact(fact_id, user_state="confirmed", status="active")
    audit("fact.confirm", fact_id)
    return get_fact(fact_id)


def user_deny(fact_id: int, reason: str = "") -> dict | None:
    """'No, that is wrong' / 'forget that': retracted, removed from search, and never resurrected by a non-athlete source."""
    with _c() as c:
        c.execute("UPDATE mem_fact SET status = 'retracted', user_state = 'denied', valid_to = ? WHERE id = ?", (now(), fact_id))
        _fts_put(c, "fact", fact_id, "")
        audit("fact.deny", fact_id, reason, c)
    drop_embedding("fact", fact_id)
    return get_fact(fact_id)


def user_correct(fact_id: int, new_object: str, body: str | None = None, value_num: float | None = None) -> dict | None:
    """'Actually it is X': the old value is superseded and the athlete's correction becomes a strongly-held fact."""
    old = get_fact(fact_id)
    if not old:
        return None
    with _c() as c:
        c.execute("UPDATE mem_fact SET status = 'superseded', user_state = 'denied', valid_to = ? WHERE id = ?", (now(), fact_id))
        _fts_put(c, "fact", fact_id, "")
    res = add_fact(old["predicate"], new_object, subject=old["subject_id"], body=body, value_num=value_num, unit=old["unit"], polarity=old["polarity"], kind=old["kind"],
                   source="corrected", sensitivity=old["sensitivity"], pinned=old["pinned"], evidence=[{"t": "correction", "of": fact_id}])
    if res["id"]:
        update_fact(res["id"], user_state="corrected")
        with _c() as c:
            c.execute("UPDATE mem_fact SET supersedes = ? WHERE id = ?", (fact_id, res["id"]))
        audit("fact.correct", fact_id, f"-> {res['id']}")
    return get_fact(res["id"]) if res["id"] else None


# ====================================================== discoveries (validated patterns) ======================================================
def upsert_discovery(key: str, family: str, title: str, statement: str, effect: dict | None = None, *, recommendation: str | None = None,
                     consumers: list[str] | None = None, status: str | None = None, evidence: list | None = None, data_hash: str | None = None,
                     replicated: bool | None = None) -> dict:
    """Create or refresh a validated pattern about THIS athlete. `key` is a stable hash of the hypothesis (family + variables + lag), so re-testing
    the same hypothesis updates the same row. `replicated=True` means the latest test agreed with the stored direction."""
    ts = now()
    with _c() as c:
        r = c.execute("SELECT * FROM mem_discovery WHERE key = ?", (key,)).fetchone()
        if r:
            c.execute("UPDATE mem_discovery SET family = ?, title = ?, statement = ?, effect = ?, recommendation = ?, consumers = ?, status = ?, evidence = ?, data_hash = ?, "
                      "last_tested = ?, times_tested = times_tested + 1, times_replicated = times_replicated + ? WHERE id = ?",
                      (family, title, statement, _j(effect), recommendation, _j(consumers), status or r["status"], _j(evidence), data_hash, ts, int(bool(replicated)), r["id"]))
            did = r["id"]
        else:
            did = c.execute("INSERT INTO mem_discovery (key, family, title, statement, effect, recommendation, consumers, status, evidence, data_hash, first_seen, last_tested, times_replicated) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (key, family, title, statement, _j(effect), recommendation, _j(consumers), status or "candidate", _j(evidence),
                                                                   data_hash, ts, ts, int(bool(replicated)))).lastrowid
        st = c.execute("SELECT status FROM mem_discovery WHERE id = ?", (did,)).fetchone()["status"]
        _fts_put(c, "discovery", did, f"{title}. {statement}" if st in ("candidate", "confirmed", "weakening") else "")
    return get_discovery(did)


def get_discovery(discovery_id: int) -> dict | None:
    with _c() as c:
        return _row(c.execute("SELECT * FROM mem_discovery WHERE id = ?", (discovery_id,)).fetchone())


def discovery_by_key(key: str) -> dict | None:
    with _c() as c:
        return _row(c.execute("SELECT * FROM mem_discovery WHERE key = ?", (key,)).fetchone())


def discoveries(status: Iterable[str] | None = ("confirmed", "weakening"), family: str | None = None, consumer: str | None = None, limit: int = 50) -> list[dict]:
    q, a = "SELECT * FROM mem_discovery WHERE 1=1", []
    if status is not None:
        st = list(status)
        q += f" AND status IN ({','.join('?' * len(st))})"; a += st
    if family:
        q += " AND family = ?"; a.append(family)
    with _c() as c:
        rows = [_row(r) for r in c.execute(q + " ORDER BY times_replicated DESC, last_tested DESC LIMIT ?", (*a, limit * 2)).fetchall()]
    if consumer:
        rows = [r for r in rows if not r.get("consumers") or consumer in r["consumers"]]
    return rows[:limit]


def set_discovery(discovery_id: int, **fields) -> None:
    allowed = {"status", "surfaced_ts", "user_state"}
    if set(fields) - allowed:
        raise ValueError("only status, surfaced_ts, user_state can be set")
    with _c() as c:
        c.execute(f"UPDATE mem_discovery SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), discovery_id))
        r = c.execute("SELECT * FROM mem_discovery WHERE id = ?", (discovery_id,)).fetchone()
        if r:
            live = r["status"] in ("candidate", "confirmed", "weakening") and r["user_state"] != "denied"
            _fts_put(c, "discovery", discovery_id, f"{r['title']}. {r['statement']}" if live else "")


# ====================================================== predictions (and their errors) ======================================================
def log_prediction(target: str, predicted: float, horizon_ts: str, *, subject: str | None = None, lo: float | None = None, hi: float | None = None,
                   model: str = "", features: dict | None = None) -> int:
    with _c() as c:
        return c.execute("INSERT INTO mem_prediction (ts, target, subject, horizon_ts, predicted, lo, hi, model, features) VALUES (?,?,?,?,?,?,?,?,?)",
                         (now(), target, subject, horizon_ts, float(predicted), lo, hi, model, _j(features))).lastrowid


def resolve_prediction(prediction_id: int, actual: float) -> dict | None:
    with _c() as c:
        r = c.execute("SELECT * FROM mem_prediction WHERE id = ?", (prediction_id,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE mem_prediction SET actual = ?, error = ?, resolved_ts = ? WHERE id = ?", (float(actual), float(actual) - r["predicted"], now(), prediction_id))
        return _row(c.execute("SELECT * FROM mem_prediction WHERE id = ?", (prediction_id,)).fetchone())


def predictions(target: str | None = None, subject: str | None = None, resolved: bool | None = None, limit: int = 200) -> list[dict]:
    q, a = "SELECT * FROM mem_prediction WHERE 1=1", []
    if target:
        q += " AND target = ?"; a.append(target)
    if subject:
        q += " AND subject = ?"; a.append(subject)
    if resolved is True:
        q += " AND actual IS NOT NULL"
    elif resolved is False:
        q += " AND actual IS NULL"
    with _c() as c:
        return [_row(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*a, limit)).fetchall()]


# ====================================================== questions (active learning) ======================================================
def add_question(topic: str, question: str, why: str = "", value: float = 0.5, options: list[str] | None = None) -> dict:
    """One OPEN question per topic: asking again refreshes it (and keeps the higher value) instead of piling up."""
    with _c() as c:
        r = c.execute("SELECT * FROM mem_question WHERE topic = ? AND status IN ('open','asked')", (topic,)).fetchone()
        if r:
            c.execute("UPDATE mem_question SET value = MAX(value, ?), why = COALESCE(NULLIF(?, ''), why) WHERE id = ?", (value, why, r["id"]))
            return _row(c.execute("SELECT * FROM mem_question WHERE id = ?", (r["id"],)).fetchone())
        qid = c.execute("INSERT INTO mem_question (ts, topic, question, why, value, options) VALUES (?,?,?,?,?,?)", (now(), topic, question, why, value, _j(options))).lastrowid
        return _row(c.execute("SELECT * FROM mem_question WHERE id = ?", (qid,)).fetchone())


def questions(status: Iterable[str] = ("open",), limit: int = 10) -> list[dict]:
    st = list(status)
    with _c() as c:
        return [_row(r) for r in c.execute(f"SELECT * FROM mem_question WHERE status IN ({','.join('?' * len(st))}) ORDER BY value DESC, id LIMIT ?", (*st, limit)).fetchall()]


def mark_asked(question_id: int) -> None:
    with _c() as c:
        c.execute("UPDATE mem_question SET status = 'asked', asked_ts = ? WHERE id = ?", (now(), question_id))


def answer_question(question_id: int, answer: str, fact_id: int | None = None) -> dict | None:
    with _c() as c:
        c.execute("UPDATE mem_question SET status = 'answered', answer = ?, fact_id = ? WHERE id = ?", (answer, fact_id, question_id))
        return _row(c.execute("SELECT * FROM mem_question WHERE id = ?", (question_id,)).fetchone())


def dismiss_question(question_id: int) -> None:
    with _c() as c:
        c.execute("UPDATE mem_question SET status = 'dismissed' WHERE id = ?", (question_id,))


# ====================================================== dossier versions ======================================================
def save_dossier(sections: dict, narrative: str = "", diff: dict | None = None, data_hash: str | None = None) -> dict:
    with _c() as c:
        v = (c.execute("SELECT COALESCE(MAX(version), 0) AS v FROM mem_dossier").fetchone()["v"]) + 1
        did = c.execute("INSERT INTO mem_dossier (ts, version, sections, narrative, diff, data_hash) VALUES (?,?,?,?,?,?)", (now(), v, _j(sections), narrative, _j(diff), data_hash)).lastrowid
        return _row(c.execute("SELECT * FROM mem_dossier WHERE id = ?", (did,)).fetchone())


def latest_dossier() -> dict | None:
    with _c() as c:
        return _row(c.execute("SELECT * FROM mem_dossier ORDER BY version DESC LIMIT 1").fetchone())


def dossier_history(limit: int = 12) -> list[dict]:
    with _c() as c:
        return [_row(r) for r in c.execute("SELECT * FROM mem_dossier ORDER BY version DESC LIMIT ?", (limit,)).fetchall()]


# ====================================================== housekeeping ======================================================
def stats() -> dict:
    with _c() as c:
        n = lambda sql: c.execute(sql).fetchone()[0]
        return {"entities": n("SELECT COUNT(*) FROM mem_entity"), "facts_active": n("SELECT COUNT(*) FROM mem_fact WHERE status = 'active'"),
                "facts_total": n("SELECT COUNT(*) FROM mem_fact"), "edges": n("SELECT COUNT(*) FROM mem_edge"), "embeddings": n("SELECT COUNT(*) FROM mem_embedding"),
                "discoveries": n("SELECT COUNT(*) FROM mem_discovery WHERE status IN ('candidate','confirmed','weakening')"), "predictions": n("SELECT COUNT(*) FROM mem_prediction"),
                "questions_open": n("SELECT COUNT(*) FROM mem_question WHERE status = 'open'"), "dossier_versions": n("SELECT COUNT(*) FROM mem_dossier")}


def audit_log(limit: int = 50) -> list[dict]:
    with _c() as c:
        return [dict(r) for r in c.execute("SELECT * FROM mem_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
