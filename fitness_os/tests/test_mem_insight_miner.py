"""The miner end to end: planted truth is confirmed with the right sign and size, null athletes yield nothing, the lifecycle (candidate / confirmed /
weakening / retired) behaves, statements are generated from numbers, traits are written, runs are idempotent and never raise."""
from __future__ import annotations

import re

import numpy as np
import pytest

from backend.memory import db
from fitness_os.mem import frames, jobs, miner, store, synth

END = "2026-03-10"
K_MAINT = "maintenance_calories:intake_vs_weight>tdee:window"
K_LATE = "late_meals_sleep:late_meal>sleep_h:lag0"
K_PROT = "protein_recovery:protein_g>soreness:lag1"
K_SLEEP = "sleep_performance:sleep_h>perf:lag0"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", f"{END}T12:00:00+00:00")


def _by_key(res: list[dict]) -> dict:
    return {r["key"]: r for r in res}


# ---------------------------------------------------------------- recovery of planted truth
def test_planted_effects_are_confirmed_with_right_sign_and_size():
    truth = synth.make_athlete(4, 112, synth.default_effects())
    res = _by_key(miner.analyze(n_perm=999, n_boot=200))
    assert sum(r["passes"] for r in res.values()) >= 6
    m = res[K_MAINT]
    assert m["passes"] and abs(m["effect"] - 320) < 0.35 * 320 and m["ci"][0] < 320 < m["ci"][1]
    late = res[K_LATE]
    assert late["passes"] and late["effect"] < 0 and abs(late["effect"] + 0.9) < 0.35 * 0.9
    prot = res[K_PROT]
    assert prot["passes"] and prot["effect"] < 0 and abs(prot["effect"] + 0.5) < 0.5 * 0.5
    sl = res[K_SLEEP]
    assert sl["passes"] and sl["effect"] > 0 and sl["ci"][0] < truth["effects"]["sleep_perf"]["slope_pct_per_unit"] < sl["ci"][1]
    wd = res["weekday_adherence:trained>weekday:lag0"]
    assert wd["passes"] and wd["weekday"] == 0 and wd["rate"] < wd["rate_other"] - 0.2          # Monday
    wp = res["weekday_performance:perf>weekday:lag0"]
    assert wp["weekday"] == 3 and wp["effect"] < 0                                              # Thursday runs low


def test_rpe_underreporting_is_found_and_an_honest_noisy_rater_is_not():
    truth = synth.make_athlete(5, 112, synth.default_effects())
    r = _by_key(miner.analyze(n_perm=499, n_boot=100)).get("rpe_bias:rpe>implied_e1rm:within_session")
    assert r and r["effect"] > 0 and r["p"] < 0.01 and r["reps_bias"] > 0
    assert r["effect"] == pytest.approx(truth["effects"]["rpe_bias"]["slope_pct_per_rir"], rel=0.5)
    for seed in (30, 31, 32):                                       # same noisy, rounded ratings, no bias planted
        synth.make_athlete(seed, 112, None)
        n = _by_key(miner.analyze(n_perm=499, n_boot=100)).get("rpe_bias:rpe>implied_e1rm:within_session")
        assert n is None or not n["passes"]


def test_rpe_null_is_calibrated_not_centred_on_zero_and_both_directions_are_testable():
    """An honest rater leaves a small NEGATIVE structural offset (censoring at failure, early-stopped sets); the p-value and effect are measured against honest
    synthetic copies of the athlete, so a very noisy honest rater is not flagged and an UNDER-rating (harder than it was) rater is found with a negative sign."""
    key = "rpe_bias:rpe>implied_e1rm:within_session"
    effs, offs = [], []
    for seed in (40, 41, 42, 43):                                      # rating noise doubled: raw slope about -1 % per RPE point
        synth.make_athlete(seed, 112, None, noise=2.0)
        r = _by_key(miner.analyze(n_perm=199, n_boot=60)).get(key)
        if r:
            effs.append(r["effect"])
            offs.append(r["null_offset"])
            assert not r["passes"]
    assert len(effs) >= 3 and np.mean(offs) < -0.2                      # the offset was real and was measured ...
    assert abs(np.mean(effs)) < 0.6 * abs(np.mean(offs))                # ... and removed from the reported effect
    synth.make_athlete(5, 112, {"rpe_bias": {"b": -0.4}})              # reports reserve SMALLER than it was (rates effort harder)
    r = _by_key(miner.analyze(n_perm=499, n_boot=100)).get(key)
    assert r and r["effect"] < 0 and r["p"] < 0.05 and r["reps_bias"] < 0


def test_weekly_whitening_recovers_negative_lag1_correlation():
    rng = np.random.default_rng(3)
    n = 60
    lev = rng.normal(0, 1, n + 1)
    d = np.diff(lev)                                                    # differences of white levels: MA(1), lag-1 correlation -0.5
    x = rng.normal(0, 1, n)
    N = np.ones((n, 1))
    xw, yw, Nw, theta, m = miner._ma1_whiten(x, d + 0.5 * x, N)
    assert -0.95 < theta < -0.3 and m.all() and len(yw) == n
    _, _, _, th0, _ = miner._ma1_whiten(x, rng.normal(0, 1, n) + 0.5 * x, N)
    assert th0 > -0.4                                                   # white errors: (almost) no filtering


def test_confounded_variable_is_not_credited_when_only_sleep_matters():
    """Energy follows sleep in this world; only sleep is planted. Holding sleep fixed, energy must not be confirmed."""
    synth.make_athlete(7, 112, {"sleep_perf": {"pct_per_hour": 2.5}})
    res = _by_key(miner.analyze(n_perm=999, n_boot=100))
    assert not res["energy_performance:energy>perf:lag0"]["passes"]
    assert not res["soreness_performance:soreness>perf:lag0"]["passes"]


# ---------------------------------------------------------------- null athletes
def test_null_athletes_produce_no_confirmed_discoveries():
    confirmed = tested = 0
    for seed in range(1000, 1006):
        synth.make_athlete(seed, 84, None)
        res = miner.analyze(n_perm=499, n_boot=60)
        tested += len(res)
        confirmed += sum(r["passes"] for r in res)
    assert tested >= 60
    assert confirmed / tested < 0.10                                # the bar; the bench measures 0 on 30 athletes


def test_too_little_data_tests_nothing_instead_of_guessing():
    synth.make_athlete(2, 12, synth.default_effects())
    res = miner.analyze(n_perm=199, n_boot=30)
    assert not any(r["passes"] for r in res)
    assert miner.run().get("skipped") or miner.run().get("ran")


# ---------------------------------------------------------------- run(): writes, idempotence, lifecycle
def test_run_writes_discoveries_statements_and_traits_and_is_idempotent():
    synth.make_athlete(4, 112, synth.default_effects())
    r1 = miner.run()
    assert r1["ran"] and r1["confirmed"] >= 6 and r1["tested"] >= 15
    conf = store.discoveries(status=("confirmed",), limit=50)
    keys = {d["key"] for d in conf}
    assert {K_MAINT, K_LATE, K_PROT} <= keys
    d = store.discovery_by_key(K_MAINT)
    assert d["family"] == "maintenance_calories" and "kcal" in d["statement"] and "formula" in d["statement"]
    assert "same direction" in d["statement"] and "42" in d["statement"] or "logged days" in d["statement"]     # n and the replication note
    assert d["recommendation"] and d["consumers"] == ["nutrition", "coach"]
    e = d["effect"]
    assert {"estimate", "ci", "p", "q", "n", "replicated", "unit", "gates", "floor"} <= set(e) and e["q"] < 0.10 and e["replicated"] is True
    assert d["evidence"][0]["t"] == "frames"
    for d_ in conf:                                                   # statements are plain second person with numbers
        assert d_["statement"].strip() and "you" in d_["statement"].lower()
    # traits carry numbers
    traits = store.facts(source="derived", limit=50)
    by = {(f["predicate"], f["object_text"]): f for f in traits}
    assert by[("maintenance_kcal", "maintenance")]["value_num"] == pytest.approx(d["effect"]["estimate"], rel=0.01)
    assert any(f["predicate"] == "response_profile" and f["value_num"] is not None for f in traits)
    assert any(f["predicate"] == "recovery_profile" for f in traits)
    n_facts, alphas = len(traits), {f["id"]: f["alpha"] for f in traits}
    # idempotent
    assert miner.run().get("skipped") == "unchanged"
    r3 = miner.run_full()
    assert r3["ran"] and r3["confirmed"] == r1["confirmed"]
    traits2 = store.facts(source="derived", limit=50)
    assert len(traits2) == n_facts and {f["id"]: f["alpha"] for f in traits2} == alphas       # re-testing does not inflate confidence
    assert len(store.discoveries(status=("confirmed",), limit=50)) == len(conf)


def test_run_picks_up_new_data_and_jobs_call_the_exact_names():
    synth.make_athlete(4, 112, synth.default_effects())
    miner.run()
    db.add_checkin(sleep_h=7.0, soreness=2, energy=3)
    assert miner.run().get("ran")                                      # new data -> new hash
    rep = jobs.nightly()
    assert "error" not in rep["miner.run"] and "error" not in rep["forecast.resolve_due"]
    assert "error" not in jobs.weekly()["miner.run_full"]


def test_never_raises(monkeypatch):
    assert isinstance(miner.run(), dict)                               # empty database
    synth.make_athlete(4, 60, None)
    monkeypatch.setattr(frames, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = miner.run(force=True)
    assert "error" in r and "boom" in r["error"]


def _fake(passes: bool, **kw) -> dict:
    base = {"passes": passes, "candidate_ok": False, "gates": {"n": True}, "gates_keep": {"q": False, "size": False, "replicates": False, "plausible": True}}
    base.update(kw)
    return base


def test_status_lifecycle_confirmed_weakening_retired_and_hysteresis():
    prev = {"status": "confirmed", "effect": {"fails": 0}}
    assert miner.decide(_fake(True), None) == "confirmed"
    assert miner.decide(_fake(False), prev) == "weakening"                                        # stopped holding
    keep = _fake(False, gates_keep={"q": True, "size": True, "replicates": True, "plausible": True})
    assert miner.decide(keep, prev) == "confirmed"                                                # a borderline dip does not flip a confirmed pattern
    assert miner.decide(_fake(False), {"status": "weakening", "effect": {"fails": 1}}) == "weakening"
    assert miner.decide(_fake(False), {"status": "weakening", "effect": {"fails": 2}}) == "retired"   # repeated failure
    assert miner.decide(_fake(True), {"status": "retired", "effect": {"fails": 3}}) == "confirmed"    # revived by new evidence
    assert miner.decide(_fake(False), {"status": "retired", "effect": {"fails": 3}}) is None
    assert miner.decide(_fake(False, candidate_ok=True), None) == "candidate"
    assert miner.decide(_fake(False), None) is None


def test_a_confirmed_pattern_that_fades_is_weakened_then_retired_and_its_trait_archived():
    synth.make_athlete(4, 112, synth.default_effects())
    miner.run()
    assert store.discovery_by_key(K_LATE)["status"] == "confirmed"
    assert any(f["object_text"] == "late meals shorten sleep" for f in store.facts(source="derived"))
    with db.conn() as c:                                              # the link vanishes: sleep no longer follows dinner time
        c.execute("UPDATE checkins SET sleep_h = 6.5 + ((id * 37) % 13) / 10.0 WHERE sleep_h IS NOT NULL")
    statuses = []
    for _ in range(3):
        miner.run(force=True)
        statuses.append(store.discovery_by_key(K_LATE)["status"])
    assert statuses[0] == "weakening" and statuses[-1] == "retired"
    assert not any(f["object_text"] == "late meals shorten sleep" for f in store.facts(source="derived", status=("active",)))
    assert store.discoveries(status=("confirmed", "weakening"), family="late_meals_sleep") == []


def test_implausible_direction_is_never_confirmed():
    """If 'more sleep' went with WEAKER lifts, the data alone must not be able to confirm it (capped at candidate)."""
    r = {"family": "sleep_performance", "effect": -1.0, "n": 50, "min_n": 24, "size": 2.0, "floor": 0.6, "q": 0.001, "p": 1e-4, "agree": True, "gate_ci": (-2.0, -0.2),
         "key": "x"}
    miner._gates(r)
    assert not r["passes"] and r["candidate_ok"] and not r["gates"]["plausible"]


def test_gates_require_every_condition():
    ok = {"family": "weekday_adherence", "effect": 0.4, "n": 60, "min_n": 24, "size": 0.4, "floor": 0.2, "q": 0.02, "p": 0.002, "agree": True, "gate_ci": (0.1, 0.7), "key": "x", "span": 100}
    miner._gates(ok)
    assert ok["passes"]
    for patch in ({"q": 0.2}, {"q": 0.05}, {"span": 30}, {"n": 10}, {"size": 0.05}, {"gate_ci": (-0.1, 0.5)}, {"agree": False}):
        r = {**ok, **patch}
        miner._gates(r)
        assert not r["passes"], patch


def test_hypothesis_keys_are_stable_across_runs():
    synth.make_athlete(4, 112, synth.default_effects())
    a = {r["key"] for r in miner.analyze(n_perm=99, n_boot=20)}
    b = {r["key"] for r in miner.analyze(n_perm=99, n_boot=20)}
    assert a == b and all(":" in k for k in a)


def test_rendering_is_plain_language_with_numbers():
    synth.make_athlete(4, 112, synth.default_effects())
    for r in miner.analyze(n_perm=299, n_boot=50):
        if not r["passes"]:
            continue
        t = miner.render(r)
        assert t["title"] and t["statement"].endswith(").") or t["statement"].endswith(".")
        assert any(ch.isdigit() for ch in t["statement"]) and t["consumers"]
        assert "{" not in t["statement"] and not re.search(r"nan", t["statement"].lower())


def _render_input(family, effect, **kw):
    r = {"family": family, "effect": effect, "key": f"{family}:x", "label": "x", "n": 40, "agree": True,
         "passes": True, "lifts": ["squat"], "ci": (0.1, 0.9)}
    r.update(kw)
    return r


def test_soreness_energy_wording_is_direction_consistent():
    # soreness: negative effect -> "less", and the rec says push/hold at HIGH soreness
    s = miner.render(_render_input("soreness_performance", -1.5))
    assert "each extra point of soreness goes with about 1.5% less" in s["statement"]
    assert "4/5 or higher" in s["recommendation"] and "skip max attempts" in s["recommendation"]
    # energy: positive effect -> "more", rec triggers on LOW energy
    e = miner.render(_render_input("energy_performance", 2.0))
    assert "each extra point of energy goes with about 2.0% more" in e["statement"]
    assert "2/5 or lower" in e["recommendation"]
    # no recommendation when the result does not pass the gates
    assert miner.render(_render_input("energy_performance", 2.0, passes=False))["recommendation"] == ""


def test_nightly_peeking_does_not_inflate_false_confirms():
    """The miner re-tests every night on accumulating data. Null athletes looked at every 14 days must (almost) never be confirmed at ANY look.
    (Measured offline on 60 athletes, looks every 3 days: 38% with q<0.10 / no history floor, ~8% with q<0.03 + 42-day floor.)"""
    ever = 0
    n_ath = 4
    for seed in range(7100, 7100 + n_ath):
        synth.make_athlete(seed, 182, None)
        rows, sets = frames.build(182), frames.set_rows(182)
        hit = False
        for k in range(28, 183, 14):
            rr = rows[:k]
            ss = [s for s in sets if s["date"] <= rr[-1]["date"]]
            res = miner.analyze(rows=rr, sets=ss, profile=db.get_profile(), n_perm=99, n_boot=20)
            if k < miner.MIN_SPAN_CONFIRM:
                assert not any(r["passes"] for r in res)        # too little history: nothing can be first-confirmed
            hit = hit or any(r["passes"] for r in res)
        ever += hit
    assert ever <= 1
