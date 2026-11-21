"""synth.py (planted truth, determinism, null mode, mess) and frames.py (daily matrix, today vs normal, data hash, speed). Scratch database, fixed clock."""
from __future__ import annotations

import time

import numpy as np
import pytest

from backend.memory import db
from fitness_os.mem import frames, synth

END = "2026-03-10"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", f"{END}T12:00:00+00:00")


def _counts() -> dict:
    with db.conn() as c:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("sets", "checkins", "meals")}


# ---------------------------------------------------------------- synth
def test_make_athlete_writes_realistic_rows_and_returns_truth():
    truth = synth.make_athlete(3, 84, synth.default_effects())
    n = _counts()
    assert n["sets"] > 300 and 60 < n["checkins"] < 100 and n["meals"] > 200
    assert db.get_profile() and db.get_plan()
    assert truth["end"] == END and truth["days"] == 84 and not truth["null"]
    for name in ("sleep_perf", "volume", "weekday_adherence", "rpe_bias", "maintenance", "late_meal_sleep", "protein_soreness"):
        assert name in truth["effects"]
    assert truth["effects"]["sleep_perf"]["slope_pct_per_unit"] == pytest.approx(2.0)      # planted +2% per hour is exactly what the data carry
    assert truth["effects"]["maintenance"]["true_tdee"] == pytest.approx(truth["formula_tdee"] + 320)
    with db.conn() as c:
        s = c.execute("SELECT * FROM sets LIMIT 1").fetchone()
    assert s["session"][:2] == "20" and s["ts"].startswith(s["session"])                    # session date label is the truth; ts is consistent with it


def test_it_is_deterministic_per_seed_and_different_across_seeds():
    synth.make_athlete(5, 60, synth.default_effects())
    a = frames.data_hash(frames.build(60))
    synth.make_athlete(5, 60, synth.default_effects())
    assert frames.data_hash(frames.build(60)) == a
    synth.make_athlete(6, 60, synth.default_effects())
    assert frames.data_hash(frames.build(60)) != a


def test_null_mode_has_no_effects_but_the_same_mess():
    t = synth.make_athlete(8, 70, None)
    assert t["null"] and t["effects"] == {}
    rows = frames.build(70)
    assert sum(1 for r in rows if r["trained"]) > 20
    assert any(r["sleep_h"] is None for r in rows) and any(r["soreness"] is None for r in rows)    # missing fields and days
    assert sum(1 for r in rows if not r["logged"]) >= 1


def test_there_is_a_progression_trend_the_confounder():
    synth.make_athlete(9, 112, None)
    rows = frames.build(112)
    key = "e1rm_squat"
    ys = [r[key] for r in rows if r.get(key)]
    assert np.mean(ys[-8:]) > np.mean(ys[:8])


def test_the_planted_monday_dip_and_maintenance_offset_are_in_the_data():
    truth = synth.make_athlete(4, 112, synth.default_effects())
    rows = frames.build(112)
    mon = np.mean([r["trained"] for r in rows if r["dow"] == 0])
    others = sorted(np.mean([r["trained"] for r in rows if r["dow"] == d]) for d in range(1, 7))[-3:]
    assert mon < np.mean(others) - 0.15
    assert truth["true_tdee"] - truth["formula_tdee"] == pytest.approx(320)


def test_effect_strength_scales_the_planted_sizes():
    a, b = synth.default_effects(1.0), synth.default_effects(0.5)
    assert b["sleep_perf"]["pct_per_hour"] == pytest.approx(a["sleep_perf"]["pct_per_hour"] / 2)


def test_synth_is_quick():
    t = time.perf_counter()
    synth.make_athlete(1, 120, synth.default_effects())
    assert time.perf_counter() - t < 5


# ---------------------------------------------------------------- frames
def test_frames_shape_keys_and_unknown_is_none():
    synth.make_athlete(2, 60, synth.default_effects())
    rows = frames.build(60)
    assert len(rows) == 60 and rows[-1]["date"] == END and [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    r0 = rows[-1]
    for k in ("date", "dow", "sleep_h", "soreness", "energy", "bodyweight", "kcal", "protein_g", "carbs_g", "fat_g", "trained", "n_sets", "volume_kg", "rpe_mean",
              "readiness", "pain_note"):
        assert k in r0
    assert any(k.startswith("e1rm_") for k in r0)
    assert all(r["trained"] == (r["n_sets"] > 0) for r in rows)
    assert all((r["readiness"] is None) or (r["sleep_h"] is not None and r["soreness"] is not None and r["energy"] is not None) for r in rows)   # never invented
    assert any(r["kcal"] is None for r in rows)


def test_frames_use_the_session_date_label_not_the_insert_time():
    db.save_profile({"age": 30})
    for i in range(5):
        db.add_set(f"2026-03-0{i + 1}", "squat", 100.0, 5, 8.0)         # back-dated rows: ts is 'now' (March 10)
    rows = frames.build(10)
    by = {r["date"]: r for r in rows}
    assert by["2026-03-03"]["trained"] and not by["2026-03-10"]["trained"]
    assert by["2026-03-03"]["e1rm_squat"] == pytest.approx(100 * (1 + 7 / 30), rel=0.01)    # rpe-adjusted Epley: 5 reps + 2 in reserve


def test_frames_meal_clock_and_late_flag_only_when_trustworthy():
    db.add_meal("2026-03-09", "dinner", None, 800, 40, 80, 30)            # ts is the insert time (March 10): no usable clock for March 9
    with db.conn() as c:
        c.execute("INSERT INTO meals (ts, day, name, grams, kcal, protein, carbs, fat, estimated) VALUES ('2026-03-08T22:15:00+00:00','2026-03-08','snack',NULL,300,5,30,10,0)")
    by = {r["date"]: r for r in frames.build(5)}
    assert by["2026-03-09"]["late_meal"] is None and by["2026-03-09"]["kcal"] == 800
    assert by["2026-03-08"]["late_meal"] is True and by["2026-03-08"]["last_meal_h"] == pytest.approx(22.25)


def test_frames_ignore_impossible_values_and_flag_pain_notes():
    with db.conn() as c:
        c.execute("INSERT INTO checkins (ts, sleep_h, soreness, energy, bodyweight, note) VALUES ('2026-03-10T07:00:00+00:00', 40, 9, 3, 5, 'my knee hurts')")
    r = frames.build(3)[-1]
    assert r["sleep_h"] is None and r["soreness"] is None and r["bodyweight"] is None and r["energy"] == 3 and r["pain_note"]


def test_frames_are_deterministic_and_fast_for_120_days():
    synth.make_athlete(5, 120, synth.default_effects())
    a = frames.build(120)
    t = time.perf_counter()
    for _ in range(5):
        b = frames.build(120)
    ms = (time.perf_counter() - t) / 5 * 1000
    assert a == b and frames.data_hash(a) == frames.data_hash(b)
    assert ms < 200


def test_data_hash_changes_when_data_changes():
    synth.make_athlete(5, 60, None)
    h = frames.data_hash(frames.build(60))
    db.add_checkin(sleep_h=6.0)
    assert frames.data_hash(frames.build(60)) != h


def test_today_reports_deviations_with_numbers_computed_by_code():
    synth.make_athlete(5, 90, None)
    with db.conn() as c:
        c.execute("DELETE FROM checkins WHERE ts LIKE ?", (f"{END}%",))
    db.add_checkin(sleep_h=4.0, soreness=5, energy=1)                  # a clearly bad morning (db.now() honours FITNESS_NOW)
    t = frames.today()
    assert t["frame"]["date"] == END and t["frame"]["sleep_h"] == 4.0
    assert t["z"]["sleep_h"] < -1 and set(t["baseline"]["sleep_h"]) == {"mean", "sd", "n"}
    sl = [d for d in t["deviations"] if d.startswith("slept")]
    assert sl and "4.0 h" in sl[0] and "below your usual" in sl[0]
    base = t["baseline"]["sleep_h"]["mean"]
    assert f"{abs(4.0 - base):.1f} h" in sl[0]                         # the gap in the sentence is the real gap
    assert any(d.startswith("soreness") for d in t["deviations"]) and any(d.startswith("energy") for d in t["deviations"])


def test_today_with_no_data_is_empty_not_an_error():
    t = frames.today()
    assert t["baseline"] == {} and t["z"] == {}
    assert t["frame"]["sleep_h"] is None and "no check-in logged yet today" in t["deviations"]
