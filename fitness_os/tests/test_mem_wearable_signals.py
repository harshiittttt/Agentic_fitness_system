"""Wearable rhr / hrv / steps in the miner frames: columns exist (None without a device), SD floors, graceful absence, and hypothesis tests that find a
planted link and stay quiet on noise. Fully offline, scratch databases only."""
from __future__ import annotations

import numpy as np
import pytest

from backend import wearables
from backend.memory import db
from fitness_os.mem import frames, miner, synth

END = "2026-03-10"
K_HRV, K_RHR, K_STEPS = "hrv_performance:hrv_ms>perf:lag0", "rhr_performance:rhr>perf:lag0", "steps_performance:steps_prev_day>perf:lag1"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "w.db"))
    monkeypatch.setenv("FITNESS_NOW", f"{END}T12:00:00+00:00")


def _by_key(res):
    return {r["key"]: r for r in res}


def _athlete(seed=11, days=112):
    synth.make_athlete(seed, days, None, end=END)
    rows = frames.build(days)
    return rows, frames.set_rows(days)


def test_frame_keys_and_floors_exist_without_any_device():
    rows, _ = _athlete()
    assert all(k in r and r[k] is None for r in rows for k in frames.WEARABLE_KEYS)
    for k in ("rhr", "hrv_ms", "steps"):
        assert k in frames.BASE_KEYS and frames._SD_FLOOR[k] > 0
    # no device: no wearable hypothesis is tested, nothing raises, no deviations mention them
    res = _by_key(miner.analyze(n_perm=99, n_boot=30))
    assert not ({K_HRV, K_RHR, K_STEPS} & set(res))
    assert not any("HRV" in d or "heart rate" in d for d in frames.today()["deviations"])


def test_ingested_wearable_values_reach_frames_and_baselines_use_floors():
    _athlete()
    days = [r["date"] for r in frames.build(60)]
    recs = [{"date": d, "metric": "resting_hr", "value": 58.0} for d in days] + [{"date": d, "metric": "steps", "value": 8000} for d in days[-30:]]
    out = wearables.ingest("garmin", recs)
    assert out["rejected"] == 0
    rows = frames.build(60)
    assert all(r["rhr"] == 58.0 for r in rows) and rows[-1]["hrv_ms"] is None and rows[0]["steps"] is None and rows[-1]["steps"] == 8000
    b = frames._baseline(rows, "rhr")
    assert b is not None and b["sd"] == frames._SD_FLOOR["rhr"]         # a perfectly flat series still gets a floored SD
    assert frames._baseline(rows[:-35] + rows[-1:], "steps") is None     # too little history: no baseline
    t = frames.today()
    assert "rhr" in t["baseline"] and "hrv_ms" not in t["baseline"]


def test_today_flags_a_hrv_drop_in_plain_words():
    _athlete()
    days = [r["date"] for r in frames.build(60)]
    wearables.ingest("oura", [{"date": d, "metric": "hrv_ms", "value": 70.0 + (i % 3) * 2} for i, d in enumerate(days[:-1])] + [{"date": days[-1], "metric": "hrv_ms", "value": 35.0}])
    dev = [d for d in frames.today()["deviations"] if d.startswith("HRV")]
    assert dev and "below your usual" in dev[0] and "35 ms" in dev[0]


def _with_signal(rows, sets, key, fn, seed):
    """Rows with the wearable column derived from the athlete's own (hidden) performance index plus noise."""
    ctx = miner.make_ctx(rows, sets, {})
    rng = np.random.default_rng(seed)
    perf = np.where(np.isfinite(ctx.perf), ctx.perf, 0.0)
    out = [dict(r) for r in rows]
    for i, r in enumerate(out):
        r[key] = fn(perf[i], rng)
    return out


def test_planted_hrv_and_rhr_links_are_found_with_plausible_sign():
    rows, sets = _athlete(12, 140)
    sd = float(np.nanstd(miner.make_ctx(rows, sets, {}).perf))
    hrv = _with_signal(rows, sets, "hrv_ms", lambda p, g: 60.0 + 25.0 * p / sd + g.normal(0, 4), 1)
    res = _by_key(miner.analyze(rows=hrv, sets=sets, profile={}, n_perm=999, n_boot=200))
    assert res[K_HRV]["effect"] > 0 and res[K_HRV]["p"] < 0.01 and res[K_HRV]["gates"]["plausible"]
    rhr = _with_signal(rows, sets, "rhr", lambda p, g: 55.0 - 4.0 * p / sd + g.normal(0, 1.2), 2)
    r2 = _by_key(miner.analyze(rows=rhr, sets=sets, profile={}, n_perm=999, n_boot=200))
    assert r2[K_RHR]["effect"] < 0 and r2[K_RHR]["p"] < 0.01 and r2[K_RHR]["gates"]["plausible"]
    txt = miner.render(res[K_HRV])
    assert "HRV" in txt["statement"] and txt["trait"][1] == "hrv->performance"


def test_pure_noise_wearables_do_not_confirm_and_sparse_or_flat_series_are_skipped():
    rows, sets = _athlete(13, 140)
    noisy = _with_signal(rows, sets, "hrv_ms", lambda p, g: 60.0 + g.normal(0, 8), 3)
    for r, g in zip(noisy, np.random.default_rng(4).normal(55, 3, len(noisy))):
        r["rhr"] = g
    res = _by_key(miner.analyze(rows=noisy, sets=sets, profile={}, n_perm=499, n_boot=100))
    assert not res[K_HRV]["passes"] and not res[K_RHR]["passes"]
    sparse = [dict(r, hrv_ms=(60.0 + i % 5 if i % 20 == 0 else None), rhr=55.0, steps=None) for i, r in enumerate(rows)]       # ~6 readings, flat rhr, no steps
    res = _by_key(miner.analyze(rows=sparse, sets=sets, profile={}, n_perm=99, n_boot=30))
    assert not ({K_HRV, K_RHR, K_STEPS} & set(res))


def test_planted_steps_lag1_link_found_and_null_steps_quiet():
    rows, sets = _athlete(14, 140)
    ctx = miner.make_ctx(rows, sets, {})
    sd = float(np.nanstd(ctx.perf))
    perf = np.where(np.isfinite(ctx.perf), ctx.perf, 0.0)
    g = np.random.default_rng(5)
    # yesterday's steps drive today's performance negatively: steps[i-1] high -> perf[i] low, so derive steps[i] from perf[i+1]
    planted = [dict(r) for r in rows]
    for i, r in enumerate(planted):
        nxt = perf[i + 1] if i + 1 < len(perf) else 0.0
        r["steps"] = float(max(0.0, 9000.0 - 3000.0 * nxt / sd + g.normal(0, 800)))
    res = _by_key(miner.analyze(rows=planted, sets=sets, profile={}, n_perm=999, n_boot=200))
    assert K_STEPS in res and res[K_STEPS]["effect"] < 0 and res[K_STEPS]["p"] < 0.05
    null = [dict(r, steps=float(g.normal(9000, 2500))) for r in rows]
    res = _by_key(miner.analyze(rows=null, sets=sets, profile={}, n_perm=499, n_boot=100))
    assert K_STEPS not in res or not res[K_STEPS]["passes"]
