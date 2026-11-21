"""policy.check_action: the one gate every write passes. Deterministic rules in code, not prompts."""
from __future__ import annotations

from backend.memory import db

from . import ledger
from .schema import SCOPES, Proposal, Verdict

CALORIE_FLOOR = {"m": 1500, "f": 1200}
WEEKLY_VOLUME_INCREASE_CAP = 0.10
MAX_VOLUME_VS_BASE = 1.25
MIN_VOLUME = 0.5
AUTO_TRIM_MAX = 0.30
VETO_MINUTES = 180
TIER_ORDER = ["auto", "notify_first", "approve", "escalate"]
DOMAIN = {"volume_change": "training", "swap_exercise": "training", "new_block": "training", "restriction": "training",
          "calorie_change": "nutrition", "note": "memory", "escalate": "all", "nudge": "comms"}


def _shift(tier: str, dial: str) -> str:
    """Autonomy dial. Approve/escalate never loosen; hands_on makes auto/notify stricter; autopilot loosens notify only."""
    if tier in ("approve", "escalate"):
        return tier
    if dial == "hands_on":
        return TIER_ORDER[TIER_ORDER.index(tier) + 1]
    if dial == "autopilot" and tier == "notify_first":
        return "auto"
    return tier


def _block(fired: list[str], why: str) -> Verdict:
    return Verdict(verdict="block", tier="approve", fired=fired, explain=why)


def check_action(p: Proposal) -> Verdict:
    plan = db.get_plan() or {}
    prof = db.get_profile() or {}
    params = dict(p.params)

    if p.kind not in SCOPES.get(p.agent, set()):
        return _block(["agent_scope"], f"{p.agent} is not allowed to propose {p.kind}.")
    if p.kind == "escalate":  # safety path is never blocked by the kill switch or a freeze
        return Verdict(verdict="allow", tier="escalate", fired=["escalation"], params=params,
                       explain="Safety escalation: domain frozen and referred to a qualified professional.")
    if ledger.kill_switch():
        return _block(["kill_switch"], "Autonomous writes are paused (kill switch on).")
    domain = DOMAIN[p.kind]
    frozen = ledger.frozen_domains()
    if p.kind != "note" and (domain in frozen or "all" in frozen):
        return _block(["domain_frozen"], f"The {domain} domain is frozen pending professional review.")
    if p.kind != "note" and not plan:
        return _block(["no_plan"], "No plan exists yet; onboard first.")

    fired: list[str] = []
    verdict = "allow"
    explain = ""
    tier = "auto"

    if p.kind == "note":
        return Verdict(verdict="allow", tier=_shift("auto", ledger.dial()), params=params, explain="Memory note.")
    if p.kind == "nudge":   # outreach: quiet hours and the frequency cap are code, not prompts (comms-mcp)
        from . import comms
        text = comms.clean(params.get("text", ""))
        if not text:
            return _block(["empty_nudge"], "A message needs some text.")
        ok, why, rule = comms.check()
        if not ok:
            return _block([rule], why)
        params["text"] = text
        return Verdict(verdict="allow", tier="auto", params=params, explain="A short message to you, within quiet hours and the frequency cap.")
    if p.kind == "new_block":
        return Verdict(verdict="allow", tier="approve", fired=["new_block_needs_approval"], params=params,
                       explain="Starting a new training block always needs your approval.")
    if p.kind == "restriction":  # adding a restriction only makes things safer
        return Verdict(verdict="allow", tier=_shift("auto", ledger.dial()), params=params, explain="Safer by construction.")
    if p.kind == "swap_exercise":
        # a model can name a day/exercise that is not there or an unsafe replacement: block it here, never crash at apply time
        from backend.tools import plan_edit as pe
        try:
            di = int(params.get("day_index"))
            ex_name, rep = str(params.get("exercise", "")).strip().lower(), str(params.get("replacement", "")).strip().lower()
        except (TypeError, ValueError):
            return _block(["invalid_swap"], "A swap needs a day number, an exercise and a replacement.")
        if not 0 <= di < len(plan.get("days", [])):
            return _block(["invalid_swap"], f"There is no training day {di}.")
        if ex_name not in {e["name"] for e in plan["days"][di]["exercises"]}:
            return _block(["invalid_swap"], f"{ex_name} is not in {plan['days'][di]['name']}.")
        if rep not in {a["name"] for a in pe.get_alternatives(ex_name)}:
            return _block(["invalid_swap"], f"{rep} is not a safe like-for-like swap for {ex_name} given your restrictions.")
        params.update(day_index=di, exercise=ex_name, replacement=rep)
        from . import trust
        tier, note = trust.adjust("swap_exercise", params, _shift("auto", ledger.dial()), [])
        return Verdict(verdict="allow", tier=tier, params=params, veto_window_minutes=VETO_MINUTES if tier == "notify_first" else None,
                       explain=("Like-for-like swap (validated). " + note).strip())

    if p.kind == "volume_change":
        frac = float(params.get("fraction", 0))
        cur, base = plan.get("volume_mult", 1.0), plan.get("base_volume", 1.0)
        if frac > 0:
            injuries = [m for m in db.get_memory(20) if m["kind"] == "injury"]
            if injuries or ledger.get_setting("pain_active", False):
                return _block(["no_increase_under_pain"], "No volume increase while an injury/pain restriction is active.")
            room = max(0.0, WEEKLY_VOLUME_INCREASE_CAP - ledger.applied_volume_increase(7))
            if frac > room:
                fired.append("max_weekly_load_increase")
                frac, verdict = round(room, 4), "allow_with_limits"
                explain = f"Weekly load increase capped at {WEEKLY_VOLUME_INCREASE_CAP:.0%}. "
            ceiling = base * MAX_VOLUME_VS_BASE
            if cur * (1 + frac) > ceiling:
                fired.append("max_volume_vs_baseline")
                frac, verdict = round(max(0.0, ceiling / cur - 1), 4), "allow_with_limits"
                explain += "Volume cannot exceed 125% of baseline. "
            if frac <= 0.001:
                return _block(fired or ["max_weekly_load_increase"], explain or "No headroom for a volume increase.")
            tier = "auto" if frac <= 0.05 else "notify_first"
        else:
            if cur * (1 + frac) < MIN_VOLUME:
                fired.append("min_volume")
                frac, verdict = round(MIN_VOLUME / cur - 1, 4), "allow_with_limits"
                explain = "Volume cannot go below 50% of the template. "
            tier = "auto" if abs(frac) <= AUTO_TRIM_MAX else "approve"
        params["fraction"] = frac

    elif p.kind == "calorie_change":
        frac = float(params.get("fraction", 0))
        cur = plan.get("nutrition", {}).get("calories")
        if not cur:
            return _block(["no_nutrition_plan"], "No calorie target to change.")
        floor = CALORIE_FLOOR.get(prof.get("sex", "m"), 1500)
        if abs(frac) > 0.20:
            return _block(["max_calorie_change"], "A calorie change over 20% is never automatic.")
        if cur * (1 + frac) < floor:
            fired.append("min_calorie_floor")
            if cur <= floor:
                return _block(fired, f"Calories are already at the {floor} kcal floor for safety.")
            frac, verdict = round(floor / cur - 1, 4), "allow_with_limits"
            explain = f"Calories cannot go below {floor} kcal. "
        a = abs(frac)
        tier = "auto" if a <= 0.05 else "notify_first" if a <= 0.10 else "approve"
        params["fraction"] = frac

    tier = _shift(tier, ledger.dial())
    from . import trust   # earned trust: bounded, after every deterministic limit, never loosens approve/escalate or a limit that fired
    tier, trust_note = trust.adjust(p.kind, params, tier, fired)
    if trust_note:
        explain += trust_note + " "
    return Verdict(verdict=verdict, tier=tier, fired=fired, params=params,
                   veto_window_minutes=VETO_MINUTES if tier == "notify_first" else None,
                   explain=(explain + f"Tier {tier}.").strip())
