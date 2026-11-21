"""One-command smoke test for everything that needs an LLM API key (Gemini by default).

    python -m backend.check_ai

Runs against a throwaway database. Prints PASS/FAIL per check plus what the model actually said,
so you can judge the wording too, not just whether it ran.
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

from backend.agents.base import MODEL, FAST_MODEL, have_key
from backend.memory import db

results: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        try:
            ok, detail = fn()
        except Exception as e:  # report, don't crash: one broken path shouldn't hide the others
            ok, detail = False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}"
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}\n       {detail}\n")
        return fn
    return deco


def main() -> int:
    if not have_key():
        print("No LLM key found.\n  1. Copy .env.example to .env\n  2. Put your key after GEMINI_API_KEY=\n  3. Run this again.")
        return 2

    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "check.db")
    from backend.agents import orchestrator as orch
    from backend.coach import live
    from backend.tools.demo import PROFILE
    from backend.tools.meal_log import log_meal
    from backend.tools.voice_parser import parse_voice_log

    print(f"Models: main={MODEL}  fast={FAST_MODEL}\n")
    orch.onboard({**PROFILE, "name": "Alex", "coach_persona": "hype"})
    db.add_checkin(sleep_h=7.5, soreness=2, energy=4)

    @check("1. Key works and the fast model answers")
    def _():
        from backend.agents.base import quick_text
        out = quick_text("Reply with exactly one word.", "Say ready.", max_tokens=10)
        return bool(out), f"model said: {out!r}"

    @check("2. Voice parsing handles natural phrasing (words, not digits)")
    def _():
        r = parse_voice_log("did squats, one oh five for five, then a hundred and ten for three, that last one was a grinder")
        sets = r.get("sets", [])
        ok = len(sets) >= 2 and sets[0].get("weight") == 105 and sets[1].get("weight") == 110 and (sets[-1].get("rpe") or 0) >= 9
        return ok, f"parsed: {sets}"

    @check("3. Voice parsing flags pain and does not invent sets")
    def _():
        r = parse_voice_log("just stretching, my left knee feels sharp when I bend it")
        return not r.get("sets") and bool(r.get("safety_note")), f"parsed: {r}"

    @check("4. Unknown food gets an estimate (flagged as estimated)")
    def _():
        r = log_meal("a bowl of quinoa upma and a glass of buttermilk")
        est = [i for i in r["items"] if i.get("estimated")]
        return bool(est) and all(i["kcal"] > 0 for i in est), f"items: {[(i['name'], i['kcal'], i.get('estimated')) for i in r['items']]}  unknown: {r['unknown']}"

    @check("5. Coach polish keeps every number intact")
    def _():
        src = "Nice work, 72.5 for 6. Next set, try 75 for 6. Take two minutes."
        out = live.polish(src, "mentor", "Alex")
        return live.numbers_preserved(src, out), f"in:  {src}\n       out: {out}  ({'rewritten' if out != src else 'kept original'})"

    @check("6. Live coach: open conversation sounds human and stays short")
    def _():
        r = live.handle("hey, is it normal that my grip gives out before my back on rows?")
        words = len(r["reply"].split())
        bad = any(t in r["reply"] for t in ("**", "# ", "- ", "As an AI"))
        return r["intent"] == "chat" and 5 <= words <= 80 and not bad, f"({r['intent']}, {words} words) {r['reply']}"

    @check("7. Live coach: refuses to invent numbers it does not have")
    def _():
        r = live.handle("what did I deadlift last month?")
        return r["intent"] == "chat" and bool(r["reply"]), f"{r['reply']}\n       (read this one yourself: it should say it has no deadlift history, not make up a weight)"

    @check("8. Live coach: pain in free speech is handled safely")
    def _():
        r = live.handle("my shoulder feels sharp on the way up")
        return r["safety_flag"] is not None, f"{r['reply']}"

    @check("9. Coach chat can fit the session to your time (tool use)")
    def _():
        r = orch.chat("I only have 25 minutes today, can you shorten my workout?")
        ov = (db.get_plan() or {}).get("today_override")
        return bool(ov) and ov["day"].get("est_minutes", 99) <= 25, f"{r}\n       override: {ov and ov.get('reason')} ({ov and ov['day'].get('est_minutes')} min)"

    @check("10. Persona voices differ for the same moment")
    def _():
        outs = {}
        for pid in ("hype", "mentor", "straight"):
            db.save_profile({**db.get_profile(), "coach_persona": pid})
            outs[pid] = live.polish("Solid set. 80 for 8. Rest two minutes.", pid, "Alex")
        distinct = len(set(outs.values())) == 3
        return distinct, "\n       ".join(f"{k}: {v}" for k, v in outs.items())

    failed = [n for n, ok, _ in results if not ok]
    print("=" * 60)
    print(f"{len(results) - len(failed)}/{len(results)} passed" + (f"   FAILED: {', '.join(failed)}" if failed else "   all good"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
