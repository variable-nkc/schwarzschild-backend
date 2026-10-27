"""Internal data access for schwarzschild-backend v1.

This module is the only place that touches SQLite. It uses plain
``sqlite3`` and plain dicts — no pandas, no ORM. The FastAPI layer
(``app/main.py``) and the pydantic models (``app/models.py``) sit on
top of it; nothing in here knows about HTTP.

All connections are read-only (``mode=ro``). Callers check that the
configured path exists before calling these functions, so a missing
file surfaces as a clean "not configured" response, not a crash.
"""

import sqlite3
from pathlib import Path

from .config import BOOLEAN_COLUMNS, LABEL_PRIORITY, SETS


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite Row to a JSON-ready dict.

    The only transformation is INTEGER 0/1 -> bool for the known
    boolean columns (see config.BOOLEAN_COLUMNS). Everything else is
    returned exactly as stored, so a dyad-status response is the full
    rollup row, not a reduced projection.
    """
    out = dict(row)
    for col in BOOLEAN_COLUMNS:
        if col in out and out[col] is not None:
            out[col] = bool(out[col])
    return out


def fetch_provenance(path: Path) -> tuple | None:
    """Return one (build_id, vintage, computed_at) tuple from the DB.

    A rollup database is built in one run, so these are uniform across
    rows; v1 takes the first row rather than scanning the table.
    Returns None when the table is empty.
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT build_id, vintage, computed_at "
            "FROM dyad_status_rollup LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return (row["build_id"], row["vintage"], row["computed_at"])


def fetch_dyad(
    path: Path, iso1: str, iso2: str, as_of: str | None
) -> dict | None:
    """Return one full rollup row, or None when there is no such row.

    With no as_of, the dyad's latest as-of is used.
    """
    with _connect(path) as conn:
        if as_of is None:
            row = conn.execute(
                "SELECT * FROM dyad_status_rollup "
                "WHERE iso1 = ? AND iso2 = ? "
                "ORDER BY as_of DESC LIMIT 1",
                (iso1, iso2),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM dyad_status_rollup "
                "WHERE iso1 = ? AND iso2 = ? AND as_of = ?",
                (iso1, iso2, as_of),
            ).fetchone()
    return _row_to_dict(row) if row is not None else None


def latest_as_of(path: Path) -> str | None:
    """Return the latest as_of in the rollup, or None when empty."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MAX(as_of) AS m FROM dyad_status_rollup"
        ).fetchone()
    return row["m"] if row is not None else None


def fetch_watchlist(
    path: Path,
    as_of: str | None,
    labels: list[str] | None,
    limit: int,
) -> tuple[str | None, list[dict]]:
    """Return (effective as_of, rows) for one as-of.

    Rows are full rollup rows, in hot-first label-priority order as a
    first pass (see config.LABEL_PRIORITY), then by dyad for a
    deterministic order within a label. With no as_of, the latest
    as-of in the DB is used. An as-of with no rows returns an empty
    list, not an error: an empty watchlist is a legitimate answer.
    """
    effective = as_of if as_of is not None else latest_as_of(path)
    if effective is None:
        return None, []
    case = " ".join(
        f"WHEN '{label}' THEN {i}" for i, label in enumerate(LABEL_PRIORITY)
    )
    sql = (
        "SELECT * FROM dyad_status_rollup WHERE as_of = ? "
        + ("AND label IN (%s) " % ",".join("?" * len(labels)) if labels else "")
        + f"ORDER BY CASE label {case} ELSE {len(LABEL_PRIORITY)} END, "
        + "iso1, iso2 LIMIT ?"
    )
    params: list = [effective]
    if labels:
        params.extend(labels)
    params.append(limit)
    with _connect(path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return effective, [_row_to_dict(r) for r in rows]


def fetch_history(path: Path, iso1: str, iso2: str) -> list[dict]:
    """Return all rollup rows for one dyad, oldest as-of first."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM dyad_status_rollup "
            "WHERE iso1 = ? AND iso2 = ? ORDER BY as_of ASC",
            (iso1, iso2),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def build_series(iso1: str, iso2: str, rows: list[dict]) -> dict:
    """Build the v1 PCS-series payload from rollup history rows.

    This is a *rollup-history* series, not the PCS panel series: it
    carries what the rollup stores per as-of (per-set R7/R28
    direction and std_resid, R7 share, temperature, label, and the
    R7 support fields). The panel-only fields (baseline mu, phi,
    Beta tails) are not in the rollup and are deliberately not
    invented here — serving them is a later object (see README,
    "Not built yet").
    """
    series_rows = []
    for row in rows:
        per_set = {}
        for name in SETS:
            per_set[name] = {
                "direction_r7": row.get(f"dir_r7_{name}"),
                "share_r7": row.get(f"share_r7_{name}"),
                "std_resid_r7": row.get(f"std_r7_{name}"),
                "direction_r28": row.get(f"dir_r28_{name}"),
                "std_resid_r28": row.get(f"std_r28_{name}"),
            }
        series_rows.append(
            {
                "as_of": row.get("as_of"),
                "temperature": row.get("temperature"),
                "label": row.get("label"),
                "v_7d": row.get("v_7d"),
                "days_present_7": row.get("days_present_7"),
                "per_set": per_set,
            }
        )
    return {
        "dyad_id": f"{iso1}_{iso2}",
        "iso1": iso1,
        "iso2": iso2,
        "dates": [r["as_of"] for r in series_rows],
        "rows": series_rows,
    }
