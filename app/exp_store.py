"""Internal data access for the experimental ``/exp/`` endpoints.

This module is the only place the ``/exp/`` routes touch SQLite.
It mirrors ``app/store.py``'s discipline: plain ``sqlite3``, plain
dicts, read-only connections (``mode=ro``), a fresh connection per
call, no DB mutation, no new indexes, no new DB files.

Raw rows only. Nothing in here computes shares, joints, PCS values,
anomalies, or forecasts: the ``/exp/`` endpoints deliver stored
rows (rollup rows, DDA rows) and the clients do the analytics
locally, exactly as they do today against local files. The one
exception to "rows verbatim" is the same boolean conversion v1
applies to rollup rows (see ``app.store._row_to_dict``): INTEGER
0/1 flag columns surface as JSON booleans, so an ``/exp/`` rollup
row has the same shape as a v1 ``/dyad-status`` row.

The DDA source is the sidecar file (table ``dda``), not the mirror:
``DDA_SQLITE`` must point at a sidecar-shaped file. A mirror-shaped
file (table ``analytics_dyad_date_actions``) is not read here, so a
misconfigured path fails loudly as a DB error instead of silently
serving from the wrong table.
"""

import sqlite3
from pathlib import Path

from .store import _row_to_dict


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_dda_records(path: Path, iso1: str, iso2: str) -> list[dict]:
    """Return one dyad's raw DDA rows, oldest date first.

    Each row is ``{obs_date, cameo_code, obs_count}``. ``cameo_code``
    is always a JSON string: the sidecar stores it as TEXT, and the
    cast here guards against an INTEGER-stored code (which would
    otherwise lose a leading zero, e.g. ``"010"`` -> ``10``).
    An unknown dyad returns an empty list, not an error.
    """
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT obs_date, cameo_code, obs_count FROM dda "
            "WHERE iso1 = ? AND iso2 = ? "
            "ORDER BY obs_date ASC, cameo_code ASC",
            (iso1, iso2),
        ).fetchall()
    return [
        {
            "obs_date": r["obs_date"],
            "cameo_code": str(r["cameo_code"]),
            "obs_count": r["obs_count"],
        }
        for r in rows
    ]


def fetch_corpus(path: Path, iso1: str, iso2: str) -> list[dict]:
    """Return the target dyad's referent (corpus) DDA rows.

    Referent rule, mirrored exactly from the dan/poc standalone
    harness (``experiments/poc_app.py:get_corpus``): a dyad
    ``(s, t)`` is a referent of target ``(a, b)`` when it shares a
    country with the target — ``s == a or t == a or s == b or
    t == b`` — excluding the target dyad itself (``s == a and
    t == b``) and its exact inverse (``s == b and t == a``).
    Direction matters: ``USA_CAN`` and ``CAN_USA`` are different
    dyads, and both are excluded here (the target and its inverse);
    every other dyad sharing either country is included.
    Rows carry dyad identity (``iso1, iso2``) because TF-IDF
    groups by dyad, and are ordered by dyad, date, then code for a
    deterministic payload. An unknown target returns an empty list
    (its referents may still be non-empty only if the target's
    countries appear at all — the rule is purely on the codes).
    """
    a, b = iso1, iso2
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT iso1, iso2, obs_date, cameo_code, obs_count "
            "FROM dda "
            "WHERE (iso1 IN (?, ?) OR iso2 IN (?, ?)) "
            "AND NOT (iso1 = ? AND iso2 = ?) "
            "AND NOT (iso1 = ? AND iso2 = ?) "
            "ORDER BY iso1 ASC, iso2 ASC, obs_date ASC, "
            "cameo_code ASC",
            (a, b, a, b, a, b, b, a),
        ).fetchall()
    return [
        {
            "iso1": r["iso1"],
            "iso2": r["iso2"],
            "obs_date": r["obs_date"],
            "cameo_code": str(r["cameo_code"]),
            "obs_count": r["obs_count"],
        }
        for r in rows
    ]


def fetch_rollup_at(path: Path, as_of: str) -> list[dict]:
    """Return full rollup rows at one as-of, ordered by dyad.

    No field trimming: every stored column rides along, in the
    same shape as a v1 row. An as-of with no rows returns an empty
    list, not an error.
    """
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM dyad_status_rollup "
            "WHERE as_of = ? ORDER BY iso1 ASC, iso2 ASC",
            (as_of,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def fetch_rollup_dyad(path: Path, iso1: str, iso2: str) -> list[dict]:
    """Return one dyad's full rollup rows, oldest as-of first."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM dyad_status_rollup "
            "WHERE iso1 = ? AND iso2 = ? ORDER BY as_of ASC",
            (iso1, iso2),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def fetch_notable(path: Path, limit: int) -> list[dict]:
    """Return the most active dyads by peak weekly volume.

    Mirrors ``gallery_data.notable_dyads`` (analytics repo,
    ``muse/viz-gallery``): ``MAX(v_7d)`` grouped by dyad, ordered
    by that peak descending, capped at ``limit``. The gallery's
    column is ``peak_v7``; the API names it ``peak_v_7d`` to match
    the rollup column it peaks over. No other ranking is applied.
    """
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT iso1, iso2, MAX(v_7d) AS peak_v_7d "
            "FROM dyad_status_rollup "
            "GROUP BY iso1, iso2 ORDER BY peak_v_7d DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "iso1": r["iso1"],
            "iso2": r["iso2"],
            "peak_v_7d": r["peak_v_7d"],
        }
        for r in rows
    ]


def fetch_dda_stats(path: Path) -> dict:
    """Return cheap whole-sidecar facts for ``/exp/meta``."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MIN(obs_date) AS mn, MAX(obs_date) AS mx, "
            "COUNT(*) AS n FROM dda"
        ).fetchone()
        dyads = conn.execute(
            "SELECT COUNT(*) AS n FROM "
            "(SELECT DISTINCT iso1, iso2 FROM dda)"
        ).fetchone()
    return {
        "dda_min_date": row["mn"],
        "dda_max_date": row["mx"],
        "dda_rows": row["n"],
        "dda_dyads": dyads["n"],
    }


def fetch_rollup_meta(path: Path) -> dict:
    """Return cheap whole-rollup facts for ``/exp/meta``."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MIN(as_of) AS mn, MAX(as_of) AS mx "
            "FROM dyad_status_rollup"
        ).fetchone()
        dyads = conn.execute(
            "SELECT COUNT(*) AS n FROM "
            "(SELECT DISTINCT iso1, iso2 FROM dyad_status_rollup)"
        ).fetchone()
        prov = conn.execute(
            "SELECT build_id, vintage FROM dyad_status_rollup LIMIT 1"
        ).fetchone()
    return {
        "rollup_min_asof": row["mn"],
        "rollup_max_asof": row["mx"],
        "rollup_dyads": dyads["n"],
        "rollup_build_id": prov["build_id"] if prov is not None else None,
        "rollup_vintage": prov["vintage"] if prov is not None else None,
    }
