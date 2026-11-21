from backend.policy.adaptation import (
    Signal, change_dampening, has_safety_flag, readiness_score,
    recommended_change, trend_signal,
)
from backend.tools.calculators import (
    epley_e1rm, linear_slope, macro_targets, next_set_suggestion, round_to_plate,
)


def test_epley():
    assert epley_e1rm(100, 1) == 100
    assert epley_e1rm(80, 8) == 101.3


def test_round_to_plate():
    assert round_to_plate(78.0) == 77.5
    assert round_to_plate(78.9) == 80.0


def test_next_set_hard_drops_load():
    r = next_set_suggestion(80, 6, 10, target_reps=8, target_rpe=8)
    assert r["weight"] < 80


def test_next_set_easy_raises_load():
    r = next_set_suggestion(80, 10, 6, target_reps=8, target_rpe=8)
    assert r["weight"] > 80


def test_slope():
    assert linear_slope([1, 2, 3, 4]) == 1.0


def test_macros_sum_reasonable():
    m = macro_targets(2500, 80)
    assert m["protein_g"] == 144


def test_noise_does_not_change_plan():
    noisy = [100, 101, 99, 100, 101, 100, 99, 100]
    sig = trend_signal("e1rm", noisy, scale=1.0)
    assert recommended_change(sig, 0.1) == 0.0


def test_sustained_signal_changes_plan():
    rising_rpe = [7, 7.5, 8, 8.5, 9, 9.5, 9.5, 10]
    sig = trend_signal("rpe", rising_rpe, scale=0.3)
    assert recommended_change(sig, 0.1) > 0


def test_dampening_is_soft_not_a_ban():
    assert change_dampening(0) == 1.0
    assert abs(change_dampening(10) - 0.3) < 1e-9
    strong = Signal("x", 1.0, 1.0)
    assert recommended_change(strong, 0.1, recent_changes=10) > 0


def test_safety_flag():
    assert has_safety_flag("my shoulder has a sharp pain") == "pain"
    assert has_safety_flag("felt great") is None


def test_readiness():
    assert readiness_score(8, 1, 5)["label"] == "high"
    assert readiness_score(4, 5, 1)["label"] == "low"


def test_safety_flag_catches_how_lifters_actually_talk():
    for phrase in ("my elbow hurts", "i tweaked my back", "felt a sharp pinch", "got tingling in my hand", "strained something"):
        assert has_safety_flag(phrase), phrase
    for phrase in ("pulled 180 for 3", "legs are sore today", "felt great"):
        assert has_safety_flag(phrase) is None, phrase
