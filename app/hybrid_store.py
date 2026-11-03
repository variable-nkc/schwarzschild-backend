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
- Gap days (joint None) are forward-filled for the model only;
  the raw history returned for display keeps the gaps as nulls.
  Filled days carry weight ~0 in trend fits (their joint_volume
  is 0), so the fill preserves daily spacing without pulling
  trends. ``series.n_filled`` counts them.
- Weights and information stock are both daily joint_volume
  (see hybrid_forecast's header for why).
- History too short for even the k=0 origin (60 + 20 = 80 days)
  returns status "insufficient_history" (HTTP 200, never 404) —
  the figure shows a notice, not a crash. Unknown dyads return
  status "no_data" the same way.
"""

from __future__ import annotations

from datetime import date, timedelta
import sqlite3

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
) -> dict:
    """Hybrid forecast for one dyad: model / 60-day / multi-origin.

    Model defaults to F2 (O1 pin); F0p/F0m/F1 selectable. F3 is
    not offered (no uniqueness series). Unknown model raises
    ValueError before any DB work — the route turns it into
    a 400, not a 503.
    """
    if model not in hybrid_forecast.HYBRID_MODELS:
        raise ValueError(f"unknown hybrid model: {model!r}")
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
    joints: list[dict | None] = []
    vols: list[float] = []
    filled: list[bool] = []
    last: dict | None = None
    for r in rows:
        dates.append(r["as_of"])
        if r["joint"] is None:
            joints.append(last)
            filled.append(True)
        else:
            last = r["joint"]
            joints.append(r["joint"])
            filled.append(False)
        vols.append(float(r["joint_volume"]))
    first_known = next((i for i, j in enumerate(joints) if j is not None),
                       None)
    if first_known is None:
        return {"status": "no_data", "dyad": f"{iso1}_{iso2}",
                "as_of": window_end, "requested_as_of": as_of}
    dates = dates[first_known:]
    joints = joints[first_known:]
    vols = vols[first_known:]
    filled = filled[first_known:]
    n_filled = sum(1 for f in filled if f)
    history = [
        {"date": r["as_of"],
         **({p: r["joint"][p] for p in PHASES} if r["joint"] is not None
            else {p: None for p in PHASES})}
        for r in rows[-HISTORY_DAYS:]
    ]
    series_rows = [[joints[i][p] for p in PHASES]
                   for i in range(len(joints))]
    try:
        leaf = hybrid_forecast.forecast_multi_origin(
            model,
            hybrid_forecast.ilr(series_rows), vols, vols,
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
        "series": {"n_days": len(dates), "n_filled": n_filled,
                   "first_date": dates[0], "last_date": dates[-1]},
    }
