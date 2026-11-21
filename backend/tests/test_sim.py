"""12-week simulation regression tests. The real agents run against a simulated lifter (backend/sim).
Each run is a few seconds, so this file is the slow part of the suite. Thresholds are deliberately loose:
they guard the BEHAVIOUR (detects, doesn't thrash, recovers), not exact numbers."""
import pytest

from backend.sim.athlete import SCENARIOS
from backend.sim.run import run


def sc(prefix):
    return next(s for s in SCENARIOS if s.name.startswith(prefix))


def test_steady_progress_leaves_plan_alone():
    r = run(sc("steady"), "weekly")
    assert r["n_changes"] == 0 and set(r["vol_trace"]) == {1.0}


def test_plateau_is_detected_and_adaptation_pays_off():
    adaptive, control = run(sc("plateau"), "weekly"), run(sc("plateau"), "none")
    assert max(adaptive["vol_trace"]) > 1.0
    assert adaptive["final_e1rm"] > control["final_e1rm"] * 1.03   # at least +3% better than a static plan
    assert adaptive["reversals"] == 0                              # climbs, doesn't zig-zag


def test_auto_volume_is_capped_even_with_constant_reviews():
    r = run(sc("plateau"), "session")
    assert max(r["vol_trace"]) <= 1.25 + 1e-9


def test_overreaching_cuts_volume_then_recovers():
    r = run(sc("overreach"), "weekly")
    assert min(r["vol_trace"][5:9]) < 1.0           # noticed during the stress block
    assert r["vol_trace"][-1] >= 0.99               # and returned to baseline afterwards


def test_pain_vetoes_immediately_then_eases_back():
    r = run(sc("pain"), "weekly")
    assert r["vetoes"] >= 1
    assert r["vol_trace"][3] <= 0.65                # capped the same week it was reported
    assert r["vol_trace"][-1] >= 0.99               # and doesn't stay crushed forever


def test_noisy_data_with_constant_reviews_does_not_thrash():
    r = run(sc("noisy"), "session")                 # worst case: review after every single session
    assert r["n_changes"] <= 8
    assert 0.9 <= min(r["vol_trace"]) and max(r["vol_trace"]) <= 1.25 + 1e-9
