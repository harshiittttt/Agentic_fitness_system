"""Roundtable call mode (docs section 09): talk to the whole team at once, run by a Moderator.

    1 Triage      the Moderator invites only the 2-3 relevant specialists (or whoever you address by name)      [code]
    2 Answers     invited agents answer concurrently, short, from their own domain and the shared state         [LLM]
    3 Debate      if they disagree (one wants more, another less), up to 2 evidence-based rebuttal rounds       [LLM]
    4 Verdict     the Moderator rules by the deterministic rule (safety first) and gives ONE recommendation      [code + LLM voice]
    5 Commit      plan changes still go through the Critic and the policy tiers; the call is summarised to memory

Care first: disordered-eating / crisis language and red-flag symptoms never reach the agents; the athlete gets the serious reply.
The Moderator's wording is only accepted if it adds no number that is not in the team's own words (else a plain deterministic verdict).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from backend.agents.base import LLMError, have_key, quick_text
from backend.memory import db

from . import clock, control, critic, memory, runtime, watchdogs
from .schema import Proposal

MAX_SPEAKERS = 3
MAX_ROUNDS = 2          # rebuttal rounds after the first answers ("one or two rounds, then the Moderator rules")
SPECIALISTS = ("training", "nutrition", "recovery", "progress", "sleep", "injury", "adherence", "periodization", "logistics")
KEYWORDS: dict[str, tuple[str, ...]] = {
    "training": ("squat", "bench", "deadlift", "press", "row", "program", "split", "workout", "exercise", "volume", "sets", "reps", "lift", "swap",
                 "train", "gym", "ppl", "push", "pull", "legs", "deload"),
    "nutrition": ("calorie", "kcal", "protein", "carb", "fat", "diet", "eat", "food", "meal", "macro", "cut", "bulk", "weight loss", "fasting", "supplement"),
    "recovery": ("tired", "sore", "soreness", "fatigue", "recover", "rest", "readiness", "burnout", "energy", "overtrain"),
    "sleep": ("sleep", "insomnia", "bedtime", "stress", "stressed", "nap", "wake"),
    "injury": ("pain", "hurt", "injur", "knee", "shoulder", "back", "elbow", "wrist", "hip", "ankle", "strain", "sprain", "rehab", "tweak"),
    "progress": ("plateau", "stall", "stuck", "progress", "stronger", "trend", "forecast", "pr ", "e1rm", "gaining"),
    "periodization": ("deload", "taper", "peak", "block", "mesocycle", "periodi", "competition", "meet", "contest", "race", "phase", "cycle"),
    "logistics": ("travel", "trip", "flight", "hotel", "busy", "schedule", "no time", "short on time", "minutes", "equipment", "gym access", "reschedule", "move my"),
    "adherence": ("motivat", "skip", "missed", "consisten", "lazy", "habit", "routine", "streak", "can't stick", "discipline"),
}
DEFAULTS = ("training", "recovery", "nutrition")
LABEL = {"training": "Training", "nutrition": "Nutrition", "recovery": "Recovery", "progress": "Progress", "sleep": "Sleep & Stress",
         "injury": "Injury & Rehab", "adherence": "Adherence", "periodization": "Periodization", "logistics": "Logistics", "moderator": "Moderator"}

ANSWER_RULES = (
    "ROUNDTABLE MODE: the athlete asked the whole team one question. Answer it from YOUR domain only, in at most two short sentences, "
    "in `reasoning`, speaking to them directly. Use only numbers from your context or tools. Propose a change only if the data clearly "
    "supports it; otherwise return no proposals. Do not answer for other specialists. Name days and exercises in words (for example 'leg day'), "
    "never as an index or number like 'day 0'."
)
REBUTTAL_RULES = (
    "ROUNDTABLE REBUTTAL: another specialist disagrees with you (see `opposing_view`). Reconsider with evidence. Keep or change your "
    "position in at most two sentences in `reasoning`; if the other view is safer or better supported, concede and drop or soften your proposal."
)
MODERATOR = (
    "You are the MODERATOR of a coaching team. Give the athlete ONE clear recommendation in 2-4 short sentences, plain words, speaking to "
    "'you'. Base it only on the team's answers and the ruling below. Do not add any number that is not in them. Do not mention the debate "
    "mechanics. Say which changes (if any) are being proposed and that they still need the usual safety checks."
)


def triage(question: str, addressed: list[str] | None = None) -> list[str]:
    q = question.lower()
    named = [a for a in (addressed or []) if a in SPECIALISTS]
    if not named:   # "@nutrition ...", "ask training ...", "hey recovery, ..."
        named = [a for a in SPECIALISTS if re.search(rf"(@|\bask\b\s+(the\s+)?|\bhey\b\s+){a}\b", q)]
    if named:
        return named[:MAX_SPEAKERS]
    scores = {a: sum(1 for k in kws if k in q) for a, kws in KEYWORDS.items()}
    ranked = [a for a, s in sorted(scores.items(), key=lambda kv: (-kv[1], list(KEYWORDS).index(kv[0]))) if s > 0][:MAX_SPEAKERS]
    for d in DEFAULTS:           # an ambiguous question still gets at least two voices
        if len(ranked) >= 2:
            break
        if d not in ranked:
            ranked.append(d)
    return ranked


def _sign(p: Proposal) -> int:
    f = p.params.get("fraction", 0) or 0
    return 1 if f > 0 else -1 if f < 0 else 0


def conflicts(runs: dict[str, dict]) -> list[tuple[str, str, str]]:
    """Disagreements the agents' own proposals reveal: opposite directions on the same thing, or more load against a safety proposal."""
    out = []
    names = list(runs)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for pa in runs[a]["proposals"]:
                for pb in runs[b]["proposals"]:
                    if pa.kind == pb.kind and pa.kind in ("volume_change", "calorie_change") and _sign(pa) * _sign(pb) < 0:
                        out.append((a, b, f"{LABEL[a]} says {pa.summary()}, {LABEL[b]} says {pb.summary()}"))
                    else:
                        up = pa if (pa.kind == "volume_change" and _sign(pa) > 0) else pb if (pb.kind == "volume_change" and _sign(pb) > 0) else None
                        safe = pb if up is pa else pa
                        if up is not None and safe.kind in ("restriction", "escalate") and {up.agent, safe.agent} == {a, b}:
                            out.append((up.agent, safe.agent, f"{LABEL[up.agent]} wants {up.summary()} while {LABEL[safe.agent]} flags {safe.summary()}"))
    seen, uniq = set(), []
    for a, b, why in out:
        if (a, b) not in seen:
            seen.add((a, b))
            uniq.append((a, b, why))
    return uniq


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _verdict_text(question: str, finals: list[str], kept: list[Proposal], notes: list[str]) -> str:
    """`finals` = each invited specialist's last word. Plain deterministic verdict first; the Moderator may only re-voice it."""
    ruling = [p.summary() for p in kept]
    said = " ".join(f for f in finals if f)
    plain = said[:400] or "The team has no change to suggest."
    plain += (" Proposed: " + "; ".join(ruling) + ". These still go through the usual safety checks." if ruling else "")
    if not have_key():
        return plain
    source = json.dumps({"q": question, "said": said, "ruling": ruling, "notes": notes}, ensure_ascii=False)
    try:
        out = quick_text(MODERATOR, source, max_tokens=220, timeout=12.0)
    except (LLMError, Exception):
        return plain
    return out if out and _numbers(out) <= _numbers(source + question) else plain


def run(question: str, addressed: list[str] | None = None, *, commit: bool = True) -> dict:
    question = (question or "").strip()[:500]
    if not question:
        raise ValueError("Ask the team something.")
    from backend.coach import care
    hindi = (db.get_profile() or {}).get("coach_persona") in ("haryanvi", "roast")
    cared = care.handle(question, hindi)
    if cared:
        return {"question": question, "speakers": [], "turns": [], "conflict": False, "verdict": cared, "results": [], "care": True, "llm": have_key()}
    rf = watchdogs.red_flag(question)
    if rf:
        r = control.submit(Proposal(agent="watchdog", kind="escalate", params={"domain": "training", "reason": rf},
                                    rationale=f"Red-flag symptom ('{rf}'). Training advice is paused; please see a medical professional.", evidence=question[:120], confidence=1.0),
                           trigger="roundtable")
        return {"question": question, "speakers": ["injury"], "turns": [], "conflict": False, "care": True, "llm": have_key(), "results": [r],
                "verdict": "That symptom needs a medical professional, not a coaching opinion. I've paused training changes until you've been seen."}
    speakers = triage(question, addressed)
    if not have_key():
        return {"question": question, "speakers": speakers, "turns": [], "conflict": False, "results": [], "llm": False,
                "verdict": "The team needs an LLM key (GEMINI_API_KEY) to discuss this. Your plan and logging still work."}

    payload = {"question": question, "mode": "roundtable"}
    with ThreadPoolExecutor(max_workers=len(speakers)) as ex:
        first = list(ex.map(lambda a: runtime.run_agent(a, "chat.message", payload, ANSWER_RULES), speakers))
    runs = {r["agent"]: r for r in first}
    turns = [{"agent": a, "round": 1, "text": runs[a]["reasoning"].strip(), "proposals": [p.summary() for p in runs[a]["proposals"]]} for a in runs]

    rounds, disputes = 0, conflicts(runs)
    while disputes and rounds < MAX_ROUNDS:
        rounds += 1
        involved = sorted({x for a, b, _ in disputes for x in (a, b)})

        def rebut(agent: str) -> dict:
            others = [f"{LABEL[o]}: {runs[o]['reasoning']} | proposes: {[p.summary() for p in runs[o]['proposals']]}" for o in involved if o != agent]
            return runtime.run_agent(agent, "chat.message", {**payload, "opposing_view": others, "round": rounds + 1}, REBUTTAL_RULES)

        with ThreadPoolExecutor(max_workers=len(involved)) as ex:
            for r in ex.map(rebut, involved):
                runs[r["agent"]] = r
                turns.append({"agent": r["agent"], "round": rounds + 1, "text": r["reasoning"].strip(), "proposals": [p.summary() for p in r["proposals"]]})
        disputes = conflicts(runs)

    proposals = [p for r in runs.values() for p in r["proposals"]]
    kept, notes = control.reconcile(proposals)                      # the Moderator's tiebreak: safety first, then authority
    if disputes:
        notes.append("The team did not fully agree after the rebuttal rounds; the safer proposal was kept: " + "; ".join(w for _, _, w in disputes))
    verdict = _verdict_text(question, [r["reasoning"].strip() for r in runs.values()], kept, notes)
    results = []
    if commit and kept:
        rv = critic.review(kept, "roundtable", notes)
        results = [{"proposal": p.summary(), "agent": p.agent, **control.submit(p, trigger="roundtable")} for p in rv["proposals"]]
    if commit:
        try:
            memory.add("episodic", f"Roundtable on {clock.iso()[:10]}: you asked '{question[:80]}'. Team verdict: {verdict[:220]}", 0.5, min_sim=0.98)
        except Exception:
            pass
    return {"question": question, "speakers": speakers, "turns": turns, "conflict": bool(disputes), "rebuttal_rounds": rounds,
            "notes": notes, "verdict": verdict, "results": results, "llm": True}
