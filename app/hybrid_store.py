"""Hybrid forecast (F2, 60-day, multi-origin) over DDA daily joints.

O1 pin (Dan, 2026-11-03): PoC model F2, horizon 60 days,
multi-origin stability check. The math lives in
``hybrid_forecast`` (ported from dan/poc); this module only
builds the input series and shapes the response.

Series construction (the port decisions, all surfaced in the
response so the figure can caption them):
- Input = daily DDA joints (obs-weighted closed geometric mean
  of the day's code simplexes) over a 270-day window ending at
  the effective as-of. 270 covers the origin span (125 days for
  all five origins), the 180-day regime window, and the 180-day
  history the R&D figure draws.
- Effective as-of follows LC-07 exactly: the dyad's latest
  rollup as-of <= requested when a rollup row exists, else the
  requested date verbatim (or the latest DDA date when omitted).
- Input = the decayed sequential-Bayesian trend of the daily
  DDA joints (PoC series.py/api.py): posterior S, effective
  weights w_used, information stock N_eff. Gap days (joint
  None, volume 0) are NOT filled for the model: they enter
  as NaN rows with W = 0, which the trend ignores — the
  posterior carries forward, N_eff decays. The raw history
  returned for display keeps the gaps as nulls.
  ``series.n_filled`` still counts gap days in the window.
- Model triple is (ILR(S), w_used, N_eff), as in the PoC
  pipeline — not (ILR(raw joints), vols, vols).
- History too short for even the k=0 origin (60 + 20 = 80 days)
  returns status "insufficient_history" (HTTP 200, never 404) —
  the figure shows a notice, not a crash. Unknown dyads return
  status "no_data" the same way.
- Response also carries trend_series: the sequential-Bayesian
  posterior per history date (same dates as history, aligned by
  date not position; nulls only before the first observed
  joint). The R&D figure draws its history lines from
  trend_series, so the lines move with the Trend inputs. Raw
  history stays as input documentation.
"""

from __future__ import annotations

from datetime import date, timedelta
import sqlite3

import numpy as np

from . import dev_dda_store, hybrid_forecast, lc_store

HYBRID_WINDOW = 270
HISTORY_DAYS = 180
PHASES = ["p1", "p2", "p3", "p4", "p5"]
PHASE_NAMES = ["durable peace", "stable peace", "unstable peace",
               "crisis", "war"]


def fetch_hybrid(
    rollup_conn: sqlite3.Connection | None,
    dda_conn: sqlite3.Connection,
    simplexes: dict,
    iso1: str,
    iso2: str,
    as_of: str | None,
    model: str = hybrid_forecast.HYBRID_MODEL,
    half_life_days: float = 7.0,
    w_cap: float = 0.0,
    w_alpha: float = 1.0,
) -> dict:
    """Hybrid forecast for one dyad: model / 60-day / multi-origin.

    Model defaults to F2 (O1 pin); F0p/F0m/F1 selectable. F3 is
    not offered (no uniqueness series). The model input is the
    decayed sequential-Bayesian trend of the daily joints (PoC
    series.py/api.py): posterior S, effective weights w_used,
    information stock N_eff — not the raw joints. Defaults
    (Dan, 2026-11-03): half-life 7 days, w_cap 0 (= uncapped,
    the PoC UI convention), w_alpha 1. Bad params raise
    ValueError before any DB work — the route turns them into
    a 400, not a 503.
    """
    if model not in hybrid_forecast.HYBRID_MODELS:
        raise ValueError(f"unknown hybrid model: {model!r}")
    if half_life_days <= 0:
        raise ValueError("half-life must be positive")
    if w_cap < 0:
        raise ValueError("day-weight cap must be non-negative")
    if w_alpha < 0:
        raise ValueError("day-weight exponent must be non-negative")
    effective = None
    if rollup_conn is not None:
        effective = lc_store.resolve_rollup_as_of(
            rollup_conn, iso1, iso2, as_of)
    call_as_of = effective if effective is not None else as_of
    header, rows = dev_dda_store.fetch_joint(
        dda_conn, simplexes, iso1, iso2, call_as_of, HYBRID_WINDOW)
    window_end = header["as_of"]
    if window_end is None or not rows:
        return {"status": "no_data", "dyad": f"{iso1}_{iso2}",
                "as_of": None, "requested_as_of": as_of}
    dates: list[str] = []
    raw: list[dict | None] = []
    vols: list[float] = []
    filled: list[bool] = []
    for r in rows:
        dates.append(r["as_of"])
        raw.append(r["joint"])
        filled.append(r["joint"] is None)
        vols.append(float(r["joint_volume"]))
    first_known = next((i for i, j in enumerate(raw) if j is not None),
                       None)
    if first_known is None:
        return {"status": "no_data", "dyad": f"{iso1}_{iso2}",
                "as_of": window_end, "requested_as_of": as_of}
    dates = dates[first_known:]
    raw = raw[first_known:]
    vols = vols[first_known:]
    filled = filled[first_known:]
    n_filled = sum(1 for f in filled if f)
    history = [
        {"date": r["as_of"],
         **({p: r["joint"][p] for p in PHASES} if r["joint"] is not None
            else {p: None for p in PHASES})}
        for r in rows[-HISTORY_DAYS:]
    ]
    # Model input: the sequential-Bayesian trend triple, not the
    # raw joints. Gap days (joint None, volume 0) become NaN
    # rows with W = 0, which sequential_posteriors ignores:
    # the posterior carries forward, N_eff decays. No
    # forward-fill is needed for the model input anymore.
    daily = np.array(
        [[j[p] for p in PHASES] if j is not None else [np.nan] * len(PHASES)
         for j in raw], dtype=float)
    trend = hybrid_forecast.sequential_posteriors(
        daily, np.array(vols, dtype=float), half_life_days,
        w_cap=(w_cap if w_cap > 0 else None), w_alpha=w_alpha)
    # trend_series: the posterior per HISTORY date, aligned by
    # date (not position — the trimmed series can be shorter
    # than the window when the dyad's data starts late). Gap
    # days inside the series carry a posterior (carried
    # forward), so only pre-first-data dates are null.
    post_by_date = {d: trend["posterior"][i]
                    for i, d in enumerate(dates)}
    trend_series = [
        {"date": h["date"],
         **({p: float(post_by_date[h["date"]][j])
             for j, p in enumerate(PHASES)}
            if h["date"] in post_by_date
            else {p: None for p in PHASES})}
        for h in history
    ]
    try:
        leaf = hybrid_forecast.forecast_multi_origin(
            model,
            hybrid_forecast.ilr(trend["posterior"]),
            trend["weights"], trend["n_eff"],
            hybrid_forecast.HYBRID_HORIZON,
            hybrid_forecast.HYBRID_ORIGINS)
    except ValueError as exc:
        return {"status": "insufficient_history",
                "dyad": f"{iso1}_{iso2}", "as_of": window_end,
                "requested_as_of": as_of, "detail": str(exc),
                "model": model,
                "n_days": len(dates),
                "required_days": hybrid_forecast.HYBRID_HORIZON + 20}
    end = date.fromisoformat(window_end)
    targets = {str(k): (end + timedelta(
        days=hybrid_forecast.HYBRID_HORIZON - k)).isoformat()
        for k in leaf["origins"]}
    return {
        "status": "ok",
        "dyad": f"{iso1}_{iso2}",
        "as_of": window_end,
        "requested_as_of": as_of,
        "model": model,
        "horizon": hybrid_forecast.HYBRID_HORIZON,
        "trend": {"half_life_days": half_life_days,
                  "w_cap": w_cap, "w_alpha": w_alpha},
        "origins": leaf["origins"],
        "origin_targets": targets,
        "origin_points": leaf["origin_points"],
        "phases": PHASE_NAMES,
        "path": leaf["path"],
        "lower": leaf["lower"],
        "upper": leaf["upper"],
        "regime_now": leaf.get("regime_now"),
        "p_change": leaf.get("p_change"),
        "regime_dist": leaf.get("regime_dist"),
        "history": history,
        "trend_series": trend_series,
        "series": {"n_days": len(dates), "n_filled": n_filled,
                   "first_date": dates[0], "last_date": dates[-1]},
    }
