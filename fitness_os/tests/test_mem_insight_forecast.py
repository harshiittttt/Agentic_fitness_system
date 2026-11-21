"""forecast.py: predictions with intervals are logged, resolved against the frames, scored against the naive baseline, and the biggest misses explained."""
from __future__ import annotations

import os
from datetime import date, timedelta

import numpy as np
import pytest

from backend.memory import db
from fitness_os.mem import forecast, frames, jobs, miner, store, synth

END = "2026-03-10"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", f"{END}T20:00:00+00:00")


def test_e1rm_forecast_is_sane_logged_and_idempotent_per_day():
    synth.make_athlete(3, 84, synth.default_effects())
    rows = frames.build(60)
    recent = [r["e1rm_squat"] for r in rows if r.get("e1rm_squat")][-6:]
    p = forecast.predict("e1rm", "squat", horizon_days=3)
    assert p["lo"] < p["value"] < p["hi"] and p["horizon"] == (date.fromisoformat(END) + timedelta(days=3)).isoformat()
    assert 0.8 * np.median(recent) < p["value"] < 1.2 * np.median(recent)
    assert p["n"] >= 6 and p["model"] and isinstance(p["basis"], list)
    stored = store.predictions("e1rm", "squat")
    assert len(stored) == 1 and stored[0]["predicted"] == pytest.approx(p["value"]) and stored[0]["lo"] == pytest.approx(p["lo"])
    assert forecast.predict("e1rm", "squat", horizon_days=3)["prediction_id"] == p["prediction_id"]      # same question, same day: not logged twice
    assert len(store.predictions("e1rm", "squat")) == 1


def test_every_target_forecasts_and_thin_data_declines_instead_of_guessing():
    synth.make_athlete(3, 84, synth.default_effects())
    for t, kw in (("readiness", {}), ("bodyweight", {"horizon_days": 7}), ("adherence", {})):
        p = forecast.predict(t, **kw)
        assert p["value"] is not None and p["lo"] <= p["value"] <= p["hi"], t
    a = forecast.predict("adherence")
    assert 0.0 <= a["lo"] <= a["value"] <= a["hi"] <= 1.0 and a["basis"]
    assert forecast.predict("e1rm", "snatch")["value"] is None
    assert forecast.predict("e1rm")["value"] is None and forecast.predict("sleep")["value"] is None


def test_no_data_and_thin_data_never_raise():
    assert forecast.predict("readiness")["value"] is None
    db.add_checkin(sleep_h=7.0, soreness=2, energy=3)
    assert forecast.predict("readiness")["value"] is None
    assert isinstance(forecast.resolve_due(), dict) and forecast.calibration("e1rm")["n"] == 0 and forecast.surprises(3) == []


def test_forecast_uses_only_the_past(monkeypatch):
    """Standing at an earlier date, later rows (which exist in this pre-filled database) must not influence the forecast."""
    synth.make_athlete(3, 84, synth.default_effects())
    a = forecast.predict("readiness", as_of="2026-02-20", log=False)
    with db.conn() as c:
        c.execute("DELETE FROM checkins WHERE ts > '2026-02-21'")
    b = forecast.predict("readiness", as_of="2026-02-20", log=False)
    assert a["value"] == pytest.approx(b["value"]) and a["lo"] == pytest.approx(b["lo"])


def test_predictions_resolve_against_what_happened_and_are_scored():
    synth.make_athlete(3, 84, synth.default_effects())
    d0 = date.fromisoformat(END) - timedelta(days=12)
    for k in range(0, 10):
        os.environ["FITNESS_NOW"] = f"{(d0 + timedelta(days=k)).isoformat()}T20:00:00+00:00"
        forecast.predict("readiness", horizon_days=1)
        forecast.predict("e1rm", "squat", horizon_days=2)
        forecast.predict("adherence", horizon_days=1)
    os.environ["FITNESS_NOW"] = f"{END}T20:00:00+00:00"
    assert store.predictions(resolved=False)
    r = forecast.resolve_due()
    assert r["resolved"] >= 15
    assert forecast.resolve_due()["resolved"] == 0                                      # nothing resolved twice
    for t in ("readiness", "e1rm", "adherence"):
        c = forecast.calibration(t)
        assert c["n"] >= 5 and c["mae"] is not None and c["naive_mae"] is not None and c["skill"] is not None
        if t != "adherence":
            assert 0.0 <= c["coverage"] <= 1.0
    ps = store.predictions("readiness", resolved=True)
    day = {r_["date"]: r_ for r_ in frames.build(40)}
    for p in ps:
        assert p["actual"] == pytest.approx(day[p["features"]["target_date"]]["readiness"])    # resolved to the real frame value
        assert p["error"] == pytest.approx(p["actual"] - p["predicted"])


def test_a_prediction_for_a_day_that_is_not_over_stays_pending():
    synth.make_athlete(3, 84, synth.default_effects())
    forecast.predict("readiness", horizon_days=1)
    assert forecast.resolve_due()["resolved"] == 0
    os.environ["FITNESS_NOW"] = f"{(date.fromisoformat(END) + timedelta(days=3)).isoformat()}T09:00:00+00:00"     # tomorrow's frame never got a check-in
    assert forecast.resolve_due()["resolved"] == 0


def test_surprises_rank_by_standardised_error_with_plain_explanations():
    synth.make_athlete(3, 84, synth.default_effects())
    for k, (pred, actual) in enumerate([(60.0, 61.0), (60.0, 75.0), (60.0, 50.0), (60.0, 60.5)]):
        pid = store.log_prediction("readiness", pred, f"2026-03-0{k + 3}T23:59:59+00:00", lo=55.0, hi=65.0, model="m",
                                   features={"target_date": f"2026-03-0{k + 3}", "naive": 58.0})
        store.resolve_prediction(pid, actual)
    s = forecast.surprises(2)
    assert [x["actual"] for x in s] == [75.0, 50.0]                                      # biggest standardised misses first
    assert s[0]["z"] == pytest.approx(15 / (10 / (2 * 1.2816)), abs=0.01) and "expected" in s[0]["explanation"] and "readiness" in s[0]["explanation"] and "2026-03-04" in s[0]["explanation"]
    assert forecast.surprises(1, target="e1rm") == []


def test_confirmed_discoveries_adjust_the_forecast_and_say_why():
    synth.make_athlete(4, 112, synth.default_effects())
    miner.run()
    assert store.discovery_by_key("sleep_performance:sleep_h>perf:lag0")["status"] == "confirmed"
    with db.conn() as c:
        c.execute("DELETE FROM checkins WHERE ts LIKE ?", (f"{END}%",))
    db.add_checkin(sleep_h=5.0, soreness=2, energy=3)                                    # a short night, session today
    with db.conn() as c:
        c.execute("DELETE FROM sets WHERE session = ?", (END,))
    plain = forecast.predict("e1rm", "squat", horizon_days=0, use_discoveries=False, log=False)
    adj = forecast.predict("e1rm", "squat", horizon_days=0, log=False)
    assert adj["value"] < plain["value"] and any("sleep 5" in b for b in adj["basis"])
    assert "confirmed patterns" in adj["model"]


def test_forecasts_beat_the_naive_baseline_with_honest_intervals():
    """A replay on synthetic athletes: only older rows visible at each step. (The big sweep is in bench_insight.)"""
    r = forecast.backtest(n_athletes=4, weeks=12, start_week=6)
    assert r["e1rm"]["skill"] > 0 and r["readiness"]["skill"] > 0 and r["adherence"]["skill"] > 0
    for t in ("e1rm", "readiness"):
        assert abs(r[t]["coverage"] - forecast.NOMINAL) <= 0.15, (t, r[t])


def test_jobs_resolve_step_is_the_forecast_function():
    assert jobs.NIGHTLY[0] == ("forecast", "resolve_due")
    assert "error" not in jobs.nightly()["forecast.resolve_due"]
