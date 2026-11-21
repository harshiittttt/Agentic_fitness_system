"""find_tools(query, k): BM25 over name + summary + description + path + tags, with a small synonym expansion. Stdlib only, offline.
BLOCKED tools are never returned. Curated (hand-written voice) tools outrank generated ones on equal score."""
from __future__ import annotations

import math
import re
from collections import Counter

from . import catalog, risk

SYN_GROUPS = [
    {"calendar", "agenda", "trip", "travel", "schedule", "event", "competition", "busy", "holiday", "vacation"},
    {"food", "meal", "eat", "ate", "eaten", "nutrition", "breakfast", "lunch", "dinner", "snack", "calories", "macros", "diet"},
    {"plan", "split", "workout", "training", "session", "program", "routine"},
    {"memory", "know", "remember", "knows", "fact", "facts", "dossier", "recall"},
    {"delete", "remove", "erase", "undo", "forget", "drop", "scratch"},
    {"coach", "coaches", "agents", "agent", "team", "decisions", "decision"},
    {"trace", "did", "done", "activity", "history", "audit", "ran", "run"},
    {"weight", "weigh", "bodyweight", "weighin", "scale"},
    {"sleep", "checkin", "feel", "feeling", "soreness", "energy", "readiness", "recovery", "tired"},
    {"progress", "trend", "trends", "stats", "insights", "chart", "series", "strength", "lifts"},
    {"swap", "replace", "substitute", "alternative", "alternatives", "exercise"},
    {"evidence", "research", "science", "study", "studies"},
    {"profile", "goal", "settings", "preferences", "injury", "injuries"},
    {"log", "record", "add", "track", "save", "enter"},
]
SYN = {}
for g in SYN_GROUPS:
    for w in g:
        SYN.setdefault(w, set()).update(g - {w})
# extra words per tool (how a person would ask for it); how to add one: put the tool name and the phrases here
ALIASES = {
    "agents_trace_runs": "what the coaches agents did trace history activity recent runs",
    "agents_trace_run": "trace detail of one run",
    "agents_decisions": "decisions the coaches made proposals approvals pending",
    "agents_agenda_list": "calendar trips travel events list what is coming up",
    "agents_agenda_add": "calendar add trip travel competition busy event",
    "meal": "log food meal i ate breakfast lunch dinner snack",
    "meal_delete": "delete remove a meal food entry",
    "nutrition_today": "calories protein macros left remaining eaten today",
    "nutrition_day": "calories protein macros meals of a day",
    "memory_get_dossier": "what the coach knows about me profile summary",
    "plan": "my workout plan split this week",
    "plan_swap": "swap exercise replace move workout day",
    "session_weigh_in": "weigh in body weight kilos",
}
STOP = {"the", "a", "an", "my", "me", "i", "to", "of", "for", "and", "or", "in", "on", "is", "it", "do", "what", "please", "can", "you", "that", "this", "with", "get", "show", "tell"}
_W = re.compile(r"[a-z0-9]+")


def _stem(w: str) -> str:
    for suf in ("ing", "es", "s", "ed"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in _W.findall((text or "").lower().replace("_", " ")) if w not in STOP]


def _doc_tokens(t: dict) -> list[str]:
    path = re.sub(r"\{[^}]*\}", " ", t.get("path", ""))
    return tokens(" ".join([t["name"]] * 3 + [ALIASES.get(t["name"], "")] * 2 + [t.get("summary", "")] * 2 + [t.get("description", ""), path, " ".join(t.get("tags", []))]))


def _expand(q: list[str]) -> dict[str, float]:
    w: dict[str, float] = {}
    for t in q:
        w[t] = max(w.get(t, 0), 1.0)
    raw = {s for t in q for s in SYN.get(t, ())} | {s for t in q for orig in [t] for k, v in SYN.items() if _stem(k) == orig for s in v}
    for s in raw:
        s2 = _stem(s)
        if s2 not in w:
            w[s2] = 0.35
    return w


def find_tools(query: str, k: int = 8, extra: list[dict] | None = None) -> list[dict]:
    """-> [{name, summary, risk, args, source, score}] best first. `extra` = curated specs {name, description[, risk]}."""
    k = max(1, min(int(k or 8), 20))
    docs = []
    for t in catalog.callable_tools():
        docs.append((t, _doc_tokens(t), 0))
    for e in extra or []:
        spec = {"name": e["name"], "summary": e.get("description", "")[:120], "description": e.get("description", ""), "path": "", "tags": [], "risk": e.get("risk", risk.WRITE_SAFE),
                "schema": e.get("input_schema") or {"properties": {}}, "source": "curated"}
        docs.append((spec, _doc_tokens(spec), 1))
    q = _expand(tokens(query))
    if not q or not docs:
        return []
    n = len(docs)
    avg = sum(len(d[1]) for d in docs) / n or 1.0
    df = Counter(t for _, toks, _ in docs for t in set(toks))
    scored = []
    for spec, toks, cur in docs:
        tf = Counter(toks)
        s = 0.0
        for term, w in q.items():
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += w * idf * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * len(toks) / avg))
        if s > 0:
            nm = set(tokens(spec["name"]))
            s += 0.8 * sum(w for t, w in q.items() if t in nm and df[t] <= 0.08 * n)   # a (rare) query word in the name is a strong signal
            scored.append((s + (0.01 if cur else 0.0), cur, spec))
    scored.sort(key=lambda x: (-round(x[0], 6), -x[1], x[2]["name"]))
    out = []
    for s, cur, spec in scored[:k]:
        out.append({"name": spec["name"], "summary": spec.get("summary", "")[:140], "risk": spec["risk"], "source": "curated" if cur else "generated",
                    "args": catalog.compact_args(spec) if spec.get("schema", {}).get("properties") is not None else {}, "score": round(s, 2)})
    return out
