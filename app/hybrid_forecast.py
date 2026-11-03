"""F2 regime forecast with multi-origin mixture, ported from the PoC.

Provenance: ``dan/poc`` @ ``15b0e41``, ``src/seqbayes/forecast.py``
(``f2_regime``, ``forecast_from_arrays`` F2 branch) and
``src/seqbayes/geometry.py`` (``ilr_basis``/``ilr``/``ilr_inv``).
Same formulas, reimplemented in stdlib-only Python — the backend
venv carries no numpy (see requirements.txt), and Dan's pickup is
pull-and-restart with no pip step, so no new dependency is added.
Do not "improve" the math here — improvements belong in the PoC,
and this module is re-ported from there.

Two deliberate deltas from the PoC code, both documented, neither
a formula change:
- WLS trend is degree 1, so the lstsq fit is replaced by its
  closed form (identical solution for a line).
- MC draws for simplex quantiles use ``random.Random(0).gauss``
  instead of numpy's ``default_rng(0)`` — a different RNG stream,
  so quantiles differ from a PoC run by Monte Carlo noise only
  (same 500 draws, same seed concept).

Restriction: only the F2 rung is ported. The PoC ladder's F0/F1/F3
rungs are omitted deliberately — O1 pins F2, and a port that
silently offered other models would reopen a closed decision.

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

import math
import random

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


def _closure(row: list[float]) -> list[float]:
    s = sum(row)
    return [v / s for v in row]


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    s = sum(row)
    return [v / s for v in row]


def ilr_basis(d: int) -> list[list[float]]:
    """Default orthonormal ILR basis (d, d-1) via Helmert contrasts."""
    helm = [[0.0] * (d - 1) for _ in range(d)]
    for j in range(d - 1):
        for i in range(j + 1):
            helm[i][j] = 1.0
        helm[j + 1][j] = -(j + 1)
    for j in range(d - 1):
        norm = math.sqrt((j + 1) * (j + 2))
        for i in range(d):
            helm[i][j] /= norm
    return helm


def _clr(row: list[float]) -> list[float]:
    lx = [math.log(v) for v in row]
    m = sum(lx) / len(lx)
    return [v - m for v in lx]


def ilr(x: list[list[float]], v: list[list[float]] | None = None,
        ) -> list[list[float]]:
    """ILR transform: simplex rows (..., d) -> R^(d-1)."""
    for row in x:
        if any(val <= 0 for val in row):
            raise ValueError(
                "ilr: x must be strictly positive (zero-replace first)")
    d = len(x[0])
    if v is None:
        v = ilr_basis(d)
    out = []
    for row in x:
        c = _clr(row)
        out.append([sum(c[i] * v[i][j] for i in range(d))
                    for j in range(d - 1)])
    return out


def ilr_inv(z: list[list[float]], v: list[list[float]] | None = None,
            ) -> list[list[float]]:
    """Inverse ILR: R^(d-1) rows -> simplex rows (..., d)."""
    d = len(z[0]) + 1
    if v is None:
        v = ilr_basis(d)
    out = []
    for row in z:
        out.append(_closure(
            [math.exp(sum(row[j] * v[i][j] for j in range(d - 1)))
             for i in range(d)]))
    return out


def _check_history(n: int, need: int, what: str) -> None:
    if n < need:
        raise ValueError(f"{what}: need >= {need} days of history, got {n}")


def _wls_trend(Z: list[list[float]], w: list[float]):
    """Weighted least squares line per ILR dim (closed form).

    Returns (coef (2, d), resid_var (d,)) with the PoC's weight
    normalization (wn sums to L) and dof = max(L - 2, 1).
    """
    L = len(Z)
    d = len(Z[0])
    wsum = sum(w)
    wn = [wi / wsum * L for wi in w]
    s = list(range(L))
    sw = sum(wn[i] * s[i] for i in range(L)) / L
    s_var = sum(wn[i] * (s[i] - sw) ** 2 for i in range(L))
    coef = []
    for j in range(d):
        yw = sum(wn[i] * Z[i][j] for i in range(L)) / L
        slope = (sum(wn[i] * (s[i] - sw) * (Z[i][j] - yw)
                     for i in range(L)) / s_var) if s_var > 0 else 0.0
        coef.append((yw - slope * sw, slope))
    dof = max(L - 2, 1)
    var = []
    for j in range(d):
        a, b = coef[j]
        var.append(sum(wn[i] * (Z[i][j] - (a + b * s[i])) ** 2
                       for i in range(L)) / dof)
    return coef, var


def _neff_scale(n_eff_now: float, n_eff_ref: float) -> float:
    """Inflate uncertainty when current information stock is thin."""
    if n_eff_ref <= 0:
        return 1.0
    return min(max(n_eff_ref / max(n_eff_now, 1e-9), 1.0), 4.0)


def _mat_pow_2x2(P: list[list[float]], h: int) -> list[list[float]]:
    out = [[1.0, 0.0], [0.0, 1.0]]
    for _ in range(h):
        out = [[out[0][0] * P[0][0] + out[0][1] * P[1][0],
                out[0][0] * P[0][1] + out[0][1] * P[1][1]],
               [out[1][0] * P[0][0] + out[1][1] * P[1][0],
                out[1][0] * P[0][1] + out[1][1] * P[1][1]]]
    return out


def _regime_states(Z: list[list[float]], p: dict):
    """Calm/agitated regimes from trailing volatility of ILR increments.

    Returns (regimes (T,) bool agitated, P (2,2) transition matrix,
    threshold used).
    """
    Wv = p["vol_window"]
    step = [math.sqrt(sum((Z[i + 1][j] - Z[i][j]) ** 2
                          for j in range(len(Z[0]))))
            for i in range(len(Z) - 1)]
    v = []
    for i in range(len(step)):
        window = step[max(0, i - Wv + 1): i + 1]
        m = sum(window) / len(window)
        v.append(math.sqrt(sum((x - m) ** 2 for x in window) / len(window)))
    v = [v[0]] + v
    Wr = min(p["regime_window"], len(v))
    thr = _median(v[-Wr:])
    agit = [x > thr for x in v]
    r = [1 if a else 0 for a in agit[-Wr:]]
    counts = [[1.0, 1.0], [1.0, 1.0]]
    for a, b in zip(r[:-1], r[1:]):
        counts[a][b] += 1
    P = [[counts[i][j] / sum(counts[i]) for j in (0, 1)] for i in (0, 1)]
    return agit, P, thr


def f2_regime(
    Z: list[list[float]],
    w: list[float],
    n_eff: list[float],
    horizons: list[int],
    p: dict,
) -> dict:
    """F2: 2-state Markov regime + per-regime F1 trends (mixture forecast)."""
    L = min(p["trend_window"], len(Z))
    _check_history(len(Z), p["min_days"], "F2")
    agit, P, thr = _regime_states(Z, p)
    r0 = 1 if agit[-1] else 0
    reg = {}
    for r in (0, 1):
        idx = [i for i in range(L)
               if (not agit[-L + i] if r == 0 else agit[-L + i])]
        if len(idx) < 10:
            idx = list(range(L))
        Zr = [Z[-L + i] for i in idx]
        wr = [w[-L + i] if w[-L + i] > 0 else 1e-9 for i in idx]
        reg[r] = _wls_trend(Zr, wr)
    tau = p["tau"] or L
    scale_n = _neff_scale(n_eff[-1], _median(n_eff[-L:]))
    s_last = L - 1
    pi0 = [1.0, 0.0] if r0 == 0 else [0.0, 1.0]
    out = {}
    for h in horizons:
        Ph = _mat_pow_2x2(P, h)
        pi_h = [pi0[0] * Ph[0][0] + pi0[1] * Ph[1][0],
                pi0[0] * Ph[0][1] + pi0[1] * Ph[1][1]]
        mus, vars_ = [], []
        for r in (0, 1):
            coef, rvar = reg[r]
            s = s_last + h
            mus.append([coef[j][0] + coef[j][1] * s
                        for j in range(len(Z[0]))])
            vars_.append([rv * (1.0 + h / tau) * scale_n for rv in rvar])
        d = len(Z[0])
        mu = [sum(pi_h[r] * mus[r][j] for r in (0, 1)) for j in range(d)]
        var = [sum(pi_h[r] * (vars_[r][j] + (mus[r][j] - mu[j]) ** 2)
                   for r in (0, 1)) for j in range(d)]
        p_change = 1.0 - Ph[r0][r0]
        out[h] = {"mu": mu, "var": var, "p_change": p_change,
                  "regime_now": "agitated" if r0 else "calm",
                  "regime_dist": pi_h}
    return out


def _simplex_quantiles(mu: list[float], var: list[float], p: dict):
    """Predictive summaries on the simplex from ILR Gaussian params.

    path = predictive mean ilr_inv(mu) — a valid simplex, the point
    forecast. lower/upper = component-wise 5%/95% quantiles of MC
    draws — MARGINAL predictive intervals for display. They are not
    joint compositional intervals and need not sum to 1.
    """
    rng = random.Random(p["seed"])
    draws = [[rng.gauss(mu[j], math.sqrt(max(var[j], 0.0)))
              for j in range(len(mu))] for _ in range(p["n_samples"])]
    S = ilr_inv(draws)
    # Columns of S are simplex components (5), NOT ILR dims (4):
    # iterating over len(mu) here would silently drop the last
    # component (caught live 2026-11-03 — zip had masked it).
    d = len(S[0])
    lower, upper = [], []
    for j in range(d):
        col = sorted(row[j] for row in S)
        for q, acc in ((0.05, lower), (0.95, upper)):
            pos = q * (len(col) - 1)
            lo, hi = int(math.floor(pos)), int(math.ceil(pos))
            acc.append(col[lo] + (col[hi] - col[lo]) * (pos - lo))
    return {"path": ilr_inv([mu])[0], "lower": lower, "upper": upper}


def forecast_f2_multi_origin(
    Z: list[list[float]],
    w: list[float],
    n_eff: list[float],
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
    d = len(Z[0])
    mus = [per_origin[k][horizon]["mu"] for k in per_origin]
    vrs = [per_origin[k][horizon]["var"] for k in per_origin]
    n = len(mus)
    mu_bar = [sum(m[j] for m in mus) / n for j in range(d)]
    var_bar = []
    for j in range(d):
        mean_var = sum(v[j] for v in vrs) / n
        mean_mu = mu_bar[j]
        var_mu = sum((m[j] - mean_mu) ** 2 for m in mus) / n
        var_bar.append(mean_var + var_mu)
    q = _simplex_quantiles(mu_bar, var_bar, p)
    leaf = {
        "path": q["path"],
        "lower": q["lower"],
        "upper": q["upper"],
        "origins": sorted(per_origin),
        "origin_points": {
            str(k): _simplex_quantiles(
                per_origin[k][horizon]["mu"],
                per_origin[k][horizon]["var"], p)["path"]
            for k in per_origin},
    }
    for key in ("p_change", "regime_now", "regime_dist"):
        if key in per_origin[0][horizon]:
            leaf[key] = per_origin[0][horizon][key]
    return leaf
