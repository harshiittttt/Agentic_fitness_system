"""Earned-trust autonomy: tiers loosen with accepted changes, tighten after pushback, never past a hard limit or the dial."""
import pytest

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import control, ledger, policy, trust
from fitness_os.schema import Proposal


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T12:00:00+00:00")
    orch.onboard({**demo.PROFILE, "name": "Alex"})


def vol(frac, agent="training"):
    return Proposal(agent=agent, kind="volume_change", params={"fraction": frac}, rationale="Evidence says so.", confidence=0.7)


def history(frac, statuses, kind="volume_change", agent="training", tier="auto"):
    """Past decisions. Accepted ones are backdated 8 days: outside the 7-day weekly load cap (which counts applied increases) but inside the trust window."""
    for st in statuses:
        did = ledger.append(agent=agent, kind=kind, proposal={"agent": agent, "kind": kind, "params": {"fraction": frac}, "rationale": "r"},
                            action={"fraction": frac}, rationale="r", evidence="e", tier=tier, fired=[], status=st)
        if st == "applied":
            ledger.update(did, ts="2026-03-02T12:00:00+00:00")


def test_action_types_split_by_direction():
    assert trust.action_type("volume_change", {"fraction": -0.1}) == "volume:cut"
    assert trust.action_type("volume_change", {"fraction": 0.05}) == "volume:increase"
    assert trust.action_type("calorie_change", {"fraction": -0.05}) == "calories:cut"


def test_a_fresh_system_changes_nothing():
    v = policy.check_action(vol(-0.10))
    assert v.tier == "auto" and "Trusted" not in v.explain and "Stricter" not in v.explain


def test_notify_first_becomes_auto_after_enough_accepted_changes():
    assert policy.check_action(vol(0.08)).tier == "notify_first"          # a 8% increase waits out a veto window...
    history(0.08, ["applied"] * 6, tier="notify_first")
    v = policy.check_action(vol(0.08))
    assert v.tier == "auto" and "Trusted" in v.explain and v.veto_window_minutes is None


def test_trust_is_per_action_type():
    history(0.08, ["applied"] * 6, tier="notify_first")                   # earned for increases
    assert policy.check_action(vol(-0.10)).tier == "auto"                  # cuts were always auto; unaffected
    assert policy.check_action(Proposal(agent="nutrition", kind="calorie_change", params={"fraction": 0.08}, rationale="needs more", confidence=0.6)).tier == "notify_first"


def test_reverts_tighten_auto_to_notify_first_and_then_approve():
    history(-0.10, ["applied", "reverted", "reverted"])
    v = policy.check_action(vol(-0.10))
    assert v.tier == "notify_first" and "Stricter" in v.explain and v.veto_window_minutes
    history(-0.10, ["vetoed"])
    assert policy.check_action(vol(-0.10)).tier == "approve"


def test_old_pushback_is_forgotten():
    history(-0.10, ["reverted", "reverted", "reverted"])
    assert policy.check_action(vol(-0.10)).tier == "approve"
    import os
    os.environ["FITNESS_NOW"] = "2026-05-01T12:00:00+00:00"                # 7 weeks later: outside the 14-day window
    assert policy.check_action(vol(-0.10)).tier == "auto"


def test_hands_on_dial_is_never_loosened_by_trust():
    history(0.08, ["applied"] * 8, tier="notify_first")
    ledger.set_setting("dial", "hands_on")
    assert policy.check_action(vol(0.08)).tier == "approve"               # hands_on pushes notify -> approve; trust does not undo that


def test_approve_and_hard_limits_are_never_loosened():
    history(-0.40, ["applied"] * 9, tier="approve")
    assert policy.check_action(vol(-0.40)).tier == "approve"              # a cut over 30% always needs you
    history(0.5, ["applied"] * 9, tier="notify_first")
    v = policy.check_action(vol(0.5))
    assert "max_weekly_load_increase" in v.fired and v.tier != "auto"     # a limit fired: trust stays out of it


def test_the_athletes_own_changes_are_not_trust_evidence():
    history(0.08, ["applied"] * 9, agent="user", tier="user")
    assert policy.check_action(vol(0.08)).tier == "notify_first"


def test_new_action_types_can_start_one_tier_stricter_when_enabled():
    assert policy.check_action(vol(-0.10)).tier == "auto"
    ledger.set_setting("strict_new_types", True)
    v = policy.check_action(vol(-0.10))
    assert v.tier == "notify_first" and "track record" in v.explain
    history(-0.10, ["applied"] * 3)
    assert policy.check_action(vol(-0.10)).tier == "auto"                  # it now has a track record


def test_escalation_and_safety_paths_ignore_trust():
    r = control.submit(Proposal(agent="recovery", kind="escalate", params={"domain": "training", "reason": "numbness"}, rationale="Red flag.", confidence=1))
    assert r["tier"] == "escalate"
