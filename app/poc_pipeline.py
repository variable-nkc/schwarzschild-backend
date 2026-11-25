"""PoC analytics pipeline, ported for the R&D page (2026-11-17).

A faithful port of the seqbayes PoC engine (dan/poc:
src/seqbayes/{geometry,series,forecast,anomaly,temperature,
tfidf,adapters,config,api}.py) so the v0.1 R&D page can render
the original PoC dashboard figures against the SAME source
data as every other live component: records are read from the
DDA sidecar's `dda` table (iso1, iso2, obs_date, cameo_code,
obs_count — exactly the PSV record shape the PoC app consumed)
and the crosswalk from DDA_SIMPLEXES_JSON (cameo_tokens.json).

Port notes:
- numpy-only, per this backend's standing discipline (see
  requirements.txt); the PoC's pandas groupby/merge steps are
  re-expressed as dict/array accumulations with identical
  arithmetic.
- The set-share block is EXCLUDED (Dan, 2026-11-17): the
  vetted D17/D18 version lives in Dig/Vet. A "setshare" key in
  the request body is accepted and ignored.
- A "volume_regime" block is added (gallery S10, Dan,
  2026-11-17): pooled 7d volume vs its trailing-90d baseline
  distribution, computed from the dyad's ALL-records daily
  volume (the gallery's daily_frame sums unmapped codes too,
  unlike records_to_daily's mapped-only W).
- JSON cleaning recurses lists as well as dicts (the PoC's
  _clean predates real JSON transport); NaN -> None
  everywhere, which the frontend maps back to NaN exactly as
  the PoC dashboard's _arr did.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import numpy as np

# ---------------------------------------------------------------------------
# geometry (port of seqbayes/geometry.py)
# ---------------------------------------------------------------------------


def closure(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    s = x.sum(axis=-1, keepdims=True)
    if np.any(s <= 0):
        raise ValueError("closure: non-positive row sum encountered")
    return x / s


def multiplicative_zero_replace(x: np.ndarray,
                                delta: float = 5e-3) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = x.copy()
    zero = out == 0
    n_zero = zero.sum(axis=-1, keepdims=True)
    out = np.where(zero, delta, out * (1.0 - n_zero * delta))
    return closure(out)


def weighted_geometric_mean(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    w = np.asarray(w, dtype=float)
    if np.any(x <= 0):
        raise ValueError("weighted_geometric_mean: x must be > 0")
    wsum = w.sum()
    if wsum <= 0:
        raise ValueError("weighted_geometric_mean: weights must sum > 0")
    log_mean = (w[:, None] * np.log(x)).sum(axis=0) / wsum
    return closure(np.exp(log_mean - log_mean.max()))


def ilr_basis(d: int) -> np.ndarray:
    helm = np.zeros((d, d - 1))
    for j in range(d - 1):
        helm[: j + 1, j] = 1.0
        helm[j + 1, j] = -(j + 1)
    helm = helm / np.sqrt((np.arange(d - 1) + 1) * (np.arange(d - 1) + 2))
    return helm


def _clr(x: np.ndarray) -> np.ndarray:
    lx = np.log(x)
    return lx - lx.mean(axis=-1, keepdims=True)


def ilr(x: np.ndarray, v: np.ndarray | None = None) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if np.any(x <= 0):
        raise ValueError("ilr: x must be strictly positive")
    d = x.shape[-1]
    if v is None:
        v = ilr_basis(d)
    return _clr(x) @ v


def ilr_inv(z: np.ndarray, v: np.ndarray | None = None) -> np.ndarray:
    z = np.asarray(z, dtype=float)
    d = z.shape[-1] + 1
    if v is None:
        v = ilr_basis(d)
    return closure(np.exp(z @ v.T))


def aitchison_distance(x: np.ndarray, y: np.ndarray,
                       v: np.ndarray | None = None) -> np.ndarray:
    return np.linalg.norm(ilr(x, v) - ilr(y, v), axis=-1)


# ---------------------------------------------------------------------------
# series (port of seqbayes/series.py)
# ---------------------------------------------------------------------------


def half_life_to_lam(half_life_days: float) -> float:
    if half_life_days <= 0:
        raise ValueError("half-life must be positive")
    return 0.5 ** (1.0 / half_life_days)


def day_weights(w_raw: np.ndarray, w_cap: float | None = None,
                w_alpha: float = 1.0) -> np.ndarray:
    w = np.asarray(w_raw, dtype=float)
    if np.any(w < 0):
        raise ValueError("day_weights: W must be non-negative")
    if w_cap is not None:
        if w_cap <= 0:
            raise ValueError("day_weights: w_cap must be positive")
        w = np.minimum(w, w_cap)
    if w_alpha != 1.0:
        w = w**w_alpha
    return w


def sequential_posteriors(
    daily: np.ndarray,
    w_raw: np.ndarray,
    half_life_days: float,
    w_cap: float | None = None,
    w_alpha: float = 1.0,
    zero_delta: float = 5e-3,
) -> dict:
    D = np.asarray(daily, dtype=float)
    W = np.asarray(w_raw, dtype=float)
    if D.ndim != 2 or W.ndim != 1 or D.shape[0] != W.shape[0]:
        raise ValueError("shapes: daily (T,K), w_raw (T,)")
    t, k = D.shape
    lam = half_life_to_lam(half_life_days)
    w = day_weights(W, w_cap=w_cap, w_alpha=w_alpha)
    log_accum = np.zeros(k)
    n_eff = 0.0
    S = np.empty_like(D)
    N = np.empty(t)
    for i in range(t):
        log_accum *= lam
        n_eff *= lam
        if w[i] > 0:
            d = D[i]
            if not np.all(np.isfinite(d)):
                raise ValueError(
                    f"non-finite daily simplex on day {i} with w>0")
            d = multiplicative_zero_replace(closure(d), delta=zero_delta)
            log_accum += w[i] * np.log(d)
            n_eff += w[i]
        if n_eff > 0:
            v = log_accum / n_eff
            S[i] = closure(np.exp(v - v.max()))
        else:
            S[i] = np.full(k, 1.0 / k)
        N[i] = n_eff
    return {"posterior": S, "n_eff": N, "lam": lam, "weights": w}


# ---------------------------------------------------------------------------
# forecast (port of seqbayes/forecast.py)
# ---------------------------------------------------------------------------

FC_DEFAULTS = {
    "trend_window": 90,
    "err_window": 180,
    "vol_window": 14,
    "regime_window": 180,
    "tau": None,
    "quadratic": False,
    "damp_tau": None,
    "n_samples": 500,
    "seed": 0,
    "min_days": 20,
    "gamma": 0.25,
}


def _check_history(Z: np.ndarray, need: int, what: str) -> None:
    if len(Z) < need:
        raise ValueError(
            f"{what}: need >= {need} days of history, got {len(Z)}")


def _wls_trend(Z: np.ndarray, w: np.ndarray, degree: int = 1):
    L, d = Z.shape
    s = np.arange(L, dtype=float)
    X = np.vander(s, degree + 1, increasing=True)
    wn = w / w.sum() * L
    sw = np.sqrt(wn)
    coef, *_ = np.linalg.lstsq(X * sw[:, None], Z * sw[:, None],
                               rcond=None)
    resid = Z - X @ coef
    dof = max(L - (degree + 1), 1)
    var = (wn[:, None] * resid**2).sum(axis=0) / dof
    return coef, var


def _neff_scale(n_eff_now: float, n_eff_ref: float) -> float:
    if n_eff_ref <= 0:
        return 1.0
    return float(np.clip(n_eff_ref / max(n_eff_now, 1e-9), 1.0, 4.0))


def f0_persistence(Z: np.ndarray, horizons: list[int], p: dict) -> dict:
    _check_history(Z, max(horizons) + p["min_days"], "F0p")
    mu = Z[-1].copy()
    W = min(p["err_window"], len(Z) - max(horizons))
    base = Z[-(W + max(horizons)):]
    out = {}
    for h in horizons:
        errs = base[h:] - base[:-h]
        out[h] = {"mu": mu, "var": errs.var(axis=0)}
    return out


def f0_mean(Z: np.ndarray, horizons: list[int], p: dict) -> dict:
    L = min(p["trend_window"], len(Z))
    _check_history(Z, max(horizons) + L, "F0m")
    mu = Z[-L:].mean(axis=0)
    W = min(p["err_window"], len(Z) - max(horizons) - L + 1)
    errs_all = []
    for t in range(len(Z) - max(horizons) - W, len(Z) - max(horizons)):
        m = Z[t - L + 1: t + 1].mean(axis=0)
        errs_all.append(Z[t + 1: t + 1 + max(horizons)] - m)
    errs_all = np.array(errs_all)
    out = {}
    for i, h in enumerate(horizons):
        out[h] = {"mu": mu, "var": errs_all[:, i, :].var(axis=0)}
    return out


def f1_trend(Z: np.ndarray, w: np.ndarray, n_eff: np.ndarray,
             horizons: list[int], p: dict) -> dict:
    L = min(p["trend_window"], len(Z))
    _check_history(Z, p["min_days"], "F1")
    Zw, ww = Z[-L:], w[-L:]
    ww = np.where(ww <= 0, 1e-9, ww)
    deg = 2 if p["quadratic"] else 1
    coef, rvar = _wls_trend(Zw, ww, degree=deg)
    tau = p["tau"] or L
    damp_tau = p["damp_tau"] or L
    s_last = L - 1
    scale_n = _neff_scale(n_eff[-1], np.median(n_eff[-L:]))
    out = {}
    for h in horizons:
        s = s_last + h
        mu = coef[0] + coef[1] * s
        if deg == 2:
            mu = mu + coef[2] * s**2 / (1.0 + s / damp_tau)
        var = rvar * (1.0 + h / tau) * scale_n
        out[h] = {"mu": mu, "var": var}
    return out


def _regime_states(Z: np.ndarray, p: dict,
                   uniq_daily: np.ndarray | None = None):
    Wv = p["vol_window"]
    step = np.linalg.norm(np.diff(Z, axis=0), axis=1)
    v = np.array([step[max(0, i - Wv + 1): i + 1].std()
                  for i in range(len(step))])
    v = np.concatenate([[v[0]], v])
    Wr = min(p["regime_window"], len(v))
    thr = np.median(v[-Wr:])
    if uniq_daily is not None:
        u = uniq_daily[-Wr:]
        slope = np.polyfit(np.arange(Wr), u, 1)[0] if Wr > 2 else 0.0
        su = u.std()
        z = slope / su if su > 0 else 0.0
        mult = float(np.clip(1.0 - p["gamma"] * np.clip(z, -2, 2),
                             0.5, 1.5))
        thr = thr * mult
    agit = v > thr
    r = agit[-Wr:].astype(int)
    counts = np.ones((2, 2))
    for a, b in zip(r[:-1], r[1:]):
        counts[a, b] += 1
    P = counts / counts.sum(axis=1, keepdims=True)
    return agit, P, thr


def f2_regime(Z: np.ndarray, w: np.ndarray, n_eff: np.ndarray,
              horizons: list[int], p: dict,
              uniq_daily: np.ndarray | None = None) -> dict:
    L = min(p["trend_window"], len(Z))
    _check_history(Z, p["min_days"], "F2")
    agit, P, thr = _regime_states(Z, p, uniq_daily)
    r0 = int(agit[-1])
    reg = {}
    for r in (0, 1):
        idx = np.where(~agit[-L:] if r == 0 else agit[-L:])[0]
        if len(idx) < 10:
            idx = np.arange(L)
        Zr, wr = Z[-L:][idx], w[-L:][idx]
        wr = np.where(wr <= 0, 1e-9, wr)
        coef, rvar = _wls_trend(Zr, wr, degree=1)
        reg[r] = (coef, rvar)
    tau = p["tau"] or L
    scale_n = _neff_scale(n_eff[-1], np.median(n_eff[-L:]))
    s_last = L - 1
    pi0 = np.zeros(2)
    pi0[r0] = 1.0
    out = {}
    for h in horizons:
        pi_h = pi0 @ np.linalg.matrix_power(P, h)
        mus, vars_ = [], []
        for r in (0, 1):
            coef, rvar = reg[r]
            s = s_last + h
            mus.append(coef[0] + coef[1] * s)
            vars_.append(rvar * (1.0 + h / tau) * scale_n)
        mus = np.array(mus)
        vars_ = np.array(vars_)
        mu = (pi_h[:, None] * mus).sum(axis=0)
        var = (pi_h[:, None] * (vars_ + (mus - mu) ** 2)).sum(axis=0)
        p_change = 1.0 - float(np.linalg.matrix_power(P, h)[r0, r0])
        out[h] = {"mu": mu, "var": var, "p_change": p_change,
                  "regime_now": "agitated" if r0 else "calm",
                  "regime_dist": pi_h.tolist()}
    return out


def _expand_models(models: list[str]) -> list[str]:
    out = []
    for m in models:
        if m == "F0":
            out.extend(["F0p", "F0m"])
        else:
            out.append(m)
    return out


#: Composite forecast models (Dan, 2026-11-25; R&D surface —
#: POOL is also served by the workflow hybrid engine).
#: POOL = equal-weight pool of F1 and F2. ANCH05 / ANCHBT =
#: the trailing-mean anchor (F0m) plus a λ share of the
#: pooled F1+F2 deviation from it — ANCH05 with λ = 0.5
#: declared as a judgement, ANCHBT with λ estimated per
#: dyad and horizon by rolling-origin backtest (research-
#: grade). A composite's per-origin (mu, var) is the
#: weighted mixture of its components' Gaussians — the
#: same total-variance rule as the cross-origin mixture,
#: so component disagreement widens the bands.
COMPOSITE_MODELS = ("POOL", "ANCH05", "ANCHBT")


def _pool_raw(Zk, wk, nk, horizons, p):
    """Per-origin raw for POOL: F1 and F2 mixed per horizon
    with equal weights (mixture mean / total variance)."""
    r1 = f1_trend(Zk, wk, nk, horizons, p)
    r2 = f2_regime(Zk, wk, nk, horizons, p)
    out = {}
    for h in horizons:
        mus = np.array([r1[h]["mu"], r2[h]["mu"]])
        vrs = np.array([r1[h]["var"], r2[h]["var"]])
        out[h] = {"mu": mus.mean(axis=0),
                  "var": vrs.mean(axis=0) + mus.var(axis=0)}
    return out


def _anchored_raw(Zk, wk, nk, horizons, p, lam_by_h):
    """Per-origin raw for the anchored composites: the
    (1−λ, λ) mixture of the F0m anchor and the F1+F2 pool,
    per horizon. The mixture mean is exactly anchor +
    λ·(pool − anchor) — the shrinkage form."""
    ra = f0_mean(Zk, horizons, p)
    rp = _pool_raw(Zk, wk, nk, horizons, p)
    out = {}
    for h in horizons:
        lam = lam_by_h[h]
        mu = (1.0 - lam) * ra[h]["mu"] + lam * rp[h]["mu"]
        var = ((1.0 - lam) * ra[h]["var"] + lam * rp[h]["var"]
               + lam * (1.0 - lam)
               * (rp[h]["mu"] - ra[h]["mu"]) ** 2)
        out[h] = {"mu": mu, "var": var}
    return out


def _estimate_lambda(Z, w, n_eff, h, p):
    """Backtest-estimated anchoring weight for one horizon
    (ANCHBT; research-grade first cut, Dan, 2026-11-25).

    Rolling origins inside the dyad's own history: at
    truncation k = h, h+15, … (≤ h+180, at most 13 origins),
    fit F0m and the F1+F2 pool on Z[:T−k] and score the
    anchored point anchor + λ·(pool − anchor) against the
    realized Z[T−k−1+h] by ILR squared error, over a λ grid
    of 0.00–1.00 in 0.05 steps. λ̂ minimises the total
    score; ties resolve to the smaller λ (np.argmin takes
    the first minimum — prefer the anchor). Windows overlap
    when h > 15, so origin scores are correlated; that is
    accepted for a first cut and stated here. Fewer than 3
    usable origins → (0.5, n, False): the declared-judgement
    value, flagged as not estimated."""
    T = len(Z)
    grid = [i / 20 for i in range(21)]
    totals = np.zeros(len(grid))
    n = 0
    k = h
    while k <= h + 180 and n < 13:
        idx = T - k - 1 + h
        if idx >= T:
            k += 15
            continue
        Zk, wk, nk = Z[:T - k], w[:T - k], n_eff[:T - k]
        try:
            ra = f0_mean(Zk, [h], p)[h]
            rp = _pool_raw(Zk, wk, nk, [h], p)[h]
        except ValueError:
            break  # less history only gets worse
        base = ra["mu"] - Z[idx]
        dev = rp["mu"] - ra["mu"]
        for i, lam in enumerate(grid):
            e = base + lam * dev
            totals[i] += float(e @ e)
        n += 1
        k += 15
    if n < 3:
        return 0.5, n, False
    return grid[int(np.argmin(totals))], n, True


def _simplex_quantiles(mu: np.ndarray, var: np.ndarray, p: dict):
    rng = np.random.default_rng(p["seed"])
    sd = np.sqrt(np.maximum(var, 1e-12))
    draws = rng.normal(mu, sd, size=(p["n_samples"], len(mu)))
    S = ilr_inv(draws)
    q = np.quantile(S, [0.05, 0.95], axis=0)
    return {"path": ilr_inv(mu), "lower": q[0], "upper": q[1]}


def forecast_from_arrays(
    Z: np.ndarray,
    w: np.ndarray,
    n_eff: np.ndarray,
    horizons: list[int],
    models: list[str],
    params: dict | None = None,
    uniq_daily: np.ndarray | None = None,
    origins: list[int] | None = None,
) -> dict:
    p = dict(FC_DEFAULTS)
    p.update(params or {})
    Z = np.asarray(Z, dtype=float)
    origins = sorted(set(int(o) for o in
                         (origins if origins is not None else [0])))
    if any(o < 0 for o in origins):
        raise ValueError("origins must be non-negative")
    need = max(horizons) + p["min_days"]
    result = {}
    for m in _expand_models(models):
        mh = {}
        lam_by_h: dict[int, float] | None = None
        lam_meta: dict[int, dict] = {}
        if m == "ANCH05":
            lam_by_h = {h: 0.5 for h in horizons}
        elif m == "ANCHBT":
            lam_by_h = {}
            for h in horizons:
                lam, n_ev, est = _estimate_lambda(
                    Z, w, n_eff, h, p)
                lam_by_h[h] = lam
                lam_meta[h] = {"lambda": lam,
                               "lambda_origins": n_ev,
                               "lambda_estimated": est}
        per_origin: dict[int, dict] = {}
        for k in origins:
            if len(Z) - k < need:
                continue
            Zk = Z[: len(Z) - k] if k else Z
            wk = w[: len(w) - k] if k else w
            nk = n_eff[: len(n_eff) - k] if k else n_eff
            uk = uniq_daily[: len(Zk)] if uniq_daily is not None else None
            try:
                if m == "F0p":
                    raw = f0_persistence(Zk, horizons, p)
                elif m == "F0m":
                    raw = f0_mean(Zk, horizons, p)
                elif m == "F1":
                    raw = f1_trend(Zk, wk, nk, horizons, p)
                elif m == "F2":
                    raw = f2_regime(Zk, wk, nk, horizons, p)
                elif m == "F3":
                    if uk is None:
                        raise ValueError(
                            "F3 requires uniq_daily (enable TF-IDF)")
                    raw = f2_regime(Zk, wk, nk, horizons, p,
                                    uniq_daily=uk)
                elif m == "POOL":
                    raw = _pool_raw(Zk, wk, nk, horizons, p)
                elif m in ("ANCH05", "ANCHBT"):
                    raw = _anchored_raw(Zk, wk, nk, horizons, p,
                                        lam_by_h)
                else:
                    raise ValueError(f"unknown forecast model '{m}'")
            except ValueError:
                if k == 0:
                    raise
                continue
            per_origin[k] = raw
        if 0 not in per_origin:
            raise ValueError("no usable forecast origin (history "
                             "too short)")
        for h in horizons:
            mus = np.array([per_origin[k][h]["mu"] for k in per_origin])
            vrs = np.array([per_origin[k][h]["var"] for k in per_origin])
            mu_bar = mus.mean(axis=0)
            var_bar = vrs.mean(axis=0) + mus.var(axis=0)
            q = _simplex_quantiles(mu_bar, var_bar, p)
            leaf = {"ilr_mean": mu_bar.tolist(),
                    "ilr_var": var_bar.tolist(),
                    "horizon": h,
                    "path": q["path"].tolist(),
                    "lower": q["lower"].tolist(),
                    "upper": q["upper"].tolist(),
                    "origins": sorted(per_origin),
                    "origin_points": {
                        str(k): _simplex_quantiles(
                            per_origin[k][h]["mu"],
                            per_origin[k][h]["var"], p)["path"].tolist()
                        for k in per_origin}}
            for key in ("p_change", "regime_now", "regime_dist"):
                if key in per_origin[0][h]:
                    leaf[key] = per_origin[0][h][key]
            if m == "ANCH05":
                leaf["lambda"] = 0.5
                leaf["lambda_estimated"] = False
            elif m == "ANCHBT":
                leaf.update(lam_meta[h])
            mh[h] = leaf
        result[m] = mh
    return result


# ---------------------------------------------------------------------------
# anomaly (port of seqbayes/anomaly.py)
# ---------------------------------------------------------------------------

AN_DEFAULTS = {
    "baseline_window": 28,
    "vol_window": 14,
    "robust_window": 90,
    "std_floor_frac": 0.5,
    "eps": 1e-9,
}


def _trailing_mean_std(Z: np.ndarray, B: int):
    T, d = Z.shape
    mean = np.full((T, d), np.nan)
    std = np.full((T, d), np.nan)
    csum = np.zeros(d)
    csum2 = np.zeros(d)
    for t in range(T):
        if t >= B:
            m = csum / B
            v = np.maximum(csum2 / B - m**2, 0.0)
            mean[t] = m
            std[t] = np.sqrt(v)
        csum += Z[t]
        csum2 += Z[t] ** 2
        if t - B >= 0:
            csum -= Z[t - B]
            csum2 -= Z[t - B] ** 2
    return mean, std


def a1_level_shifts(Z: np.ndarray, p: dict) -> dict:
    B = p["baseline_window"]
    T = len(Z)
    mean, std = _trailing_mean_std(Z, B)
    Wr = p.get("robust_window", 90)
    frac = p.get("std_floor_frac", 0.5)
    floor = np.full((T, Z.shape[1]), np.nan)
    for t in range(T):
        win = std[max(0, t - Wr): t]
        win = win[np.isfinite(win).all(axis=1)]
        if len(win) >= 10:
            floor[t] = frac * np.median(win, axis=0)
    std_eff = np.maximum(std, np.where(np.isfinite(floor), floor,
                                       p["eps"]))
    std_eff = np.maximum(std_eff, p["eps"])
    z = (Z - mean) / std_eff
    az = np.abs(z)
    max_abs = np.full(T, np.nan)
    ok = np.isfinite(az).all(axis=1)
    max_abs[ok] = az[ok].max(axis=1)
    displacement = Z - mean
    return {"components": z, "max_abs": max_abs,
            "displacement": displacement}


def a2_distributional(S: np.ndarray, p: dict) -> dict:
    B = p["baseline_window"]
    T = len(S)
    score = np.full(T, np.nan)
    w = np.ones(B) / B
    for t in range(B, T):
        base = weighted_geometric_mean(S[t - B: t], w)
        score[t] = aitchison_distance(S[t][None, :], base[None, :])[0]
    return {"score": score}


def a3_variance_change(Z: np.ndarray, p: dict) -> dict:
    Wv = p["vol_window"]
    Wr = p["robust_window"]
    T = len(Z)
    step = np.diff(Z, axis=0)
    v = np.full(T, np.nan)
    for t in range(Wv, T):
        seg = step[t - Wv: t]
        v[t] = np.var(seg, axis=0).sum()
    score = np.full(T, np.nan)
    for t in range(Wv + Wr, T):
        win = v[t - Wr: t]
        win = win[np.isfinite(win)]
        if len(win) < 10:
            continue
        med = np.median(win)
        mad = np.median(np.abs(win - med)) + p["eps"]
        score[t] = (v[t] - med) / (1.4826 * mad)
    return {"raw": v, "score": score}


def _robust_standardize(x: np.ndarray, p: dict) -> np.ndarray:
    Wr = p["robust_window"]
    T = len(x)
    z = np.full(T, np.nan)
    for t in range(Wr, T):
        win = x[t - Wr: t]
        win = win[np.isfinite(win)]
        if len(win) < 10:
            continue
        med = np.median(win)
        mad = np.median(np.abs(win - med)) + p["eps"]
        z[t] = (x[t] - med) / (1.4826 * mad)
    return z


def anomaly_scores(
    S: np.ndarray,
    Z: np.ndarray,
    families: list[str] | None = None,
    params: dict | None = None,
) -> dict:
    p = dict(AN_DEFAULTS)
    p.update(params or {})
    S = closure(np.asarray(S, dtype=float))
    Z = np.asarray(Z, dtype=float)
    fams = families or ["A1", "A2", "A3"]
    out: dict = {}
    if "A1" in fams:
        out["A1"] = a1_level_shifts(Z, p)
    if "A2" in fams:
        out["A2"] = a2_distributional(S, p)
    if "A3" in fams:
        out["A3"] = a3_variance_change(Z, p)
    parts = []
    if "A1" in out:
        parts.append(out["A1"]["max_abs"])
    if "A2" in out:
        out["A2"]["std_score"] = _robust_standardize(out["A2"]["score"], p)
        parts.append(out["A2"]["std_score"])
    if "A3" in out:
        parts.append(out["A3"]["score"])
    if parts:
        M = np.vstack(parts)
        Mf = np.where(np.isfinite(M), M, -np.inf)
        comp = Mf.max(axis=0)
        comp[~np.isfinite(M).any(axis=0)] = np.nan
        out["composite"] = comp
    return out


# ---------------------------------------------------------------------------
# temperature (port of seqbayes/temperature.py)
# ---------------------------------------------------------------------------


def dyad_temperature(S: np.ndarray, window_days: int = 28) -> np.ndarray:
    risk = S[:, 3] + S[:, 4]
    T = len(risk)
    out = np.full(T, np.nan)
    valid = np.isfinite(risk)
    cumsum = np.nancumsum(np.where(valid, risk, 0.0))
    counts = np.cumsum(valid.astype(float))
    for t in range(T):
        s = max(0, t - window_days)
        n = counts[t] - (counts[s - 1] if s > 0 else 0.0)
        if n >= window_days / 2:
            out[t] = (cumsum[t] - (cumsum[s - 1] if s > 0 else 0.0)) / n
    return out


def temperature_weight(T: np.ndarray, t_lo: float = 0.32,
                       t_hi: float = 0.42,
                       w_min: float = 0.10) -> np.ndarray:
    T = np.asarray(T, dtype=float)
    frac = (T - t_lo) / (t_hi - t_lo)
    frac = np.clip(frac, 0.0, 1.0)
    w = w_min + (1.0 - w_min) * frac
    w[~np.isfinite(T)] = w_min
    return w


def temperature_velocity(T: np.ndarray,
                         lag_days: int = 28) -> np.ndarray:
    T = np.asarray(T, dtype=float)
    v = np.full_like(T, np.nan)
    ok = np.isfinite(T)
    for t in range(len(T)):
        if t >= lag_days and ok[t] and ok[t - lag_days]:
            v[t] = T[t] - T[t - lag_days]
    return v


def velocity_weight(v: np.ndarray, v_scale: float = 0.05,
                    v_max: float = 0.40) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    w = v_max * np.clip(v / v_scale, 0.0, 1.0)
    w[~np.isfinite(v)] = 0.0
    return w


def modulate_composite(
    composite: np.ndarray,
    S: np.ndarray,
    window_days: int = 28,
    t_lo: float = 0.32,
    t_hi: float = 0.42,
    w_min: float = 0.10,
    velocity_enabled: bool = False,
    velocity_lag_days: int = 28,
    velocity_scale: float = 0.05,
    velocity_max: float = 0.40,
) -> dict:
    temp = dyad_temperature(S, window_days)
    w_level = temperature_weight(temp, t_lo, t_hi, w_min)
    if velocity_enabled:
        vel = temperature_velocity(temp, velocity_lag_days)
        w_vel = velocity_weight(vel, velocity_scale, velocity_max)
        w = np.minimum(1.0, w_level + w_vel)
    else:
        vel = np.full_like(temp, np.nan)
        w_vel = np.zeros_like(temp)
        w = w_level
    return {
        "temperature": temp,
        "velocity": vel,
        "weight_level": w_level,
        "weight_velocity": w_vel,
        "weight": w,
        "raw": np.asarray(composite, dtype=float),
        "modulated": np.asarray(composite, dtype=float) * w,
        "params": {
            "window_days": window_days, "t_lo": t_lo,
            "t_hi": t_hi, "w_min": w_min,
            "velocity_enabled": velocity_enabled,
            "velocity_lag_days": velocity_lag_days,
            "velocity_scale": velocity_scale,
            "velocity_max": velocity_max,
        },
    }


# ---------------------------------------------------------------------------
# TF-IDF (port of seqbayes/tfidf.py; counts as dicts, not pandas)
# ---------------------------------------------------------------------------

TF_MODES = ("share", "log")


def _counter(codes: np.ndarray, counts: np.ndarray) -> dict:
    out: dict[str, float] = {}
    for c, v in zip(codes, counts):
        out[c] = out.get(c, 0.0) + float(v)
    return out


def code_uniqueness(doc_counts: dict, corpus_counts: list[dict],
                    tf_mode: str = "share"):
    """Returns (codes, tf, df, idf, uniqueness, n_docs) or None."""
    if tf_mode not in TF_MODES:
        raise ValueError(f"tf_mode must be one of {TF_MODES}")
    docs = [c for c in corpus_counts
            if sum(c.values()) > 0]
    n_docs = len(docs)
    codes = set(doc_counts)
    for c in docs:
        codes |= set(c)
    codes = sorted(codes)
    if not codes:
        return None
    cnt = np.array([doc_counts.get(c, 0.0) for c in codes])
    V = len(codes)
    tot = cnt.sum()
    if tf_mode == "share":
        tf = (cnt + 1.0) / (tot + V)
    else:
        tf = np.log1p(cnt + 1.0)
    if n_docs == 0:
        idf = np.ones(len(codes))
        df = np.zeros(len(codes))
    else:
        present = np.array(
            [[1.0 if d.get(c, 0.0) > 0 else 0.0 for c in codes]
             for d in docs])
        df = present.sum(axis=0)
        idf = np.log((1.0 + n_docs) / (1.0 + df)) + 1.0
    return codes, tf, df, idf, tf * idf, n_docs


def _ord_to_iso(o: int) -> str:
    return str(np.datetime64(int(o), "D"))


def apply_snapshot_tfidf(records: dict, corpus: dict, window_days: int,
                         start_ord: int, end_ord: int,
                         tf_mode: str = "share"):
    """Records/corpus as {ords, codes, counts[, dyads]} arrays.

    Returns (w_tfidf, window_metrics, uniqueness_tables) mirroring
    the PoC: weights for window b come ONLY from window b-1.
    """
    rec_ords = records["ords"]
    rec_codes = records["codes"]
    rec_counts = records["counts"].astype(float)
    corp_ords = corpus["ords"]
    corp_codes = corpus["codes"]
    corp_counts = corpus["counts"].astype(float)
    corp_dyads = corpus["dyads"]

    n_edges = (end_ord + 1 - start_ord) // window_days + 1
    edges = start_ord + window_days * np.arange(n_edges)

    w_tfidf = rec_counts.copy()
    metrics: list[dict] = []
    tables: list[dict] = []
    last_table = None

    for b in range(len(edges) - 1):
        ws, we = int(edges[b]), int(edges[b + 1])
        in_window = (rec_ords >= ws) & (rec_ords < we)
        if b == 0:
            table = None
        else:
            pws, pwe = int(edges[b - 1]), int(edges[b])
            doc_mask = (rec_ords >= pws) & (rec_ords < pwe)
            doc_counts = _counter(rec_codes[doc_mask],
                                  rec_counts[doc_mask])
            if sum(doc_counts.values()) > 0:
                cmask = (corp_ords >= pws) & (corp_ords < pwe)
                by_dyad: dict[str, dict] = {}
                for d, c, v in zip(corp_dyads[cmask],
                                   corp_codes[cmask],
                                   corp_counts[cmask]):
                    dd = by_dyad.setdefault(d, {})
                    dd[c] = dd.get(c, 0.0) + float(v)
                table = code_uniqueness(
                    doc_counts, list(by_dyad.values()), tf_mode=tf_mode)
                last_table = table
            else:
                table = last_table
        if table is not None:
            codes, tf, df, idf, uniq, n_docs = table
            if len(codes):
                max_idf = float(np.log(1.0 + n_docs) + 1.0)
                fallback = (1.0 / (len(codes) + 1)) * max_idf
                umap = dict(zip(codes, uniq))
                idx = np.where(in_window)[0]
                for i in idx:
                    w_tfidf[i] = rec_counts[i] * umap.get(
                        rec_codes[i], fallback)
                metrics.append({
                    "window_start": _ord_to_iso(ws),
                    "window_end": _ord_to_iso(we - 1),
                    "n_codes": len(codes),
                    "total_uniqueness": float(uniq.sum()),
                    "mean_uniqueness": float(uniq.mean()),
                })
                tables.append({
                    "window_start": _ord_to_iso(ws),
                    "table": [
                        {"cameo_code": c, "tf": float(t),
                         "df": int(d), "idf": float(i2),
                         "uniqueness": float(u)}
                        for c, t, d, i2, u in zip(codes, tf, df,
                                                  idf, uniq)],
                })
                continue
        metrics.append({
            "window_start": _ord_to_iso(ws),
            "window_end": _ord_to_iso(we - 1),
            "n_codes": 0,
            "total_uniqueness": 0.0,
            "mean_uniqueness": 0.0,
        })
    return w_tfidf, metrics, tables


# ---------------------------------------------------------------------------
# daily build (port of seqbayes/adapters.records_to_daily)
# ---------------------------------------------------------------------------


def records_to_daily(records: dict, crosswalk: dict, start_ord: int,
                     end_ord: int, weights: np.ndarray | None = None,
                     zero_delta: float = 5e-3) -> dict:
    """Daily posteriors from records over [start_ord, end_ord].

    weights defaults to obs_count (the PoC's weight_col). Returns
    {dates, simplex (T,5) NaN on empty days, W, W_eff, dropped}.
    """
    T = end_ord - start_ord + 1
    ords = records["ords"]
    codes = records["codes"]
    obs = records["counts"].astype(float)
    w = obs if weights is None else np.asarray(weights, dtype=float)
    keep = (ords >= start_ord) & (ords <= end_ord)
    t_idx = (ords[keep] - start_ord).astype(int)
    codes_k = codes[keep]
    obs_k = obs[keep]
    w_k = w[keep]

    log_cache: dict[str, np.ndarray] = {}
    dropped = 0
    mapped_rows = []
    for j, c in enumerate(codes_k):
        sx = crosswalk.get(c)
        if sx is None:
            dropped += 1
            continue
        if c not in log_cache:
            log_cache[c] = np.log(multiplicative_zero_replace(
                np.array(sx, dtype=float)[None, :],
                delta=zero_delta))[0]
        mapped_rows.append(j)
    simplex = np.full((T, 5), np.nan)
    W = np.zeros(T)
    W_eff = np.zeros(T)
    if mapped_rows:
        mj = np.array(mapped_rows)
        tt = t_idx[mj]
        ww = w_k[mj]
        logsum = np.zeros((T, 5))
        logmat = np.array([log_cache[codes_k[j]] for j in mj])
        np.add.at(logsum, tt, ww[:, None] * logmat)
        wsum = np.zeros(T)
        np.add.at(wsum, tt, ww)
        obssum = np.zeros(T)
        np.add.at(obssum, tt, obs_k[mj])
        for t in range(T):
            if wsum[t] > 0:
                v = logsum[t] / wsum[t]
                simplex[t] = closure(np.exp(v - v.max()))
                W[t] = obssum[t]
                W_eff[t] = wsum[t]
    dates = [_ord_to_iso(start_ord + t) for t in range(T)]
    return {"dates": dates, "simplex": simplex, "W": W,
            "W_eff": W_eff, "dropped": dropped}


# ---------------------------------------------------------------------------
# config (port of seqbayes/config.py, set-share excluded)
# ---------------------------------------------------------------------------


@dataclass
class DecayConfig:
    half_life_days: float = 14.0
    w_cap: Optional[float] = None
    w_alpha: float = 1.0


@dataclass
class TfidfConfig:
    enabled: bool = False
    window_days: int = 28
    tf_mode: str = "share"


@dataclass
class ForecastConfig:
    horizons: List[int] = field(default_factory=lambda: [45, 60, 90])
    models: List[str] = field(default_factory=lambda: ["F0", "F1", "F2"])
    params: dict = field(default_factory=dict)
    origins: List[int] = field(default_factory=lambda: [0])


@dataclass
class AnomalyConfig:
    baseline_window_days: int = 28
    families: List[str] = field(
        default_factory=lambda: ["A1", "A2", "A3"])
    params: dict = field(default_factory=dict)


@dataclass
class TemperatureConfig:
    enabled: bool = True
    window_days: int = 28
    t_lo: float = 0.32
    t_hi: float = 0.42
    w_min: float = 0.10
    velocity_enabled: bool = False
    velocity_lag_days: int = 28
    velocity_scale: float = 0.05
    velocity_max: float = 0.40


@dataclass
class PipelineConfig:
    dyad_id: str = ""
    timeframe: dict = field(
        default_factory=lambda: {"start": "", "end": ""})
    decay: DecayConfig = field(default_factory=DecayConfig)
    tfidf: TfidfConfig = field(default_factory=TfidfConfig)
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    temperature: TemperatureConfig = field(
        default_factory=TemperatureConfig)

    @classmethod
    def from_dict(cls, d: dict) -> "PipelineConfig":
        d = dict(d)
        decay = DecayConfig(**d.pop("decay", {}))
        tfidf = TfidfConfig(**d.pop("tfidf", {}))
        forecast = ForecastConfig(**d.pop("forecast", {}))
        anomaly = AnomalyConfig(**d.pop("anomaly", {}))
        temperature = TemperatureConfig(**d.pop("temperature", {}))
        d.pop("setshare", None)  # excluded (Dan, 2026-11-17)
        cfg = cls(decay=decay, tfidf=tfidf, forecast=forecast,
                  anomaly=anomaly, temperature=temperature, **d)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.decay.half_life_days <= 0:
            raise ValueError("decay.half_life_days must be positive")
        if self.tfidf.window_days not in (7, 14, 28):
            raise ValueError("tfidf.window_days must be in {7, 14, 28}")
        if self.tfidf.tf_mode not in ("share", "log"):
            raise ValueError("tfidf.tf_mode must be 'share' or 'log'")
        if any(h <= 0 for h in self.forecast.horizons):
            raise ValueError("forecast.horizons must be positive")
        if any(o < 0 for o in self.forecast.origins):
            raise ValueError("forecast.origins must be non-negative")
        if self.anomaly.baseline_window_days <= 0:
            raise ValueError("anomaly.baseline_window_days must be > 0")
        for f in self.anomaly.families:
            if f not in ("A1", "A2", "A3"):
                raise ValueError(f"unknown anomaly family '{f}'")
        tc = self.temperature
        if tc.window_days <= 0:
            raise ValueError("temperature.window_days must be positive")
        if not (0 <= tc.t_lo < tc.t_hi <= 1):
            raise ValueError("temperature requires 0 <= t_lo < t_hi <= 1")
        if not (0 <= tc.w_min <= 1):
            raise ValueError("temperature.w_min must be in [0, 1]")
        if tc.velocity_lag_days <= 0:
            raise ValueError("temperature.velocity_lag_days must be > 0")
        if tc.velocity_scale <= 0:
            raise ValueError("temperature.velocity_scale must be > 0")
        if not (0 <= tc.velocity_max <= 1):
            raise ValueError("temperature.velocity_max must be in [0, 1]")

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# volume regime (port of gallery_data.volume_regime + pcs._window_sum)
# ---------------------------------------------------------------------------


def _cs(x: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(x)])


def _window_sum(x: np.ndarray, w: int, end_shift: int = 0,
                min_days: int | None = None) -> np.ndarray:
    if min_days is None:
        min_days = w
    n = len(x)
    c = _cs(x)
    end = np.arange(n) - end_shift
    start = end - w
    avail = np.minimum(end, n - 1) - np.maximum(start, -1)
    hi = np.clip(end + 1, 0, n)
    lo = np.clip(start + 1, 0, n)
    out = c[hi] - c[lo]
    out[(end < 0) | (avail < min_days)] = np.nan
    return out


def volume_regime(dates: list[str], vol: np.ndarray) -> dict:
    """Pooled 7d volume vs the empirical distribution of pooled 7d
    volumes over the trailing 90d baseline (gallery S10)."""
    vol = np.asarray(vol, dtype=float)
    pooled = _window_sum(vol, 7)
    rows = []
    for t in range(len(dates)):
        if not np.isfinite(pooled[t]) or t - 7 - 90 < 0:
            rows.append((np.nan, np.nan))
            continue
        base_days = vol[t - 7 - 90: t - 7]
        base_windows = np.convolve(base_days, np.ones(7), "valid")
        base_windows = base_windows[base_days[:len(base_windows)] >= 0]
        if len(base_windows) < 10:
            rows.append((np.nan, np.nan))
            continue
        expected = float(np.mean(base_windows))
        tail = float((base_windows >= pooled[t]).mean())
        rows.append((pooled[t] / expected if expected > 0 else np.nan,
                     tail))
    arr = np.array(rows, dtype=float) if rows else np.empty((0, 2))
    out = {"date": list(dates),
           "pooled7": pooled,
           "vol_ratio": arr[:, 0] if len(arr) else np.array([]),
           "vol_tail": arr[:, 1] if len(arr) else np.array([])}
    valid = np.isfinite(out["vol_ratio"])
    if valid.any():
        first = int(np.argmax(valid))
        out = {k: (v[first:] if isinstance(v, np.ndarray)
                   else v[first:]) for k, v in out.items()}
    return out


# ---------------------------------------------------------------------------
# api assembly (port of seqbayes/api.py, set-share + lens excluded)
# ---------------------------------------------------------------------------


def _clean(x):
    """JSON-safe conversion: ndarray -> list, NaN -> None (recursive)."""
    if isinstance(x, np.ndarray):
        return _clean(x.tolist())
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, float) and np.isnan(x):
        return None
    return x


def _series_block(daily: dict, cfg: PipelineConfig) -> dict:
    sim = daily["simplex"]
    w_raw = daily["W"]
    res = sequential_posteriors(
        sim, w_raw,
        half_life_days=cfg.decay.half_life_days,
        w_cap=cfg.decay.w_cap,
        w_alpha=cfg.decay.w_alpha,
    )
    post = res["posterior"]
    return {
        "dates": daily["dates"],
        "posterior_simplex": post.tolist(),
        "ilr": ilr(np.where(post <= 0, 1e-12, post)).tolist(),
        "n_eff": res["n_eff"].tolist(),
        "w_raw": w_raw.tolist(),
        "w_used": res["weights"].tolist(),
        "lam": res["lam"],
    }


def _forecast_block(series: dict, cfg: PipelineConfig,
                    uniq_daily: np.ndarray | None = None,
                    models: list[str] | None = None) -> dict:
    fc = cfg.forecast
    models = list(fc.models) if models is None else models
    if not models:
        return {"status": "not_run"}
    Z = np.array(series["ilr"])
    w = np.array(series["w_used"])
    n_eff = np.array(series["n_eff"])
    return forecast_from_arrays(
        Z, w, n_eff,
        horizons=[int(h) for h in fc.horizons],
        models=models,
        params=dict(fc.params),
        uniq_daily=uniq_daily,
        origins=list(fc.origins),
    )


def _daily_uniqueness(dates: list[str],
                      window_metrics: list[dict]) -> np.ndarray:
    if not window_metrics:
        return np.zeros(len(dates))
    starts = np.array([np.datetime64(m["window_start"], "D").astype(int)
                       for m in window_metrics])
    vals = np.array([m["mean_uniqueness"] for m in window_metrics],
                    dtype=float)
    ords = np.array([np.datetime64(d, "D").astype(int) for d in dates])
    pos = np.searchsorted(starts, ords, side="right") - 1
    out = np.where(pos >= 0, vals[np.maximum(pos, 0)], 0.0)
    return out.astype(float)


def _anomaly_block(series: dict, cfg: PipelineConfig) -> dict:
    S = np.array(series["posterior_simplex"])
    Z = np.array(series["ilr"])
    ac = cfg.anomaly
    params = dict(ac.params)
    params["baseline_window"] = ac.baseline_window_days
    scores = anomaly_scores(S, Z, families=list(ac.families),
                            params=params)
    block = {
        "dates": series["dates"],
        "families": list(ac.families),
        "baseline_window_days": ac.baseline_window_days,
        "scores": scores,
    }
    tc = cfg.temperature
    if tc.enabled and "composite" in scores:
        mod = modulate_composite(
            np.asarray(scores["composite"], dtype=float), S,
            window_days=tc.window_days, t_lo=tc.t_lo, t_hi=tc.t_hi,
            w_min=tc.w_min, velocity_enabled=tc.velocity_enabled,
            velocity_lag_days=tc.velocity_lag_days,
            velocity_scale=tc.velocity_scale,
            velocity_max=tc.velocity_max)
        block["temperature"] = mod
    else:
        block["temperature"] = {"status": "not_enabled"}
    return block


def run_pipeline(body: dict, records: dict, corpus: dict | None,
                 crosswalk: dict, daily_volume: np.ndarray) -> dict:
    """Execute the ported PoC pipeline.

    records/corpus: {ords, codes, counts[, dyads]} arrays over the
    body's timeframe; crosswalk: {code: (p1..p5)}; daily_volume:
    all-records daily volume aligned to the timeframe (for the
    volume-regime block). Mirrors seqbayes.api.run_pipeline minus
    the set-share and lens blocks.
    """
    t0 = time.time()
    cfg = PipelineConfig.from_dict(body)
    warnings: list[str] = []
    tf = cfg.tfidf
    start_ord = int(np.datetime64(cfg.timeframe["start"], "D")
                    .astype(np.int64))
    end_ord = int(np.datetime64(cfg.timeframe["end"], "D")
                  .astype(np.int64))

    if tf.enabled:
        if records is None or corpus is None or crosswalk is None:
            raise ValueError(
                "tfidf.enabled requires records, corpus and crosswalk")
        w_tfidf, window_metrics, uniq_tables = apply_snapshot_tfidf(
            records, corpus, window_days=tf.window_days,
            start_ord=start_ord, end_ord=end_ord, tf_mode=tf.tf_mode)
        daily_rw = records_to_daily(records, crosswalk, start_ord,
                                    end_ord, weights=w_tfidf)
        daily = records_to_daily(records, crosswalk, start_ord, end_ord)
        series_tfidf = _series_block(daily_rw, cfg)
        tfidf_block: dict = {
            "enabled": True,
            "tf_mode": tf.tf_mode,
            "window_days": tf.window_days,
            "window_metrics": window_metrics,
            "uniqueness_tables": uniq_tables,
        }
        dropped = daily_rw["dropped"]
    else:
        daily = records_to_daily(records, crosswalk, start_ord, end_ord)
        series_tfidf = {"status": "not_enabled"}
        tfidf_block = {"enabled": False}
        dropped = daily["dropped"]

    series = _series_block(daily, cfg)

    # --- forecasting ladder -------------------------------------
    # F3 REPAIR (Dan, 2026-11-17): the original PoC never
    # passed the uniqueness series to the unweighted block,
    # so selecting F3 raised "F3 requires uniq_daily" and
    # killed the whole run. The uniqueness series is a
    # property of the dyad's records, shared by both
    # ladders, so when TF-IDF is on it now goes to BOTH
    # blocks. When TF-IDF is off no uniqueness series
    # exists, so F3 is dropped from the model list with a
    # warning instead of failing the run. (The original
    # seqbayes api.py carries the identical repair.)
    uniq_daily = None
    if tfidf_block.get("enabled") and "ilr" in series_tfidf:
        uniq_daily = _daily_uniqueness(series_tfidf["dates"],
                                       tfidf_block["window_metrics"])
    fc_models = list(cfg.forecast.models)
    if "F3" in fc_models and uniq_daily is None:
        fc_models = [m for m in fc_models if m != "F3"]
        warnings.append(
            "F3 skipped: it is gated on code uniqueness — "
            "enable TF-IDF to run it.")
    forecast_block: dict = {
        "unweighted": _forecast_block(series, cfg,
                                      uniq_daily=uniq_daily,
                                      models=fc_models)}
    if uniq_daily is not None:
        forecast_block["tfidf"] = _forecast_block(
            series_tfidf, cfg, uniq_daily=uniq_daily,
            models=fc_models)
    else:
        forecast_block["tfidf"] = {"status": "not_enabled"}

    result = {
        "series": series,
        "series_tfidf": series_tfidf,
        "tfidf": tfidf_block,
        "forecast": forecast_block,
        "anomaly": {
            "unweighted": _anomaly_block(series, cfg),
            "tfidf": _anomaly_block(series_tfidf, cfg)
            if tfidf_block.get("enabled") and "ilr" in series_tfidf
            else {"status": "not_enabled"},
        },
        "volume_regime": volume_regime(series["dates"], daily_volume),
        "meta": {
            "config_echo": cfg.to_dict(),
            "timings": {"total_s": time.time() - t0},
            "warnings": warnings,
        },
    }
    if dropped:
        result["meta"]["warnings"].append(
            f"dropped {dropped} records with no crosswalk mapping")
    return _clean(result)


# ---------------------------------------------------------------------------
# DDA sidecar loaders
# ---------------------------------------------------------------------------


def _rows_to_records(rows: list[tuple], with_dyads: bool) -> dict:
    if with_dyads:
        return {
            "ords": np.array(
                [np.datetime64(r[2], "D").astype(int) for r in rows],
                dtype=np.int64),
            "codes": np.array([str(r[3]) for r in rows]),
            "counts": np.array([float(r[4]) for r in rows]),
            "dyads": np.array([f"{r[0]}_{r[1]}" for r in rows]),
        }
    return {
        "ords": np.array(
            [np.datetime64(r[0], "D").astype(int) for r in rows],
            dtype=np.int64),
        "codes": np.array([str(r[1]) for r in rows]),
        "counts": np.array([float(r[2]) for r in rows]),
    }


def load_records(conn: sqlite3.Connection, iso1: str, iso2: str,
                 start: str, end: str) -> dict:
    rows = conn.execute(
        "SELECT obs_date, cameo_code, obs_count FROM dda "
        "WHERE iso1 = ? AND iso2 = ? AND obs_date >= ? "
        "AND obs_date <= ? ORDER BY obs_date",
        (iso1, iso2, start, end)).fetchall()
    return _rows_to_records(rows, with_dyads=False)


def load_corpus(conn: sqlite3.Connection, iso1: str, iso2: str,
                start: str, end: str) -> dict:
    """Referent dyads: sharing either country, excluding the
    target dyad and its exact inverse (PoC poc_app.get_corpus)."""
    rows = conn.execute(
        "SELECT iso1, iso2, obs_date, cameo_code, obs_count FROM dda "
        "WHERE (iso1 IN (?, ?) OR iso2 IN (?, ?)) "
        "AND NOT (iso1 = ? AND iso2 = ?) "
        "AND NOT (iso1 = ? AND iso2 = ?) "
        "AND obs_date >= ? AND obs_date <= ? ORDER BY obs_date",
        (iso1, iso2, iso1, iso2, iso1, iso2, iso2, iso1,
         start, end)).fetchall()
    return _rows_to_records(rows, with_dyads=True)


def record_extent(conn: sqlite3.Connection, iso1: str, iso2: str) -> dict:
    """The dyad's full record extent in the sidecar, unfiltered
    by any timeframe: {first, last, n_records}; the dates are
    None when the dyad has no records. The R&D page defaults
    and clamps its timeframe start to this coverage: the
    pipeline cold-starts at the requested start, so a start
    months before the first record idles the series at the
    prior and the anomaly detectors spike when data begins
    (diagnosed 2026-11-18)."""
    first, last, n = conn.execute(
        "SELECT MIN(obs_date), MAX(obs_date), COUNT(*) FROM dda "
        "WHERE iso1 = ? AND iso2 = ?", (iso1, iso2)).fetchone()
    return {"first": first, "last": last, "n_records": int(n or 0)}


def frame_extent(conn: sqlite3.Connection) -> dict:
    """The whole sidecar's record extent: {first, last,
    n_records} across every dyad (dates None when the table
    is empty). The frontend seeds its default as-of from
    `last` (Dan, 2026-11-19: the default as-of is the most
    recent date available in the sidecar, not a hardcoded
    snapshot date), so a rebuilt sidecar moves the demo's
    default with it."""
    first, last, n = conn.execute(
        "SELECT MIN(obs_date), MAX(obs_date), COUNT(*) FROM dda",
    ).fetchone()
    return {"first": first, "last": last, "n_records": int(n or 0)}


def load_daily_volume(conn: sqlite3.Connection, iso1: str, iso2: str,
                      start: str, end: str) -> np.ndarray:
    start_ord = int(np.datetime64(start, "D").astype(np.int64))
    end_ord = int(np.datetime64(end, "D").astype(np.int64))
    vol = np.zeros(end_ord - start_ord + 1)
    for d, total in conn.execute(
            "SELECT obs_date, SUM(obs_count) FROM dda "
            "WHERE iso1 = ? AND iso2 = ? AND obs_date >= ? "
            "AND obs_date <= ? GROUP BY obs_date",
            (iso1, iso2, start, end)):
        vol[int(np.datetime64(d, "D").astype(np.int64))
            - start_ord] = float(total)
    return vol


def crosswalk_from_simplexes(
        simplexes: dict) -> dict[str, tuple[float, ...]]:
    """{code: (p1..p5)} for placed codes only (None = unplaced,
    excluded from emission, as in the PoC crosswalk build)."""
    return {str(c): tuple(float(v) for v in sx)
            for c, sx in simplexes.items() if sx is not None}
