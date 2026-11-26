"""LLM judge for 'does it sound like a coach?' on TRANSCRIPTS (never audio), through the project's own Gemini fallback chain (backend/agents/base.py).
Frugal on purpose (free tier): a small sample, results cached on disk by hash, every request counted against a daily budget in .cache/budget.json.
Keys are read from deploy/shared.env into this process only and never printed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(__file__).resolve().parent / ".cache"
RUBRIC_VERSION = "v1"
DAILY_BUDGET = int(os.environ.get("EVAL_DAILY_REQUEST_BUDGET", "400"))      # shared by the harness and the judge; the free quota is not published (about 500/day is a guess)

DIMS = ["fits_moment", "brevity", "no_repetition", "mirrors_language", "warm_not_gushing", "honest"]
SYSTEM = """You judge a gym coach's spoken reply in a voice conversation. The coach is talking to an athlete who is mid-workout, by voice.
Score each dimension 1-5 (5 best). Be strict and consistent. Reply with JSON only, no prose:
{"fits_moment": n, "brevity": n, "no_repetition": n, "mirrors_language": n, "warm_not_gushing": n, "honest": n, "note": "max 12 words"}
- fits_moment: the reply answers what was just said and suits the moment (between sets, a question, a correction). Off-topic or generic = low.
- brevity: short enough to listen to while exercising. 1-2 short sentences = 5; a paragraph = 1. A reply to chatter/noise should be minimal.
- no_repetition: does not repeat what it said earlier in the conversation, no stock phrases stacked.
- mirrors_language: replies in the language the athlete used (English -> English, Hinglish in Roman letters -> Hinglish, never Devanagari to a Roman-letter speaker).
- warm_not_gushing: warm and direct like a real coach, not robotic, not over-the-top praise.
- honest: no invented numbers or facts; says it does not know instead of guessing; does not claim actions that were not taken."""


def _load_env() -> None:
    p = ROOT / "deploy" / "shared.env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z0-9_]+)=(.*)$", line)
            if m and m.group(2).strip():
                os.environ.setdefault(m.group(1), m.group(2).strip())


# ---- request budget -------------------------------------------------------------------------------------------------
def budget_used() -> int:
    try:
        d = json.loads((CACHE / "budget.json").read_text())
        return d["count"] if d.get("date") == date.today().isoformat() else 0
    except (OSError, ValueError, KeyError):
        return 0


def budget_add(n: int) -> None:
    CACHE.mkdir(exist_ok=True)
    (CACHE / "budget.json").write_text(json.dumps({"date": date.today().isoformat(), "count": budget_used() + n}))


def budget_ok(extra: int) -> bool:
    return budget_used() + extra <= DAILY_BUDGET


def _cache_get(k: str):
    try:
        return json.loads((CACHE / "judge.json").read_text()).get(k)
    except (OSError, ValueError):
        return None


def _cache_put(k: str, v: dict) -> None:
    CACHE.mkdir(exist_ok=True)
    p = CACHE / "judge.json"
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {}
    d[k] = v
    p.write_text(json.dumps(d, ensure_ascii=False))


def _parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return {k: int(d[k]) for k in DIMS} | {"note": str(d.get("note", ""))[:120]}
    except (ValueError, KeyError, TypeError):
        return None


def pick_sample(recs: list[dict], n: int) -> list[dict]:
    """Evenly spread turns that have a spoken reply (skips errors and empty replies); one per scenario first so the sample covers different situations."""
    pool = [r for r in recs if r.get("reply") and not r.get("error")]
    seen: set[str] = set()
    first = [r for r in pool if not (r["scenario"] in seen or seen.add(r["scenario"]))]
    pool = first if len(first) >= n else first + [r for r in pool if r not in first]
    if len(pool) <= n:
        return pool
    step = len(pool) / n
    return [pool[int(i * step)] for i in range(n)]


def judge(recs: list[dict], n: int = 12) -> dict | None:
    """-> {'rows': [...], 'means': {...}, 'markdown': str} or None when n == 0 / no key / no budget."""
    if n <= 0:
        return None
    _load_env()
    sys.path.insert(0, str(ROOT))
    try:
        from backend.agents import base
    except Exception as e:                                     # backend deps missing: say so, do not crash the run
        return {"rows": [], "means": {}, "markdown": f"_judge skipped: could not import backend.agents.base ({type(e).__name__})_"}
    if not base.have_key("fast"):
        return {"rows": [], "means": {}, "markdown": "_judge skipped: no LLM key configured_"}
    sample = pick_sample(recs, n)
    rows, spent = [], 0
    for r in sample:
        k = hashlib.sha1(f"{RUBRIC_VERSION}|{r['say']}|{r['reply']}|{r.get('lang')}".encode("utf-8")).hexdigest()
        res = _cache_get(k)
        if res is None:
            if not budget_ok(spent + 1):
                rows.append({"scenario": r["scenario"], "say": r["say"], "reply": r["reply"], "scores": None, "note": "budget exhausted"})
                continue
            user = f"Athlete language: {r.get('lang')}\nAthlete said: {r['say']}\nCoach replied: {r['reply']}\nCoach intent label: {r.get('intent')}"
            try:
                msg = base._post("fast", {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], "max_tokens": 400, "temperature": 0},
                                 timeout=20.0, retries=0)["choices"][0]["message"]
                spent += 1
                res = _parse(msg.get("content") or "")
            except Exception as e:
                rows.append({"scenario": r["scenario"], "say": r["say"], "reply": r["reply"], "scores": None, "note": f"judge call failed: {type(e).__name__}"})
                continue
            if res:
                _cache_put(k, res)
        rows.append({"scenario": r["scenario"], "say": r["say"], "reply": r["reply"], "scores": res, "note": (res or {}).get("note", "unparsable")})
    if spent:
        budget_add(spent)
    scored = [x["scores"] for x in rows if x["scores"]]
    means = {d: round(sum(s[d] for s in scored) / len(scored), 2) for d in DIMS} if scored else {}
    overall = round(sum(means.values()) / len(means), 2) if means else None
    low = [x for x in rows if x["scores"] and min(x["scores"][d] for d in DIMS) < 3]
    md = [f"Sample: {len(rows)} turns ({len(scored)} scored, {spent} new judge requests, rest from cache). Target: mean >= 4.0 and no turn under 3.", ""]
    if means:
        md += ["| " + " | ".join(DIMS) + " | overall |", "|" + "---|" * (len(DIMS) + 1), "| " + " | ".join(str(means[d]) for d in DIMS) + f" | **{overall}** |", ""]
        md += [f"Turns with a score under 3: {len(low)}"] + [f"- said {x['say']!r} -> {x['reply'][:120]!r} ({x['note']})" for x in low[:8]]
    else:
        md.append("_no scores (no key, budget exhausted, or unparsable answers)_")
    md += ["", "_Judge = Gemini through the project's fallback chain, temperature 0, transcripts only. It is a smoke detector: spot-check 20 turns by ear each release._"]
    return {"rows": rows, "means": means, "overall": overall, "markdown": "\n".join(md)}
