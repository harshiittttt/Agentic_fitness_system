"""Statistics for the Insight Engine: numpy only, built for ONE person's daily series, which are autocorrelated, trending, seasonal by weekday and messy.

The one rule: a confident false insight is worse than none. So:
  * p-values come from BLOCK permutations (and a Bartlett effective-sample-size correction), never from the naive iid formula; the reported p is the
    larger (more cautious) of the two;
  * the personal progression trend and weekday effects are removed before anything is correlated (`partial_test` residualizes both variables on a
    piecewise-linear time basis, weekday dummies and any covariates, so "you got stronger over time" is never mistaken for "sleep helps");
  * outliers are winsorized (typos), confidence intervals come from a moving-block bootstrap, effects must replicate in both halves of the history,
    and Benjamini-Hochberg controls the false-discovery rate across every hypothesis tested in a run.
Every test returns effect, CI, p, n (and the effective n).
"""
from __future__ import annotations

import math
from typing import Callable, Sequence

import numpy as np

# minimum number of usable observations before a kind of test is allowed to speak at all (the miner may demand more)
MIN_N = {"corr": 18, "group": 20, "events": 5, "weekly": 10, "ratio": 24, "weekday": 24}


def min_n(kind: str) -> int:
    return MIN_N.get(kind, 20)


# ====================================================== distributions ======================================================
def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = tiny if abs(d) < tiny else d
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-12:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """P(|T| >= |t|) for Student's t with `df` degrees of freedom."""
    if not math.isfinite(t):
        return 0.0
    if df <= 0:
        return 1.0
    return float(min(1.0, betainc(df / 2.0, 0.5, df / (df + t * t))))


def t_ppf_two_sided(level: float, df: float) -> float:
    """Critical value c with P(|T| <= c) = level (bisection; accurate enough for confidence intervals)."""
    lo, hi = 0.0, 60.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if 1.0 - t_two_sided_p(mid, df) < level:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def norm_two_sided_p(z: float) -> float:
    return float(math.erfc(abs(z) / math.sqrt(2.0)))


# ====================================================== basic estimators ======================================================
def _finite_pair(x, y) -> tuple[np.ndarray, np.ndarray]:
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    return x[m], y[m]


def pearson(x, y) -> float:
    """Pearson r over the pairs where both values are known; NaN when undefined."""
    x, y = _finite_pair(x, y)
    if len(x) < 3:
        return float("nan")
    xc, yc = x - x.mean(), y - y.mean()
    den = math.sqrt(float((xc * xc).sum() * (yc * yc).sum()))
    return float((xc * yc).sum() / den) if den > 0 else float("nan")


def rank(x) -> np.ndarray:
    """Average ranks (ties share the mean rank)."""
    x = np.asarray(x, float)
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x))
    r[order] = np.arange(len(x), dtype=float)
    xs = x[order]
    i = 0
    while i < len(x):
        j = i
        while j + 1 < len(x) and xs[j + 1] == xs[i]:
            j += 1
        if j > i:
            r[order[i:j + 1]] = (i + j) / 2.0
        i = j + 1
    return r


def spearman(x, y) -> float:
    x, y = _finite_pair(x, y)
    if len(x) < 3:
        return float("nan")
    return pearson(rank(x), rank(y))


def cohens_d(a, b) -> float:
    """Standardised difference between two groups (pooled SD). NaN if a group has fewer than 2 values."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    sp = math.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1)) / (len(a) + len(b) - 2))
    return float((a.mean() - b.mean()) / sp) if sp > 0 else float("nan")


def standardized_effect(slope: float, sd_x: float, sd_y: float) -> float:
    """Slope expressed in SDs of y per SD of x (equals r for a simple regression)."""
    return float(slope * sd_x / sd_y) if sd_y > 0 else float("nan")


def theil_sen(t, y) -> tuple[float, float]:
    """Robust line: median of pairwise slopes; intercept = median(y - slope * t). (slope, intercept). Needs >= 2 distinct t."""
    t, y = _finite_pair(t, y)
    if len(t) < 2:
        return 0.0, float(np.median(y)) if len(y) else 0.0
    i, j = np.triu_indices(len(t), 1)
    dt = t[j] - t[i]
    ok = dt != 0
    if not ok.any():
        return 0.0, float(np.median(y))
    s = float(np.median((y[j][ok] - y[i][ok]) / dt[ok]))
    return s, float(np.median(y - s * t))


def winsorize(x, k: float = 4.0) -> np.ndarray:
    """Clip values further than k robust SDs (MAD) from the median: tames typos without throwing data away. Binary/constant columns are untouched."""
    x = np.asarray(x, float).copy()
    f = np.isfinite(x)
    if f.sum() < 5:
        return x
    med = np.median(x[f])
    mad = np.median(np.abs(x[f] - med)) * 1.4826
    if mad <= 0:
        return x
    x[f] = np.clip(x[f], med - k * mad, med + k * mad)
    return x


def acf(x, nlags: int) -> np.ndarray:
    """Sample autocorrelation at lags 1..nlags (zeros when the series is constant)."""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)] - np.nanmean(x)
    n = len(x)
    den = float((x * x).sum())
    out = np.zeros(nlags)
    if n < 4 or den <= 0:
        return out
    for k in range(1, min(nlags, n - 2) + 1):
        out[k - 1] = float((x[:-k] * x[k:]).sum() / den)
    return out


# ====================================================== bases: trend, weekday, residualising ======================================================
def trend_basis(t, knots_every: float = 21.0, kmax: int = 7) -> np.ndarray:
    """Piecewise-linear (hat-function) time basis, columns sum to 1 (so it also carries the intercept). Flexible enough to follow progression, a
    plateau or a bulk/cut phase, stiff enough not to swallow day-to-day effects."""
    t = np.asarray(t, float)
    span = float(np.nanmax(t) - np.nanmin(t)) if len(t) else 0.0
    k = int(np.clip(round(span / knots_every) + 1, 2, kmax))
    knots = np.linspace(np.nanmin(t), np.nanmax(t), k) if span > 0 else np.array([0.0, 1.0])
    cols = []
    for j in range(k):
        e = np.zeros(k)
        e[j] = 1.0
        cols.append(np.interp(t, knots, e))
    return np.column_stack(cols)


def dow_dummies(dow) -> np.ndarray:
    """Six weekday indicator columns (Monday is the reference)."""
    dow = np.asarray(dow, int)
    return np.column_stack([(dow == d).astype(float) for d in range(1, 7)])


def residualize(y, X: np.ndarray | None) -> np.ndarray:
    """Residuals of y after least-squares on the columns of X (X should include an intercept or a trend basis)."""
    y = np.asarray(y, float)
    if X is None or X.size == 0:
        return y - y.mean()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return y - X @ beta


def detrend(y, t, dow=None, knots_every: float = 21.0) -> tuple[np.ndarray, np.ndarray]:
    """(residual, fitted) of y on the personal time trend (+ weekday effects when `dow` is given). NaNs stay NaN and do not enter the fit."""
    y = np.asarray(y, float)
    t = np.asarray(t, float)
    m = np.isfinite(y)
    cols = [trend_basis(t, knots_every)]
    if dow is not None:
        cols.append(dow_dummies(dow))
    X = np.column_stack(cols)
    res = np.full(len(y), np.nan)
    fit = np.full(len(y), np.nan)
    if m.sum() > X.shape[1] + 2:
        beta, *_ = np.linalg.lstsq(X[m], y[m], rcond=None)
        fit = X @ beta
        res[m] = y[m] - fit[m]
    return res, fit


# ====================================================== block permutation and bootstrap ======================================================
def auto_block(*series, cap_frac: float = 0.25) -> int:
    """Block length from how long the series stay correlated: 1.5 x the first lag where every series' autocorrelation is within noise, at least 3."""
    n = min(len(np.asarray(s)) for s in series)
    thr = 1.5 / math.sqrt(max(n, 1))
    k = 1
    for s in series:
        a = acf(s, min(15, max(2, n // 3)))
        lag = 1
        for i, v in enumerate(a):
            if abs(v) >= thr:
                lag = i + 1
        k = max(k, lag + 1)
    return int(np.clip(round(1.5 * k), 3, max(3, int(n * cap_frac))))


def block_perm_idx(n: int, b: int, n_perm: int, rng: np.random.Generator) -> np.ndarray:
    """(n_perm, n) index matrix: each row is the series cut into blocks of length b at a random phase, the blocks shuffled (circularly padded)."""
    b = int(max(1, min(b, n)))
    nb = int(math.ceil(n / b))
    order = np.argsort(rng.random((n_perm, nb)), axis=1)
    idx = (order[:, :, None] * b + np.arange(b)[None, None, :]).reshape(n_perm, nb * b)[:, :n]
    shift = rng.integers(0, n, size=(n_perm, 1))
    return (idx + shift) % n


def _corr_rows(Xp: np.ndarray, y: np.ndarray) -> np.ndarray:
    Xc = Xp - Xp.mean(axis=1, keepdims=True)
    yc = y - y.mean()
    sx = np.sqrt((Xc * Xc).sum(axis=1))
    sy = math.sqrt(float((yc * yc).sum()))
    with np.errstate(invalid="ignore", divide="ignore"):
        r = (Xc @ yc) / (sx * sy)
    return np.nan_to_num(r)


def permutation_p(x, y, *, block: int | None = None, n_perm: int = 1999, seed: int = 0) -> tuple[float, float]:
    """Two-sided BLOCK-permutation p-value for the correlation of x and y, plus r. x is cut into blocks and re-ordered, which keeps its
    autocorrelation but breaks its alignment with y; the null distribution of r therefore has the right (wider) spread for autocorrelated data."""
    x, y = _finite_pair(x, y)
    n = len(x)
    if n < 5 or np.std(x) == 0 or np.std(y) == 0:
        return 1.0, float("nan")
    b = block or auto_block(x, y)
    rng = np.random.default_rng(seed)
    r = pearson(x, y)
    rp = _corr_rows(x[block_perm_idx(n, b, n_perm, rng)], y)
    return float((1 + np.sum(np.abs(rp) >= abs(r) - 1e-12)) / (n_perm + 1)), r


def effective_n(x, y, max_lag: int | None = None) -> float:
    """Bartlett's effective sample size for the correlation of two autocorrelated series: n / (1 + 2 sum rho_x(k) rho_y(k))."""
    x, y = _finite_pair(x, y)
    n = len(x)
    if n < 8:
        return float(n)
    K = max_lag or min(n // 4, 12)
    s = float(np.sum(acf(x, K) * acf(y, K)))
    return float(np.clip(n / max(1e-9, 1.0 + 2.0 * s), max(4.0, 0.15 * n), n))


def bootstrap_ci(x, y, fn: Callable[[np.ndarray, np.ndarray], float], *, block: int | None = None, n_boot: int = 400, level: float = 0.95,
                 seed: int = 0) -> tuple[float, float]:
    """Moving-block bootstrap percentile CI of fn(x, y) on paired rows (rows resampled in blocks so autocorrelation is respected)."""
    x, y = _finite_pair(x, y)
    n = len(x)
    if n < 6:
        return float("nan"), float("nan")
    b = int(block or auto_block(x, y))
    rng = np.random.default_rng(seed)
    nb = int(math.ceil(n / b))
    starts = rng.integers(0, max(1, n - b + 1), size=(n_boot, nb))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(n_boot, nb * b)[:, :n]
    vals = np.array([fn(x[i], y[i]) for i in idx], dtype=float)
    vals = vals[np.isfinite(vals)]
    if len(vals) < 20:
        return float("nan"), float("nan")
    a = (1 - level) / 2
    return float(np.quantile(vals, a)), float(np.quantile(vals, 1 - a))


def _slope(x: np.ndarray, y: np.ndarray) -> float:
    xc = x - x.mean()
    den = float((xc * xc).sum())
    return float((xc * (y - y.mean())).sum() / den) if den > 0 else float("nan")


def stratified_labels(labels: np.ndarray, strata: np.ndarray, n_perm: int, rng: np.random.Generator) -> np.ndarray:
    """(n_perm, n) label matrix where labels are shuffled only WITHIN each stratum (e.g. weekday labels shuffled within each calendar week, which is
    exactly the exchangeability a 'no weekday effect' null implies and keeps week-to-week streaks intact)."""
    n = len(labels)
    out = np.empty((n_perm, n), dtype=labels.dtype)
    for s in np.unique(strata):
        ix = np.where(strata == s)[0]
        keys = rng.random((n_perm, len(ix)))
        out[:, ix] = labels[ix][np.argsort(keys, axis=1)]
    return out


# ====================================================== the workhorse: partial association test ======================================================
def partial_test(x, y, nuisance: np.ndarray | None = None, *, n_perm: int = 1999, n_boot: int = 400, seed: int = 0, level: float = 0.95,
                 robust: str = "winsor", block: int | None = None, min_half: int = 8) -> dict:
    """Does x predict y once the nuisance columns (time trend, weekday, other covariates) are accounted for?

    x, y: aligned 1-D arrays in time order (NaN allowed; rows with any NaN in x, y or nuisance are dropped). `nuisance`: (n, k) matrix; give it an
    intercept or a trend basis. robust: "winsor" (default), "rank" (Spearman-type) or "none".
    Returns {ok, n, n_eff, slope (y units per x unit), r, p, p_perm, p_analytic, ci (slope), sd_x, sd_y, halves: [(slope, n), (slope, n)], agree, block}.
    `p` is the LARGER of the block-permutation p and the Bartlett-corrected t-test p.
    """
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    if nuisance is not None:
        nuisance = np.asarray(nuisance, float)
        m &= np.all(np.isfinite(nuisance), axis=1)
    n = int(m.sum())
    out: dict = {"ok": False, "n": n}
    if n < 8:
        return out
    xs, ys = x[m], y[m]
    N = nuisance[m] if nuisance is not None else None
    k = 0 if (N is None or not N.size) else int(np.linalg.matrix_rank(N))
    if robust == "rank":
        xs, ys = rank(xs), rank(ys)
    xr, yr = residualize(xs, N), residualize(ys, N)
    if robust == "winsor":
        yr = winsorize(yr)
        xr = winsorize(xr)
    if np.std(xr) < 1e-12 or np.std(yr) < 1e-12 or n - k < 8:
        return out
    r = pearson(xr, yr)
    slope = _slope(xr, yr)
    b = block or auto_block(xr, yr)
    p_perm, _ = permutation_p(xr, yr, block=b, n_perm=n_perm, seed=seed)
    n_eff = effective_n(xr, yr)
    df = max(1.0, n_eff - k - 2)
    tstat = r * math.sqrt(df / max(1e-12, 1 - r * r))
    p_an = t_two_sided_p(tstat, df)
    ci_b = bootstrap_ci(xr, yr, _slope, block=b, n_boot=n_boot, level=level, seed=seed + 1)
    se = abs(slope) / abs(tstat) if tstat != 0 else float("inf")
    tc = t_ppf_two_sided(level, df)
    ci_a = (slope - tc * se, slope + tc * se)
    lo = min(ci_b[0], ci_a[0]) if np.isfinite(ci_b[0]) else ci_a[0]
    hi = max(ci_b[1], ci_a[1]) if np.isfinite(ci_b[1]) else ci_a[1]
    h = n // 2
    halves = [(_slope(xr[:h], yr[:h]) if h >= min_half else float("nan"), h), (_slope(xr[h:], yr[h:]) if n - h >= min_half else float("nan"), n - h)]
    out.update(ok=True, n_eff=n_eff, slope=slope, r=r, p=float(max(p_perm, p_an)), p_perm=p_perm, p_analytic=p_an, ci=(float(lo), float(hi)),
               sd_x=float(np.std(xs)), sd_y=float(np.std(ys)), halves=halves, block=b, k_nuis=k, df=df,
               d=float(2 * r / math.sqrt(max(1e-12, 1 - r * r))))     # d: standardised effect (Cohen's d equivalent of r)
    out["agree"] = split_half_agree(slope, [halves[0][0], halves[1][0]])
    return out


def split_half_agree(effect: float, halves: Sequence[float], min_frac: float = 0.25) -> bool:
    """The direction replicates: both halves of the history have the same sign as the full-sample effect and at least `min_frac` of its size."""
    if not np.isfinite(effect) or effect == 0:
        return False
    for h in halves:
        if h is None or not np.isfinite(h) or np.sign(h) != np.sign(effect) or abs(h) < min_frac * abs(effect):
            return False
    return True


def bh_fdr(pvals: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (q-values). NaN p-values get q = 1."""
    p = np.array([1.0 if (v is None or not np.isfinite(v)) else float(v) for v in pvals])
    m = len(p)
    if m == 0:
        return []
    order = np.argsort(p)
    ranked = p[order] * m / (np.arange(m) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.clip(q, 0, 1)
    return [float(v) for v in out]
