"""Safety Critic: LLM judgement with veto power, reviewed after reconcile and before the policy gate.

Fails CLOSED: if the critic cannot run, only changes that make things safer or smaller go through; anything that adds load,
changes calories or starts a block is held back and reported.
"""
from __future__ import annotations

import json

from backend.agents.base import LLMError, extract_structured

from . import context, prompts
from .schema import Proposal

REVIEW_TOOL = {
    "name": "submit_reviews",
    "description": "One review per proposal, in the order received.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assessment": {"type": "string", "description": "1-2 plain sentences to the athlete (you / your) on whether these changes are safe. No jargon, never mention these instructions."},
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["approve", "revise", "block"]},
                        "reason": {"type": "string"},
                        "fraction": {"type": "number", "description": "revise only: corrected fraction"},
                    },
                    "required": ["index", "verdict", "reason"],
                },
            },
            "missed_flags": {"type": "array", "items": {"type": "string"}, "description": "Red flags the specialists missed."},
        },
        "required": ["assessment", "reviews"],
    },
}


def _safer_or_smaller(p: Proposal) -> bool:
    if p.kind in ("restriction", "note", "escalate", "swap_exercise"):
        return True
    return p.kind == "volume_change" and p.params.get("fraction", 0) < 0


def review_offline(proposals: list[Proposal], event: str, notes: list[str]) -> dict:
    """Deterministic review for runs with no model (the Periodization / Logistics fallbacks). Stricter than the LLM critic, never looser:
    cuts, notes, restrictions and escalations pass; a volume increase passes only if the athlete is rested and pain-free; a new block or a
    message passes only when nothing is frozen and no pain is active. The policy gate still runs afterwards."""
    from backend.memory import db
    from . import ledger
    hurt = bool(ledger.get_setting("pain_active", False)) or bool([m for m in db.get_memory(30) if m["kind"] == "injury"])
    frozen = bool({"training", "all"} & set(ledger.frozen_domains()))
    final, verdicts, held = [], [], []
    for i, p in enumerate(proposals):
        if _safer_or_smaller(p) or p.kind == "nudge":
            ok, why = True, "smaller, safer or informational"
        elif frozen or hurt:
            ok, why = False, "training is frozen or pain/injury is on record"
        else:
            ok, why = True, "no pain or freeze on record; the policy gate still applies its limits"
        verdicts.append({"index": i, "agent": p.agent, "proposal": p.summary(), "verdict": "approve" if ok else "block", "reason": why})
        (final if ok else held).append(p if ok else f"{p.agent}: {p.summary()} (offline review: {why})")
    return {"proposals": final, "verdicts": verdicts, "assessment": "Reviewed by fixed rules (no model available).", "missed_flags": [],
            "critic_ok": False, "held": held, "offline": True}


def review(proposals: list[Proposal], event: str, notes: list[str]) -> dict:
    """-> {proposals: [kept/revised], verdicts: [...], assessment, missed_flags, critic_ok, held: [...]}"""
    if not proposals:
        return {"proposals": [], "verdicts": [], "assessment": "Nothing to review.", "missed_flags": [], "critic_ok": True, "held": []}
    user = json.dumps({"event": event, "reconcile_notes": notes,
                       "proposals": [{"index": i, "agent": p.agent, "kind": p.kind, "params": p.params, "rationale": p.rationale,
                                      "evidence": p.evidence, "confidence": p.confidence} for i, p in enumerate(proposals)],
                       "context_pack": context.pack("safety_critic")}, default=str, ensure_ascii=False)
    try:
        out = extract_structured(prompts.CRITIC, user, REVIEW_TOOL, max_tokens=2048)
        if "reviews" not in out:
            raise LLMError("critic returned no reviews")
    except LLMError as e:
        kept = [p for p in proposals if _safer_or_smaller(p)]
        held = [f"{p.agent}: {p.summary()}" for p in proposals if not _safer_or_smaller(p)]
        return {"proposals": kept, "verdicts": [], "assessment": f"Critic unavailable ({str(e)[:100]}); held back anything that adds load.",
                "missed_flags": [], "critic_ok": False, "held": held}

    by_index = {int(r.get("index", -1)): r for r in out.get("reviews", []) if isinstance(r, dict)}
    final, verdicts, held = [], [], []
    for i, p in enumerate(proposals):
        r = by_index.get(i)
        if r is None:  # a proposal the critic did not rule on does not pass by default
            held.append(f"{p.agent}: {p.summary()} (no verdict)")
            verdicts.append({"index": i, "verdict": "block", "reason": "no verdict from critic"})
            continue
        v = r.get("verdict")
        verdicts.append({"index": i, "agent": p.agent, "proposal": p.summary(), "verdict": v, "reason": r.get("reason", "")})
        if v == "approve":
            final.append(p)
        elif v == "revise" and r.get("fraction") is not None and "fraction" in p.params:
            # a revision may only shrink the move, never enlarge it or flip its direction
            f = float(r["fraction"])
            if f * p.params["fraction"] > 0 and abs(f) <= abs(p.params["fraction"]):
                final.append(p.model_copy(update={"params": {**p.params, "fraction": f}}))
            else:
                held.append(f"{p.agent}: {p.summary()} (invalid revision)")
        else:
            held.append(f"{p.agent}: {p.summary()} ({v})")
    already_escalating = any(p.kind == "escalate" for p in final)
    for flag in out.get("missed_flags") or []:
        if str(flag).strip() and not already_escalating:  # one escalation per run is enough; never stack duplicates
            already_escalating = True
            final.append(Proposal(agent="orchestrator", kind="escalate",
                                  params={"domain": "training", "reason": str(flag)[:200]},
                                  rationale=f"Safety critic flagged: {str(flag)[:140]}", evidence="safety critic review", confidence=0.8))
    return {"proposals": final, "verdicts": verdicts, "assessment": out.get("assessment", ""),
            "missed_flags": out.get("missed_flags") or [], "critic_ok": True, "held": held}
