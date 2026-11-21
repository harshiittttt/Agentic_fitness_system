"""Earned-trust autonomy (docs section 08): "Autonomy is earned per action, not granted per agent."

    * Tier thresholds LOOSEN as the athlete accepts a kind of change without override (notify-first -> auto).
    * They TIGHTEN after reverts, vetoes or rejections (2 recent: auto -> notify-first; 3 recent: ask first).
    * New action types can start one tier stricter (setting `strict_new_types`, off by default so the first overnight trim is not delayed).

Trust is tracked per ACTION TYPE ("volume:cut", "volume:increase", "calories:cut", "swap"...), from the decision ledger.
It is a bounded adjustment applied by policy.check_action AFTER the deterministic limits: it can never loosen `approve` or
`escalate`, never overrides the hands-on dial, and never touches a limit that fired (caps, floors, scope, frozen domains).
"""
from __future__ import annotations

from . import clock, ledger

WINDOW_DAYS = 90
RECENT_DAYS = 14
EARN_MIN_ACCEPTED = 5          # accepted changes of this type before notify-first can become auto
EARN_MIN_RATE = 0.9            # and at least this share of all decisions of the type accepted
TIGHTEN_RECENT_NEGATIVES = 2   # reverts/vetoes/rejections in RECENT_DAYS that pull auto -> notify-first
HARD_TIGHTEN_NEGATIVES = 3     # ...and notify-first -> approve
NEGATIVE = ("reverted", "vetoed", "rejected")
POSITIVE = ("applied",)
ORDER = ["auto", "notify_first", "approve"]
TRUSTED_KINDS = ("volume_change", "calorie_change", "swap_exercise")   # the kinds whose tier is not safe-by-construction


def action_type(kind: str, params: dict | None) -> str:
    f = float((params or {}).get("fraction", 0) or 0)
    if kind == "volume_change":
        return "volume:cut" if f < 0 else "volume:increase"
    if kind == "calorie_change":
        return "calories:cut" if f < 0 else "calories:increase"
    return {"swap_exercise": "swap", "new_block": "block"}.get(kind, kind)


def _type_of(d: dict) -> str:
    return action_type(d["kind"], (d.get("proposal") or {}).get("params"))


def stats(atype: str) -> dict:
    """Outcomes for one action type from agent-made decisions (the athlete's own changes are not trust evidence)."""
    pos = neg = recent_neg = total = 0
    for d in ledger.recent(400):
        if d["tier"] == "user" or d["agent"] in ("user", "watchdog") or _type_of(d) != atype:
            continue
        age = clock.age_days(d["ts"])
        if age > WINDOW_DAYS:
            continue
        if d["status"] in POSITIVE:
            pos += 1
        elif d["status"] in NEGATIVE:
            neg += 1
            recent_neg += age <= RECENT_DAYS
        else:
            continue
        total += 1
    return {"type": atype, "accepted": pos, "pushed_back": neg, "recent_pushed_back": recent_neg, "decisions": total,
            "accept_rate": round(pos / total, 2) if total else None}


def level(st: dict) -> str:
    """'earned' | 'neutral' | 'tightened' | 'strict'"""
    if st["recent_pushed_back"] >= HARD_TIGHTEN_NEGATIVES:
        return "strict"
    if st["recent_pushed_back"] >= TIGHTEN_RECENT_NEGATIVES:
        return "tightened"
    if st["accepted"] >= EARN_MIN_ACCEPTED and (st["accept_rate"] or 0) >= EARN_MIN_RATE and st["recent_pushed_back"] == 0:
        return "earned"
    return "neutral"


def adjust(kind: str, params: dict, tier: str, fired: list[str]) -> tuple[str, str]:
    """-> (tier, note). Called by policy.check_action with the tier the deterministic rules and the dial produced."""
    if kind not in TRUSTED_KINDS or tier not in ORDER:
        return tier, ""
    if fired:   # a hard limit shaped this action: trust does not argue with limits
        return tier, ""
    at = action_type(kind, params)
    st = stats(at)
    lv = level(st)
    i = ORDER.index(tier)
    note = ""
    if lv == "strict":
        tier2 = ORDER[min(i + 2, 2)]      # repeated pushback: ask first (auto -> approve, notify-first -> approve)
        note = f"Stricter: you pushed back on {st['recent_pushed_back']} recent {at.replace(':', ' ')} changes."
    elif lv == "tightened" and tier == "auto":
        tier2, note = "notify_first", f"Stricter: you reverted or stopped {st['recent_pushed_back']} recent {at.replace(':', ' ')} changes."
    elif lv == "earned" and tier == "notify_first" and ledger.dial() != "hands_on":
        tier2, note = "auto", f"Trusted: you accepted {st['accepted']} of {st['decisions']} past {at.replace(':', ' ')} changes."
    elif tier == "auto" and ledger.get_setting("strict_new_types", False) and st["decisions"] < 3 and kind in ("volume_change", "calorie_change"):
        tier2, note = "notify_first", f"New kind of change for you ({at.replace(':', ' ')}): one step stricter until it has a track record."
    else:
        tier2 = tier
    return tier2, note


def report() -> list[dict]:
    out = []
    for at in ("volume:cut", "volume:increase", "calories:cut", "calories:increase", "swap"):
        st = stats(at)
        out.append({**st, "level": level(st)})
    return out
