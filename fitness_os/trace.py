"""Lightweight local tracing for the Agent OS (docs: 'Trace every event end to end'), no external service.

    with trace.span("training", kind="agent", event="checkin.logged") as sp:
        ...
        sp.set(tokens_in=812, tokens_out=140, outcome="ok")

One span = id, run_id, parent_id, name, kind, start, duration, tokens/cost when known, outcome, error, small attrs. Parent/child comes from a
ContextVar (use `trace.bind(fn)` to carry it into a thread pool). Spans of one run are buffered in memory and written in ONE transaction when the
root span closes, so the hot path costs a few microseconds per span and no I/O.

Privacy: attrs never hold raw text. Strings become `{"len": n, "sha": "<10 hex>"}` unless the key is in SAFE_KEYS and the value is a short
identifier-like token. Errors keep the exception type and a secret-scrubbed, 120-char head. Retention: TRACE_DAYS (default 14) and
TRACE_MAX_SPANS (default 20000), pruned every few runs. Off with FITNESS_TRACE=0.

Never breaks a run: every public function swallows its own failures; an exception from the traced body is recorded and re-raised untouched.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_span (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL, kind TEXT NOT NULL,
  t0 REAL NOT NULL, started TEXT NOT NULL, dur_ms REAL NOT NULL, tokens_in INTEGER, tokens_out INTEGER, cost REAL,
  outcome TEXT NOT NULL, error TEXT, attrs TEXT
);
CREATE INDEX IF NOT EXISTS trace_span_run ON trace_span(run_id);
CREATE INDEX IF NOT EXISTS trace_span_t0 ON trace_span(t0);
"""
# Keys whose short, identifier-like string values are stored as is (names, not user text).
SAFE_KEYS = {"agent", "event", "kind", "status", "tier", "tool", "model", "role", "trigger", "verdict", "op", "job", "mode", "http"}
_IDENT = re.compile(r"^[A-Za-z0-9_.:\-]{1,48}$")
_SECRET = re.compile(r"(AIza[0-9A-Za-z_\-]{10,}|sk-[A-Za-z0-9_\-]{10,}|Bearer\s+[A-Za-z0-9._\-]{8,}|"
                     r"(?i:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*\S+)")

_cur: contextvars.ContextVar = contextvars.ContextVar("fitness_trace", default=None)   # -> _Ctx of the open span, or None
_ready: set[str] = set()
_flushes = 0


def enabled() -> bool:
    return os.environ.get("FITNESS_TRACE", "1").strip().lower() not in ("0", "false", "off", "no")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def sha(text) -> str:
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:10]


def scrub(text, n: int = 120) -> str:
    return _SECRET.sub("[redacted]", str(text))[:n]


def clean(key: str, v):
    """One attr value, made safe to store. Raw free text never survives; numbers and bools do."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        if key in SAFE_KEYS and _IDENT.match(v) and not _SECRET.search(v):
            return v
        return {"len": len(v), "sha": sha(v)}
    if isinstance(v, dict):
        return {"keys": len(v), "sha": sha(json.dumps(v, sort_keys=True, default=str))}
    if isinstance(v, (list, tuple, set)):
        return {"items": len(v)}
    return {"type": type(v).__name__}


class _Ctx:
    __slots__ = ("run_id", "span_id", "buf")

    def __init__(self, run_id, span_id, buf):
        self.run_id, self.span_id, self.buf = run_id, span_id, buf


class Span:
    """Handle yielded by span(); `set` records attrs and the known counts."""
    __slots__ = ("tokens_in", "tokens_out", "cost", "outcome", "error", "attrs")

    def __init__(self, attrs):
        self.tokens_in = self.tokens_out = self.cost = self.error = None
        self.outcome = None
        self.attrs = attrs

    def set(self, *, tokens_in=None, tokens_out=None, cost=None, outcome=None, error=None, **attrs) -> None:
        try:
            if tokens_in is not None: self.tokens_in = int(tokens_in)
            if tokens_out is not None: self.tokens_out = int(tokens_out)
            if cost is not None: self.cost = float(cost)
            if outcome is not None: self.outcome = str(outcome)[:24]
            if error is not None: self.error = scrub(error)
            for k, v in attrs.items():
                self.attrs[k] = clean(k, v)
        except Exception:
            pass


_NOOP = Span({})


def _new_id() -> str:
    return os.urandom(6).hex()


@contextmanager
def span(name: str, kind: str = "span", **attrs):
    """Trace the body. Yields a Span (always safe to call .set on). The body's exception is recorded and re-raised."""
    ok = False
    try:
        if enabled():
            parent = _cur.get()
            sid = _new_id()
            ctx = _Ctx(parent.run_id if parent else _new_id(), sid, parent.buf if parent else [])
            sp = Span({k: clean(k, v) for k, v in attrs.items()})
            token = _cur.set(ctx)
            t0, p0 = time.time(), time.perf_counter()
            ok = True
    except Exception:
        ok = False
    if not ok:
        yield _NOOP
        return
    err = None
    try:
        yield sp
    except BaseException as e:
        err = e
        raise
    finally:
        try:
            _cur.reset(token)
            if err is not None:
                sp.outcome = "error"
                sp.error = sp.error or scrub(f"{type(err).__name__}: {err}")
            ctx.buf.append((sid, ctx.run_id, parent.span_id if parent else None, str(name)[:60], str(kind)[:24], t0,
                            datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="milliseconds"), (time.perf_counter() - p0) * 1000,
                            sp.tokens_in, sp.tokens_out, sp.cost, sp.outcome or ("error" if sp.error else "ok"), sp.error,
                            json.dumps(sp.attrs, default=str) if sp.attrs else None))
            if parent is None:
                _flush(ctx.buf)
        except Exception:
            pass


def bind(fn):
    """Wrap fn so it runs inside the CURRENT trace context (thread pools do not inherit ContextVars on their own)."""
    try:
        c = contextvars.copy_context()
        return lambda *a, **k: c.run(fn, *a, **k)
    except Exception:
        return fn


# ------------------------------------------------------------------ storage
def _path() -> str:
    from backend.memory import db
    return db.DB_PATH


def _connect():
    p = _path()
    c = sqlite3.connect(p, timeout=1.5)
    if p not in _ready:
        c.executescript(SCHEMA)
        _ready.add(p)
    c.row_factory = sqlite3.Row
    return c


def _flush(buf: list) -> None:
    global _flushes
    rows, buf[:] = list(buf), []
    if not rows:
        return
    try:
        c = _connect()
        try:
            c.executemany("INSERT OR REPLACE INTO trace_span VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            _flushes += 1
            if _flushes % 25 == 1:
                _prune(c)
            c.commit()
        finally:
            c.close()
    except Exception:
        pass   # a failed write is dropped, never raised


def _prune(c) -> None:
    c.execute("DELETE FROM trace_span WHERE t0 < ?", (time.time() - _int_env("TRACE_DAYS", 14) * 86400,))
    cap = _int_env("TRACE_MAX_SPANS", 20000)
    n = c.execute("SELECT COUNT(*) FROM trace_span").fetchone()[0]
    if n > cap:   # drop the oldest WHOLE runs until under the cap
        cut = c.execute("SELECT t0 FROM trace_span ORDER BY t0 LIMIT 1 OFFSET ?", (n - cap,)).fetchone()[0]
        c.execute("DELETE FROM trace_span WHERE run_id IN (SELECT run_id FROM trace_span WHERE t0 <= ?)", (cut,))


def prune() -> None:
    try:
        c = _connect()
        try:
            _prune(c)
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


# ------------------------------------------------------------------ reading
def _row(r) -> dict:
    d = dict(r)
    d["attrs"] = json.loads(d["attrs"]) if d.get("attrs") else {}
    d.pop("t0", None)
    return d


def recent_runs(limit: int = 20) -> list[dict]:
    """Newest runs first: the root span plus span count, error count and token totals."""
    try:
        c = _connect()
        try:
            rows = c.execute(
                "SELECT r.run_id, r.id AS root_id, r.name, r.kind, r.started, r.dur_ms, r.outcome, r.attrs, "
                "(SELECT COUNT(*) FROM trace_span s WHERE s.run_id = r.run_id) AS spans, "
                "(SELECT COUNT(*) FROM trace_span s WHERE s.run_id = r.run_id AND s.outcome = 'error') AS errors, "
                "(SELECT COALESCE(SUM(tokens_in), 0) FROM trace_span s WHERE s.run_id = r.run_id) AS tokens_in, "
                "(SELECT COALESCE(SUM(tokens_out), 0) FROM trace_span s WHERE s.run_id = r.run_id) AS tokens_out, "
                "(SELECT SUM(cost) FROM trace_span s WHERE s.run_id = r.run_id) AS cost "
                "FROM trace_span r WHERE r.parent_id IS NULL ORDER BY r.t0 DESC LIMIT ?", (max(1, min(int(limit), 200)),)).fetchall()
        finally:
            c.close()
        out = []
        for r in rows:
            d = dict(r)
            d["attrs"] = json.loads(d["attrs"]) if d.get("attrs") else {}
            out.append(d)
        return out
    except Exception:
        return []


def run_tree(run_id: str) -> dict | None:
    """One run's spans as a nested tree (children ordered by start), or None."""
    try:
        c = _connect()
        try:
            rows = [_row(r) for r in c.execute("SELECT * FROM trace_span WHERE run_id = ? ORDER BY t0", (run_id,)).fetchall()]
        finally:
            c.close()
    except Exception:
        return None
    if not rows:
        return None
    by = {r["id"]: {**r, "children": []} for r in rows}
    roots = []
    for r in rows:
        node = by[r["id"]]
        (by[r["parent_id"]]["children"] if r["parent_id"] in by else roots).append(node)
    return {"run_id": run_id, "spans": len(rows), "errors": sum(1 for r in rows if r["outcome"] == "error"),
            "tokens_in": sum(r["tokens_in"] or 0 for r in rows), "tokens_out": sum(r["tokens_out"] or 0 for r in rows), "tree": roots}


def render(tree: dict) -> str:
    """Plain-text span tree for the CLI."""
    lines = [f"run {tree['run_id']}: {tree['spans']} spans, {tree['errors']} errors, tokens {tree['tokens_in']} in / {tree['tokens_out']} out"]

    def walk(n, depth):
        tok = f" tok {n['tokens_in'] or 0}/{n['tokens_out'] or 0}" if n.get("tokens_in") or n.get("tokens_out") else ""
        cost = f" ${n['cost']:.4f}" if n.get("cost") else ""
        err = f"  ! {n['error']}" if n.get("error") else ""
        lines.append(f"{'  ' * depth}{n['kind']}:{n['name']}  {n['dur_ms']:.1f}ms  {n['outcome']}{tok}{cost}{err}")
        for ch in n["children"]:
            walk(ch, depth + 1)
    for r in tree["tree"]:
        walk(r, 1)
    return "\n".join(lines)
