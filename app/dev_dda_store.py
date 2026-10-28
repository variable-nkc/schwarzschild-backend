"""DEV ONLY data access for the DDA sidecar (dev/dda-sidecar branch).

Not part of the v1 contract. Plain dicts in, plain dicts out —
same split as ``app/store.py`` (pydantic lives at the boundary
in ``app/main.py``).

Two deliberate dev decisions, both flagged for formalization:

1. Daily set shares are computed HERE from DDA counts + sets
   membership (pooled counts per set / mapped volume, the PCS
   ``s_hat`` definition at daily grain). That is a derivation
   the v1 thin-serving rule would not do server-side; the
   contract decision (server computes vs serves raw counts and
   lets the frontend derive, as with the R28 reconstruction)
   is open until the spike measurements are in.
2. "Mapped" is the cameo_tokens.json code list (318 codes,
   verified identical to soc_long's distinct codes, 2026-10-28)
   — NOT the union of the five action sets. History: the dev
   endpoints first approximated mapped as the union (215
   codes), which measured 2026-10-28 as capturing only 43%
   of USA_CAN's trailing-60d mapped volume (4,979 of 11,568);
   Dan ordered the switch to the tokens list the same day.
   The tokens file is read here as a CODE LIST ONLY — its
   p-values are not read by any endpoint (the simplex
   extension remains future). The contract question that
   remains open is the source of truth for the mapped list
   (Dan-supplied JSON vs soc_long table vs a backend-carried
   copy); the dev endpoints use the JSON via DDA_SIMPLEXES_JSON.

A day with no mapped records yields a row with null shares
(a gap, never a zero), mirroring the rollup's treatment of
missing data.
"""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ACTION_SETS = (
    "escalatory",
    "instability",
    "stability",
    "deescalatory",
    "critical_incidents",
)


def connect(path: Path) -> sqlite3.Connection:
    """Fresh read-only connection (same discipline as store.py)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_sidecar_meta(conn: sqlite3.Connection) -> dict:
    """The sidecar's own stamp (_sidecar_meta), or {} when absent."""
    try:
        rows = conn.execute("SELECT key, value FROM _sidecar_meta").fetchall()
    except sqlite3.Error:
        return {}
    return {r["key"]: r["value"] for r in rows}


def load_sets(path: Path) -> dict[str, set[str]]:
    """Action-set membership from a cameo_groups.json file.

    Codes are compared as strings: DDA cameo_code is TEXT.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {s: {str(c) for c in data.get(s, [])} for s in ACTION_SETS}


def load_mapped(path: Path) -> set[str]:
    """Mapped code list from a cameo_tokens.json file.

    CODE LIST ONLY — p-values are not read here. The list is
    the 318-code soc_long set (verified identical, 2026-10-28).
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {str(e["code"]) for e in data if isinstance(e, dict) and "code" in e}


def _latest_as_of(conn: sqlite3.Connection, iso1: str, iso2: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(obs_date) AS m FROM dda WHERE iso1 = ? AND iso2 = ?",
        (iso1, iso2),
    ).fetchone()
    return row["m"] if row else None


def fetch_daily(
    conn: sqlite3.Connection,
    sets: dict[str, set[str]],
    mapped: set[str],
    iso1: str,
    iso2: str,
    as_of: str | None,
    window: int,
) -> tuple[dict, list[dict]]:
    """Per-day aggregates over the trailing window ending at as_of.

    Returns (header, rows): header carries the resolved as_of and
    window; rows are oldest first, one per calendar day in the
    window, with null shares on days with no mapped records.
    """
    if as_of is None:
        as_of = _latest_as_of(conn, iso1, iso2)
    if as_of is None:
        return {"dyad": f"{iso1}_{iso2}", "as_of": None, "window": window}, []
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=window - 1)
    # Per-day, per-code counts in the window (dyad-scoped; the
    # sidecar index (iso1, iso2, obs_date) serves this directly).
    day_codes: dict[str, list[tuple[str, int]]] = {}
    for r in conn.execute(
        "SELECT obs_date, cameo_code, obs_count FROM dda"
        " WHERE iso1 = ? AND iso2 = ? AND obs_date >= ? AND obs_date <= ?",
        (iso1, iso2, start.isoformat(), end.isoformat()),
    ):
        day_codes.setdefault(r["obs_date"], []).append(
            (r["cameo_code"], r["obs_count"])
        )
    rows = []
    day = start
    while day <= end:
        key = day.isoformat()
        codes = day_codes.get(key, [])
        volume_total = sum(c for _, c in codes)
        volume_mapped = sum(c for code, c in codes if code in mapped)
        per_set = {}
        for s, members in sets.items():
            total = sum(c for code, c in codes if code in members)
            per_set[s] = (total / volume_mapped) if volume_mapped else None
        rows.append(
            {
                "as_of": key,
                "volume_total": volume_total,
                "volume_mapped": volume_mapped,
                "per_set_share": per_set,
            }
        )
        day += timedelta(days=1)
    return (
        {"dyad": f"{iso1}_{iso2}", "as_of": as_of, "window": window},
        rows,
    )


def fetch_codes(
    conn: sqlite3.Connection,
    sets: dict[str, set[str]],
    mapped: set[str],
    iso1: str,
    iso2: str,
    as_of: str | None,
    window: int,
) -> tuple[dict, list[dict]]:
    """Per-code rows: as-of day + trailing-window aggregates.

    Baseline is PROVISIONAL (dev only): mean daily count over
    the window length (count_window / window). Formalization
    picks the baseline from the Part B2 candidates.
    Sorted by count at the as-of, descending, then code.
    """
    if as_of is None:
        as_of = _latest_as_of(conn, iso1, iso2)
    if as_of is None:
        return {"dyad": f"{iso1}_{iso2}", "as_of": None, "window": window}, []
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=window - 1)
    at: dict[str, int] = {}
    for r in conn.execute(
        "SELECT cameo_code, obs_count FROM dda"
        " WHERE iso1 = ? AND iso2 = ? AND obs_date = ?",
        (iso1, iso2, as_of),
    ):
        at[r["cameo_code"]] = at.get(r["cameo_code"], 0) + r["obs_count"]
    win: dict[str, int] = {}
    for r in conn.execute(
        "SELECT cameo_code, SUM(obs_count) AS s FROM dda"
        " WHERE iso1 = ? AND iso2 = ? AND obs_date >= ? AND obs_date <= ?"
        " GROUP BY cameo_code",
        (iso1, iso2, start.isoformat(), end.isoformat()),
    ):
        win[r["cameo_code"]] = r["s"]
    volume_mapped_at = sum(c for code, c in at.items() if code in mapped)
    rows = []
    for code in sorted(win, key=lambda c: (-at.get(c, 0), c)):
        code_sets = sorted(s for s, members in sets.items() if code in members)
        count_at = at.get(code, 0)
        rows.append(
            {
                "cameo_code": code,
                "sets": code_sets,
                "count_at_asof": count_at,
                "share_at_asof": (
                    (count_at / volume_mapped_at) if volume_mapped_at else None
                ),
                "count_window": win[code],
                "baseline_daily_mean": win[code] / window,
            }
        )
    return (
        {"dyad": f"{iso1}_{iso2}", "as_of": as_of, "window": window},
        rows,
    )
