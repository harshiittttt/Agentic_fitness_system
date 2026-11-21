"""stats.py against known cases: estimators, distributions, BH, detrending, and above all the calibration of the p-values on autocorrelated null series."""
from __future__ import annotations

import math

import numpy as np
import pytest

from fitness_os.mem import stats


def ar1(rng: np.random.Generator, n: int, phi: float) -> np.ndarray:
    x = np.zeros(n)
    e = rng.normal(size=n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


# ---------------------------------------------------------------- estimators
def test_pearson_and_spearman_on_known_cases():
    x = np.arange(1, 21, dtype=float)
    assert stats.pearson(x, 2 * x + 3) == pytest.approx(1.0)
    assert stats.spearman(x, np.exp(x)) == pytest.approx(1.0)            # monotone but not linear
    assert stats.pearson(x, np.exp(x)) < 0.9
    assert stats.pearson(x, -x) == pytest.approx(-1.0)
    y = x.copy()
    y[3] = np.nan                                                          # pairwise deletion
    assert stats.pearson(x, y) == pytest.approx(1.0)
    assert math.isnan(stats.pearson([1, 2], [1, 2]))                       # too few


def test_rank_averages_ties():
    assert list(stats.rank([10, 20, 20, 30])) == [0.0, 1.5, 1.5, 3.0]


def test_cohens_d_and_standardized_effect():
    a, b = np.array([5.0, 6, 7, 8, 9]), np.array([3.0, 4, 5, 6, 7])
    assert stats.cohens_d(a, b) == pytest.approx(2.0 / math.sqrt(2.5))
    assert stats.standardized_effect(2.0, 0.5, 1.0) == pytest.approx(1.0)


def test_theil_sen_ignores_outliers():
    t = np.arange(20, dtype=float)
    y = 2.0 * t + 1.0
    y[5] += 200
    y[17] -= 150
    b, a = stats.theil_sen(t, y)
    assert b == pytest.approx(2.0, abs=0.05) and a == pytest.approx(1.0, abs=0.5)


def test_winsorize_clips_typos_not_data():
    x = np.concatenate([np.random.default_rng(0).normal(0, 1, 50), [40.0]])
    w = stats.winsorize(x)
    assert w.max() < 6 and np.allclose(w[:50], x[:50])
    b = np.array([0.0, 1.0] * 10)
    assert np.array_equal(stats.winsorize(b), b)                           # binary columns are untouched


# ---------------------------------------------------------------- distributions and FDR
def test_t_and_beta_known_values():
    assert stats.betainc(2.0, 3.0, 0.5) == pytest.approx(0.6875)
    assert stats.t_two_sided_p(2.228, 10) == pytest.approx(0.05, abs=1e-3)
    assert stats.t_two_sided_p(1.96, 1e6) == pytest.approx(0.05, abs=1e-3)
    assert stats.t_ppf_two_sided(0.95, 10) == pytest.approx(2.228, abs=2e-3)
    assert stats.norm_two_sided_p(1.96) == pytest.approx(0.05, abs=1e-3)


def test_benjamini_hochberg():
    q = stats.bh_fdr([0.01, 0.04, 0.03, 0.005])
    assert q == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert stats.bh_fdr([]) == []
    assert stats.bh_fdr([float("nan"), 0.5])[0] == 1.0
    # monotone: a larger p never gets a smaller q
    p = np.random.default_rng(1).random(40)
    qs = np.array(stats.bh_fdr(p))
    assert np.all(np.diff(qs[np.argsort(p)]) >= -1e-12)


def test_min_n_rules_exist():
    assert stats.min_n("corr") >= 10 and stats.min_n("events") >= 3 and stats.min_n("unknown") > 0


# ---------------------------------------------------------------- bases and detrending
def test_trend_basis_sums_to_one_and_follows_a_plateau():
    t = np.arange(84, dtype=float)
    B = stats.trend_basis(t)
    assert np.allclose(B.sum(axis=1), 1.0)
    y = np.where(t < 40, 0.5 * t, 20.0)                                    # progress then a plateau
    res, fit = stats.detrend(y, t)
    assert np.abs(res).max() < 2.5


def test_detrend_removes_progression_and_weekday_rhythm():
    rng = np.random.default_rng(3)
    t = np.arange(112, dtype=float)
    dow = (t % 7).astype(int)
    y = 0.4 * t + np.array([0, 3, 0, -2, 0, 0, 1])[dow] + rng.normal(0, 0.3, len(t))
    res, _ = stats.detrend(y, t, dow)
    assert abs(np.polyfit(t, res, 1)[0]) < 0.01 and res.std() < 0.5


def test_detrend_keeps_nans_out_of_the_fit():
    t = np.arange(60, dtype=float)
    y = 2 * t + 1.0
    y[::3] = np.nan
    res, fit = stats.detrend(y, t)
    assert np.isnan(res[0]) and np.nanmax(np.abs(res)) < 1e-6


def test_split_half_agree():
    assert stats.split_half_agree(1.0, [0.8, 1.3])
    assert not stats.split_half_agree(1.0, [0.8, -0.1])                    # one half flips
    assert not stats.split_half_agree(1.0, [0.05, 1.0])                    # one half is negligible
    assert not stats.split_half_agree(1.0, [float("nan"), 1.0])
    assert not stats.split_half_agree(0.0, [1, 1])


def test_block_permutation_indices_are_valid():
    rng = np.random.default_rng(0)
    idx = stats.block_perm_idx(50, 7, 20, rng)
    assert idx.shape == (20, 50) and idx.min() >= 0 and idx.max() < 50
    assert len({tuple(r) for r in idx}) == 20


def test_stratified_labels_shuffle_only_within_strata():
    rng = np.random.default_rng(0)
    labels = np.array([0, 1, 2, 0, 1, 2, 0, 1, 2])
    strata = np.array([0, 0, 0, 1, 1, 1, 2, 2, 2])
    L = stats.stratified_labels(labels, strata, 50, rng)
    for row in L:
        for s in (0, 1, 2):
            assert sorted(row[strata == s]) == [0, 1, 2]


# ---------------------------------------------------------------- the partial test
def test_progression_is_not_mistaken_for_an_effect():
    """x and y both just 'get bigger over time': naively they correlate at ~0.9, after removing the trend there is nothing."""
    rng = np.random.default_rng(5)
    t = np.arange(90, dtype=float)
    x = 0.05 * t + ar1(rng, 90, 0.3) * 0.5
    y = 0.8 * t + ar1(rng, 90, 0.3) * 4
    assert stats.pearson(x, y) > 0.8
    r = stats.partial_test(x, y, stats.trend_basis(t), n_perm=499, n_boot=50, seed=1)
    assert r["ok"] and r["p"] > 0.05 and abs(r["r"]) < 0.4


def test_a_planted_effect_is_found_with_the_right_size():
    rng = np.random.default_rng(6)
    n = 90
    t = np.arange(n, dtype=float)
    x = ar1(rng, n, 0.3)
    y = 0.3 * t + 0.5 * x + rng.normal(0, 0.6, n)
    r = stats.partial_test(x, y, stats.trend_basis(t), n_perm=999, n_boot=200, seed=2)
    assert r["p"] < 0.01 and r["ci"][0] < 0.5 < r["ci"][1] and r["agree"]
    assert r["n"] == n and 0 < r["n_eff"] <= n


def test_missing_values_and_nuisance_columns_are_handled():
    rng = np.random.default_rng(7)
    n = 80
    t = np.arange(n, dtype=float)
    x, y = rng.normal(size=n), rng.normal(size=n)
    x[::7] = np.nan
    cov = rng.normal(size=n)
    cov[::11] = np.nan
    r = stats.partial_test(x, y, np.column_stack([stats.trend_basis(t), cov]), n_perm=199, n_boot=30)
    assert r["ok"] and r["n"] < n
    assert not stats.partial_test(np.ones(30), rng.normal(size=30), None)["ok"]     # constant x: no test


def test_p_values_are_calibrated_on_autocorrelated_nulls_and_naive_ones_are_not():
    """The whole point of block permutation: independent AR(1) series, p < 0.05 about 5% of the time (never wildly more); the textbook t-test lies."""
    rng = np.random.default_rng(11)
    reps, n = 300, 60
    ours = naive = 0
    for _ in range(reps):
        x, y = ar1(rng, n, 0.85), ar1(rng, n, 0.85)
        r = stats.partial_test(x, y, stats.trend_basis(np.arange(n, dtype=float)), n_perm=199, n_boot=20, seed=int(rng.integers(1 << 30)))
        ours += r["p"] < 0.05
        rr = stats.pearson(x, y)
        naive += stats.t_two_sided_p(rr * math.sqrt((n - 2) / (1 - rr * rr)), n - 2) < 0.05
    assert ours / reps < 0.09                       # near or below the nominal 5%
    assert naive / reps > 0.25                      # the naive test calls autocorrelated noise "significant" a quarter of the time or more


def test_bootstrap_ci_covers_the_true_slope_most_of_the_time():
    rng = np.random.default_rng(13)
    hit = 0
    for _ in range(60):
        n = 70
        x = ar1(rng, n, 0.4)
        y = 0.7 * x + ar1(rng, n, 0.4)
        lo, hi = stats.bootstrap_ci(x, y, stats._slope, n_boot=150, seed=int(rng.integers(1 << 30)))
        hit += lo <= 0.7 <= hi
    assert hit / 60 > 0.8
