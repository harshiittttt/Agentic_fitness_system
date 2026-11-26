"""Text-input replay harness for the Conversation Core (scenario format: docs/CONVO_CONTRACTS.md section 6).

    python -m backend.convo.replay backend/tests/convo_data/negatives.json [--live] [--limit N] [--out results.json]

Runs each scenario turn through `turn.run_turn` in-process against a throw-away database with demo data (never the real one), and checks the `expect` block plus the global
checks: every spoken number traceable, no Devanagari in speech, `done` last, no API key anywhere in the events. With --live the real model is used (free tier: keep runs small);
without it the caller must have replaced `llm.stream`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from backend.coach import live, session_flow
from backend.convo import ground, guards, llm, session, state, tools, turn
from backend.memory import db


_BASE: dict = {}


def setup_db(path: str | None = None, persona: str = "hype") -> str:
    """Point the app at a throw-away database with the demo profile and plan."""
    from backend.agents import orchestrator as orch
    from backend.tools import demo
    path = path or str(Path(tempfile.mkdtemp()) / "replay.db")
    db.DB_PATH = path
    if hasattr(db, "init"):
        try:
            db.init()
        except Exception:
            pass
    orch.onboard({**demo.PROFILE, "name": "Golu", "coach_persona": persona, "days_per_week": 4})
    _BASE["plan"] = json.loads(json.dumps(db.get_plan()))
    return path


def reset_day() -> None:
    """Between scenarios: today's sets, skips and notes are cleared, the dialogue and open questions forgotten."""
    label = session_flow.current_label()
    if _BASE.get("plan"):
        db.save_plan(json.loads(json.dumps(_BASE["plan"])))          # a swap or skip in one scenario must not leak into the next
    with db.conn() as c:
        c.execute("DELETE FROM sets WHERE session = ?", (label,))
        c.execute("DELETE FROM memory WHERE kind IN ('skip', 'injury', 'preference')")
    live._pending_clear()
    live._pending_skip.clear()
    session.clear_all()


def apply_setup(setup: dict) -> None:
    label = session_flow.current_label()
    for exn, n in (setup.get("sets_done") or {}).items():
        w = (setup.get("weights") or {}).get(exn, 60.0)
        for _ in range(int(n)):
            db.add_set(label, exn, w, 8, 8.0)
    for exn in setup.get("skipped") or []:
        db.add_memory("skip", f"{label}|{exn}")


def _all_text(events: list[dict]) -> str:
    return json.dumps(events, ensure_ascii=False, default=str)


def audit_numbers(said: str, user_texts: list[str], state_text: str, tool_facts: list[dict]) -> list[float]:
    """Numbers spoken that the athlete's words, the workout state or the tool results do not explain (after the same lenient arithmetic as the live check)."""
    allowed = ground.allowed_numbers(*user_texts, state_text, extra=tools.fact_numbers(tool_facts))
    return ground.untraceable(said, allowed)


def _state_text() -> str:
    st = live.live_context()
    rows = state.todays_sets()
    return st + " " + " ".join(f"{r['exercise']} {r['weight']} {r['reps']}" for r in rows)


def run_scenario(sc: dict, conn_id: str | None = None, key: str | None = None) -> dict:
    reset_day()
    apply_setup(sc.get("setup") or {})
    conn = conn_id or f"replay-{sc['id']}"
    users: list[str] = []
    facts_all: list[dict] = []
    out = {"id": sc["id"], "tags": sc.get("tags", []), "turns": [], "passed": True}
    for i, tn in enumerate(sc["turns"]):
        label = session_flow.current_label()
        before_sets = len([s for s in db.get_sets(limit=500) if s["session"] == label])
        req = {"text": tn["say"], "turn_id": f"{sc['id']}-{i}", "conn_id": conn, "asr": tn.get("asr"), "lang": tn.get("lang") or (sc.get("setup") or {}).get("language"),
               "set_active": tn.get("set_active"), "ambient": tn.get("ambient", False)}
        users.append(tn["say"])
        t0 = time.time()
        events = list(turn.run_turn(req, "converse"))
        wall = time.time() - t0
        tool_ev = [e for e in events if e["t"] == "tool"]
        for e in tool_ev:
            facts_all.append(e.get("facts") or {})
        d = events[-1]
        said = d.get("said", "")
        after = live.session_state() or {}
        skipped = sorted(r["name"] for r in after.get("exercises", []) if r["skipped"])
        rows = [s for s in db.get_sets(limit=500) if s["session"] == label]
        delta = len(rows) - before_sets
        checks: list[tuple[str, bool, str]] = []
        ex_ = tn.get("expect") or {}
        called_ok = [e["name"] for e in tool_ev if e["status"] == "ok"]
        called_any = [e["name"] for e in tool_ev]
        wrote = [n for n in called_ok if n in guards.WRITE]
        if "tools" in ex_:
            checks.append(("tools", all(n in called_ok for n in ex_["tools"]), f"ok-called {called_ok}"))
        if "tools_called" in ex_:
            checks.append(("tools_called", all(n in called_any for n in ex_["tools_called"]), f"called {called_any}"))
        if "tools_not" in ex_:
            checks.append(("tools_not", not any(n in called_ok for n in ex_["tools_not"]), f"ok-called {called_ok}"))
        if ex_.get("no_write_tools"):
            checks.append(("no_write_tools", not wrote, f"wrote {wrote}"))
        if "skipped" in ex_:
            checks.append(("skipped", skipped == sorted(ex_["skipped"]), f"skipped {skipped}"))
        if "sets_logged_delta" in ex_:
            checks.append(("sets_logged_delta", delta == ex_["sets_logged_delta"], f"delta {delta}"))
        if "last_set" in ex_:
            ls = ex_["last_set"]
            last = rows[0] if rows else None
            ok = bool(last) and abs(float(last["weight"] or 0) - float(ls["weight"])) < 0.01 and int(last["reps"]) == int(ls["reps"]) and (not ls.get("exercise") or last["exercise"] == ls["exercise"])
            checks.append(("last_set", ok, f"last {last and (last['exercise'], last['weight'], last['reps'])}"))
        for pat in ex_.get("say_matches", []):
            checks.append((f"say~{pat}", bool(re.search(pat, said, re.I)), said[:80]))
        for pat in ex_.get("say_not_matches", []):
            checks.append((f"say!~{pat}", not re.search(pat, said, re.I), said[:80]))
        if "expects" in ex_:
            checks.append(("expects", d.get("expects") == ex_["expects"], f"expects {d.get('expects')}"))
        if "intent" in ex_:
            checks.append(("intent", d.get("intent") == ex_["intent"], f"intent {d.get('intent')}"))
        # global checks
        bad = audit_numbers(said, users, _state_text(), facts_all)
        checks.append(("grounded", not bad, f"untraceable {bad} in {said[:80]!r}"))
        checks.append(("no_devanagari", not ground.has_devanagari(said), said[:60]))
        checks.append(("done_last", events[-1]["t"] == "done" and sum(e["t"] == "done" for e in events) == 1, "events end with one done"))
        if key:
            checks.append(("no_key_leak", key not in _all_text(events), "api key found in events"))
        ok_all = all(c[1] for c in checks)
        out["passed"] = out["passed"] and ok_all
        ms = d.get("ms") or {}
        out["turns"].append({"say": tn["say"], "said": said, "tools": [(e["name"], e["status"]) for e in tool_ev], "intent": d.get("intent"), "brain": d.get("brain"),
                             "fallback": d.get("fallback"), "model": d.get("model"), "first_say_ms": ms.get("first_say"), "first_token_ms": ms.get("first_token"),
                             "total_ms": ms.get("total"), "wall_ms": int(wall * 1000), "dropped": d.get("dropped_sentences"), "failed": [c for c in checks if not c[1]], "passed": ok_all})
    return out


def run_file(path: str, limit: int | None = None, only_tags: list[str] | None = None, out_path: str | None = None, key: str | None = None, sleep: float = 0.0) -> dict:
    scs = json.loads(Path(path).read_text(encoding="utf-8"))
    if only_tags:
        scs = [s for s in scs if set(only_tags) & set(s.get("tags", []))]
    if limit:
        scs = scs[:limit]
    results = []
    for sc in scs:
        results.append(run_scenario(sc, key=key))
        if out_path:
            Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        if sleep:
            time.sleep(sleep)
    return summarize(results)


def summarize(results: list[dict]) -> dict:
    turns = [t for r in results for t in r["turns"]]
    fs = sorted(t["first_say_ms"] for t in turns if t.get("first_say_ms"))
    ft = sorted(t["first_token_ms"] for t in turns if t.get("first_token_ms"))
    pct = lambda xs, p: xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None
    return {"scenarios": len(results), "passed": sum(r["passed"] for r in results), "turns": len(turns), "fallback_turns": sum(bool(t.get("fallback")) for t in turns),
            "first_token_ms": {"p50": pct(ft, 0.5), "p95": pct(ft, 0.95), "n": len(ft)}, "first_say_ms": {"p50": pct(fs, 0.5), "p95": pct(fs, 0.95), "n": len(fs)},
            "failures": [{"id": r["id"], "turns": [t for t in r["turns"] if not t["passed"]]} for r in results if not r["passed"]]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--tags", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--persona", default="hype")
    ap.add_argument("--no-key", action="store_true", help="pretend there is no API key: every turn must be answered by the legacy brain")
    a = ap.parse_args(argv)
    os.environ["VOICE_BRAIN"] = "converse"
    if a.no_key:
        llm.have_key = lambda: False
    setup_db(persona=a.persona)
    from backend.agents import base
    key = base._cfg("compose")["api_key"] or None
    summ = run_file(a.file, a.limit, a.tags, a.out, key, a.sleep)
    print(json.dumps({k: v for k, v in summ.items() if k != "failures"}, indent=1))
    for f in summ["failures"][:40]:
        for t in f["turns"]:
            print("FAIL", f["id"], "|", t["say"], "->", t["said"][:90], "|", t["tools"], "|", [(c[0], c[2][:70]) for c in t["failed"]])
    return 0 if not summ["failures"] else 1


if __name__ == "__main__":
    sys.exit(main())
