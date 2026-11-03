"""F2 regime forecast with multi-origin mixture, vendored from the PoC.

Provenance: ``dan/poc`` @ ``15b0e41``, ``src/seqbayes/forecast.py``
(``f2_regime``, ``forecast_from_arrays`` F2 branch) and
``src/seqbayes/geometry.py`` (``ilr_basis``/``ilr``/``ilr_inv``).
Copied verbatim (numpy) except for the restriction noted below;
do not "improve" the math here — improvements belong in the PoC,
and this module is re-vendored from there.

Restriction: only the F2 rung is vendored. The PoC ladder's F0/F1/F3
rungs are omitted deliberately — O1 pins F2, and a port that
silently offered other models would reopen a closed decision.

Dependency note (Dan, 2026-11-03): mainstream packages are
available for this work — numpy is a backend dependency
(requirements.txt), and ``pip install -r requirements.txt``
before restart is part of pickup. An earlier stdlib-only port
of this module was replaced on Dan's instruction; do not
revert to it.

What the workflow port changes (and ONLY this):
- Input series: the PoC runs F2 on the latent ILR trajectory from
  the filter. The workflow has no latent series, so the port runs
  F2 on ILR(daily DDA joints) — the observed daily composition.
  That is a noisier input than the latent posterior, so regime
  calls here are jumpier than the PoC's; the figure caption must
  say the input is observed joints, not a latent forecast.
- Day weights ``w`` and information stock ``n_eff``: the PoC takes
  them from the pipeline. The port uses daily joint_volume for
  both (days with more joint-able reports weigh more; uncertainty
  inflates up to 4x when the latest day's volume is below its
  trailing median — same rule, pipeline quantity replaced).
- Gap days (joint None) are forward-filled before the transform
  by the caller (``hybrid_store``); the fill count travels with
  the response so the figure can caption it. The fill preserves
  daily spacing for the volatility/regime windows; filled days
  carry weight ~0 in the trend fits, so they do not pull trends.

Multi-origin mixture (PoC rule, unchanged): origins [0, 7, 15, 30,
45] rerun the ladder on history truncated to T-k; the leaf's
path/lower/upper are the cross-origin mixture (mean of origin
means; variance = mean of origin variances + variance of origin
means, so disagreement widens the bands). F2 extras (regime_now,
p_change, regime_dist) come from the k=0 origin only. Origins
that lack enough history are skipped gracefully; the response
lists the origins actually used.
"""

from __future__ import annotations

import numpy as np

DEFAULTS = {
    "trend_window": 90,
    "vol_window": 14,
    "regime_window": 180,
    "tau": None,
    "n_samples": 500,
    "seed": 0,
    "min_days": 20,
}

HYBRID_MODEL = "F2"
HYBRID_HORIZON = 60
HYBRID_ORIGINS = [0, 7, 15, 30, 45]


def _closure(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    return x / x.sum(axis=-1, keepdims=True)


def ilr_basis(d: int) -> np.ndarray:
    """Default orthonormal ILR basis (d, d-1) via Helmert contrasts."""
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
    """ILR transform: simplex (..., d) -> R^(d-1)."""
    x = np.asarray(x, dtype=float)
    if np.any(x <= 0):
        raise ValueError("ilr: x must be strictly positive (zero-replace first)")
    d = x.shape[-1]
    if v is None:
        v = ilr_basis(d)
    return _clr(x) @ v


def ilr_inv(z: np.ndarray, v: np.ndarray | None = None) -> np.ndarray:
    """Inverse ILR: R^(d-1) -> simplex (..., d)."""
    z = np.asarray(z, dtype=float)
    d = z.shape[-1] + 1
    if v is None:
        v = ilr_basis(d)
    return _closure(np.exp(z @ v.T))


def _check_history(Z: np.ndarray, need: int, what: str) -> None:
    if len(Z) < need:
        raise ValueError(f"{what}: need >= {need} days of history, got {len(Z)}")


def _wls_trend(Z: np.ndarray, w: np.ndarray, degree: int = 1):
    """Weighted least squares trend on trailing window."""
    L, d = Z.shape
    s = np.arange(L, dtype=float)
    X = np.vander(s, degree + 1, increasing=True)
    wn = w / w.sum() * L
    sw = np.sqrt(wn)
    coef, *_ = np.linalg.lstsq(X * sw[:, None], Z * sw[:, None], rcond=None)
    resid = Z - X @ coef
    dof = max(L - (degree + 1), 1)
    var = (wn[:, None] * resid**2).sum(axis=0) / dof
    return coef, var


def _neff_scale(n_eff_now: float, n_eff_ref: float) -> float:
    """Inflate uncertainty when current information stock is thin."""
    if n_eff_ref <= 0:
        return 1.0
    return float(np.clip(n_eff_ref / max(n_eff_now, 1e-9), 1.0, 4.0))


def _regime_states(Z: np.ndarray, p: dict):
    """Calm/agitated regimes from trailing volatility of ILR increments.

    Returns (regimes (T,) bool agitated, P (2,2) transition matrix,
    threshold used).
    """
    Wv = p["vol_window"]
    step = np.linalg.norm(np.diff(Z, axis=0), axis=1)
    v = np.array([step[max(0, i - Wv + 1): i + 1].std()
                  for i in range(len(step))])
    v = np.concatenate([[v[0]], v])
    Wr = min(p["regime_window"], len(v))
    thr = np.median(v[-Wr:])
    agit = v > thr
    r = agit[-Wr:].astype(int)
    counts = np.ones((2, 2))
    for a, b in zip(r[:-1], r[1:]):
        counts[a, b] += 1
    P = counts / counts.sum(axis=1, keepdims=True)
    return agit, P, thr


def f2_regime(
    Z: np.ndarray,
    w: np.ndarray,
    n_eff: np.ndarray,
    horizons: list[int],
    p: dict,
) -> dict:
    """F2: 2-state Markov regime + per-regime F1 trends (mixture forecast)."""
    L = min(p["trend_window"], len(Z))
    _check_history(Z, p["min_days"], "F2")
    agit, P, thr = _regime_states(Z, p)
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


def _simplex_quantiles(mu: np.ndarray, var: np.ndarray, p: dict):
    """Predictive summaries on the simplex from ILR Gaussian params.

    path = predictive mean ilr_inv(mu) — a valid simplex, the point
    forecast. lower/upper = component-wise 5%/95% quantiles of MC
    draws — MARGINAL predictive intervals for display. They are not
    joint compositional intervals and need not sum to 1.
    """
    rng = np.random.default_rng(p["seed"])
    draws = rng.normal(
        loc=mu, scale=np.sqrt(np.maximum(var, 0)),
        size=(p["n_samples"], len(mu)))
    S = ilr_inv(draws)
    return {
        "path": ilr_inv(mu),
        "lower": np.quantile(S, 0.05, axis=0),
        "upper": np.quantile(S, 0.95, axis=0),
    }


def forecast_f2_multi_origin(
    Z: np.ndarray,
    w: np.ndarray,
    n_eff: np.ndarray,
    horizon: int = HYBRID_HORIZON,
    origins: list[int] | None = None,
    params: dict | None = None,
) -> dict:
    """F2 at one horizon with the multi-origin stability mixture.

    Returns the PoC leaf for the horizon: path/lower/upper (K,)
    simplex summaries, origins actually used, per-origin simplex
    points, and F2 extras (from the k=0 origin only).

    Raises ValueError when even the k=0 origin lacks the history
    the horizon needs (horizon + min_days days).
    """
    p = dict(DEFAULTS)
    p.update(params or {})
    Z = np.asarray(Z, dtype=float)
    w = np.asarray(w, dtype=float)
    n_eff = np.asarray(n_eff, dtype=float)
    origins = sorted(set(int(o) for o in (origins if origins is not None
                                           else HYBRID_ORIGINS)))
    if any(o < 0 for o in origins):
        raise ValueError("origins must be non-negative")
    need = horizon + p["min_days"]
    per_origin: dict[int, dict] = {}
    for k in origins:
        if len(Z) - k < need:
            continue
        Zk = Z[: len(Z) - k] if k else Z
        wk = w[: len(w) - k] if k else w
        nk = n_eff[: len(n_eff) - k] if k else n_eff
        try:
            raw = f2_regime(Zk, wk, nk, [horizon], p)
        except ValueError:
            if k == 0:
                raise
            continue
        per_origin[k] = raw
    if 0 not in per_origin:
        raise ValueError("no usable forecast origin (history too short)")
    mus = np.array([per_origin[k][horizon]["mu"] for k in per_origin])
    vrs = np.array([per_origin[k][horizon]["var"] for k in per_origin])
    mu_bar = mus.mean(axis=0)
    var_bar = vrs.mean(axis=0) + mus.var(axis=0)
    q = _simplex_quantiles(mu_bar, var_bar, p)
    leaf = {
        "path": q["path"].tolist(),
        "lower": q["lower"].tolist(),
        "upper": q["upper"].tolist(),
        "origins": sorted(per_origin),
        "origin_points": {
            str(k): _simplex_quantiles(
                per_origin[k][horizon]["mu"],
                per_origin[k][horizon]["var"], p)["path"].tolist()
            for k in per_origin},
    }
    for key in ("p_change", "regime_now", "regime_dist"):
        if key in per_origin[0][horizon]:
            leaf[key] = per_origin[0][horizon][key]
    return leaf
