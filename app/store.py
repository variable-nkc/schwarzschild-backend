"""Internal data access for schwarzschild-backend v1.

This module is the only place that touches SQLite. It uses plain
``sqlite3`` and plain dicts — no pandas, no ORM. The FastAPI layer
(``app/main.py``) and the pydantic models (``app/models.py``) sit on
top of it; nothing in here knows about HTTP.

All connections are read-only (``mode=ro``). Callers check that the
configured path exists before calling these functions, so a missing
file surfaces as a clean "not configured" response, not a crash.
"""

import bisect
import datetime
import sqlite3
from pathlib import Path

from .config import (
    BOOLEAN_COLUMNS,
    LABEL_PRIORITY,
    SETS,
    dda_path,
    dda_sets_path,
    predictive_path,
)
from .dev_dda_store import load_sets as _load_action_sets


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


def _attach_predictive(rows: list[dict]) -> list[dict]:
    """Attach the validated exceedance to rollup row dicts.

    Reads ``exc45_max`` / ``exc45_max_set`` from the predictive
    build's ``dyad_predictive_summary`` (max over the
    conflict-risk trio — escalatory, instability,
    critical_incidents; INTEGRATION_SPEC D18) and sets them on
    each row, matched on (iso1, iso2, as_of) via the summary's
    unique index. Legacy ``max_exc45`` / ``max_exc45_set`` are
    never modified — both are served during the transition.

    D18 files also carry ``exc45_calm_max`` on the summary (max
    over stability/deescalatory, NULL when neither emitted);
    when the column exists it is attached under the same key
    (None when the dyad has no summary row). Older summary
    tables without the column leave the key absent entirely,
    so a caller can tell "pre-D18 file" from "no calming row".

    The predictive DB is optional. When PREDICTIVE_SQLITE is unset
    or the file is missing, rows are returned untouched (no
    ``exc45_max`` key at all), so a caller can distinguish "not
    served" from "served, but this dyad has no validated-set row
    at this as-of" (key present, value None).
    """
    if not rows:
        return rows
    pp = predictive_path()
    if pp is None or not pp.exists():
        return rows
    with _connect(pp) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(dyad_predictive_summary)")}
        has_calm = "exc45_calm_max" in cols
        sel = ("SELECT exc45_max, exc45_max_set"
               + (", exc45_calm_max" if has_calm else "")
               + " FROM dyad_predictive_summary "
                 "WHERE iso1 = ? AND iso2 = ? AND as_of = ?")
        for row in rows:
            hit = conn.execute(
                sel, (row["iso1"], row["iso2"], row["as_of"]),
            ).fetchone()
            row["exc45_max"] = hit["exc45_max"] if hit else None
            row["exc45_max_set"] = hit["exc45_max_set"] if hit else None
            if has_calm:
                row["exc45_calm_max"] = (
                    hit["exc45_calm_max"] if hit else None)
    return _attach_support(rows)


# ---------------------------------------------------------------------
# Set support: the observation footing under the pill.
#
# The pill's mu is a volume-weighted mean of a *carry-forward* share
# series (seqbayes.setshare.daily_set_shares forward-fills each set
# over days it has no records), so for a sparse dyad a high
# exceedance can rest on a handful of genuine activity days — one
# strong day, carried for weeks. Support makes that footing
# visible: per set and as-of, how many of the trailing 90 days the
# set was genuinely observed on, and how long ago it was last seen.
# It annotates the pill; it never alters a served value.
# ---------------------------------------------------------------------

_SETS_BY_PATH: dict[str, dict[str, set[str]]] = {}


def _action_sets() -> dict[str, set[str]] | None:
    """CAMEO action-set membership for support counts, or None.

    Support is served only when the DDA sidecar and its sets JSON
    are both configured (the same pair the dev DDA endpoints use).
    The parsed sets are cached per path for the process lifetime —
    the file is a deployment constant.
    """
    dp, sp = dda_path(), dda_sets_path()
    if dp is None or sp is None or not dp.exists() or not sp.exists():
        return None
    key = str(sp)
    if key not in _SETS_BY_PATH:
        _SETS_BY_PATH[key] = _load_action_sets(sp)
    return _SETS_BY_PATH[key]


def _set_dates(
    conn: sqlite3.Connection, iso1: str, iso2: str, codes: set[str]
) -> list[str]:
    """Distinct obs_dates on which the dyad shows any code of one
    set, ascending — the set's genuine activity days in the
    sidecar."""
    if not codes:
        return []
    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT DISTINCT obs_date FROM dda "
        f"WHERE iso1 = ? AND iso2 = ? AND cameo_code IN ({marks}) "
        f"ORDER BY obs_date",
        (iso1, iso2, *sorted(codes)),
    ).fetchall()
    return [r[0] for r in rows]


def _support_at(dates: list[str], as_of: str) -> dict:
    """Support of one set at one as-of from its genuine-day list.

    ``genuine_days_90d`` counts genuine days in the trailing
    90-day window [as_of - 89d, as_of] — the window S0m averages
    over. ``last_seen_days_ago`` is the age of the most recent
    genuine day at or before the as-of (None when the set has
    never been seen for this dyad up to the as-of, within the
    sidecar's span).
    """
    a = datetime.date.fromisoformat(as_of)
    start = (a - datetime.timedelta(days=89)).isoformat()
    lo = bisect.bisect_left(dates, start)
    hi = bisect.bisect_right(dates, as_of)
    last = dates[hi - 1] if hi else None
    return {
        "genuine_days_90d": hi - lo,
        "last_seen_days_ago": (
            (a - datetime.date.fromisoformat(last)).days
            if last is not None
            else None
        ),
    }


_DDA_MAX_BY_PATH: dict[str, str | None] = {}


def _dda_max_date(conn: sqlite3.Connection, dp: Path) -> str | None:
    """Latest obs_date in the sidecar, cached per path.

    The sidecar is a deployment constant for the process lifetime
    (same footing as the sets JSON), so the max is read once. It
    bounds the realized-outcome gate: a forecast's 45-day horizon
    has elapsed only when as_of + 45d falls on or before this
    date.
    """
    key = str(dp)
    if key not in _DDA_MAX_BY_PATH:
        row = conn.execute("SELECT MAX(obs_date) FROM dda").fetchone()
        _DDA_MAX_BY_PATH[key] = row[0] if row else None
    return _DDA_MAX_BY_PATH[key]


def _daily_frame(
    conn: sqlite3.Connection, iso1: str, iso2: str,
    sets: dict[str, set[str]],
) -> tuple[list[str], list[float], dict[str, list[float]]]:
    """One dyad's daily counts as prefix sums over its active days.

    Returns (dates, vol_prefix, {set: num_prefix}): dates are the
    dyad's distinct obs_dates ascending, vol is a day's total
    obs_count over ALL its records (the pooled denominator), and
    num is a day's obs_count summed over a set's codes — per set,
    independently, because the sets overlap (a critical-incidents
    code also counts toward instability). Days with no records are
    absent from the axis and contribute 0 to any window sum, so
    prefix sums over the observed axis give exact window totals.
    """
    code_to_sets: dict[str, list[str]] = {}
    for name, codes in sets.items():
        for c in codes:
            code_to_sets.setdefault(c, []).append(name)
    vol_by_date: dict[str, float] = {}
    num_by_date: dict[str, dict[str, float]] = {n: {} for n in sets}
    for obs_date, code, total in conn.execute(
        "SELECT obs_date, cameo_code, SUM(obs_count) FROM dda "
        "WHERE iso1 = ? AND iso2 = ? GROUP BY obs_date, cameo_code",
        (iso1, iso2),
    ):
        vol_by_date[obs_date] = vol_by_date.get(obs_date, 0.0) + total
        for name in code_to_sets.get(code, ()):
            d = num_by_date[name]
            d[obs_date] = d.get(obs_date, 0.0) + total
    dates = sorted(vol_by_date)

    def _prefix(by_date: dict[str, float]) -> list[float]:
        out = [0.0]
        for d in dates:
            out.append(out[-1] + by_date.get(d, 0.0))
        return out

    return dates, _prefix(vol_by_date), {
        n: _prefix(num_by_date[n]) for n in sets}


def _realized_at(dates: list[str], vol_prefix: list[float],
                 num_prefix: list[float], as_of: str,
                 max_date: str | None) -> float | None:
    """Realized 45d outcome for one predictive row.

    The outcome statistic the D17 sweep validated against: the
    pooled trailing-90d share — Σ set obs / Σ total obs over
    [as_of + 45d − 89d, as_of + 45d] — i.e. the same S0m the
    predictive distribution forecasts, evaluated at the horizon
    date. None when the horizon has not elapsed by the sidecar's
    last date, or when the realized window carries no volume (the
    pooled ratio is then undefined, not zero).
    """
    if max_date is None:
        return None
    a = datetime.date.fromisoformat(as_of)
    end = a + datetime.timedelta(days=45)
    if end.isoformat() > max_date:
        return None
    start = (end - datetime.timedelta(days=89)).isoformat()
    lo = bisect.bisect_left(dates, start)
    hi = bisect.bisect_right(dates, end.isoformat())
    vol = vol_prefix[hi] - vol_prefix[lo]
    if vol <= 0:
        return None
    return (num_prefix[hi] - num_prefix[lo]) / vol


def _attach_support(rows: list[dict]) -> list[dict]:
    """Attach ``exc45_support`` for each row's pill driving set.

    Optionality mirrors the pill itself: rows without an
    ``exc45_max`` key (predictive not served) are returned
    untouched, and when the DDA pair is not configured the rows
    are likewise untouched — no ``exc45_support`` key at all, so
    a caller can tell "support not available" apart from
    "served, but no pill to annotate" (key present, value None:
    the sparse-dyad case, where ``exc45_max`` itself is None).
    Genuine-day lists are cached per (dyad, set) across the call,
    so a full history for one dyad costs one sidecar query per
    driving set, not one per row.
    """
    sets = _action_sets()
    if sets is None:
        return rows
    dp = dda_path()
    assert dp is not None  # _action_sets() has just verified it
    cache: dict[tuple[str, str, str], list[str]] = {}
    with _connect(dp) as conn:
        for row in rows:
            if "exc45_max" not in row:
                continue
            drive = row.get("exc45_max_set")
            if not drive or drive not in sets:
                row["exc45_support"] = None
                continue
            key = (row["iso1"], row["iso2"], drive)
            if key not in cache:
                cache[key] = _set_dates(
                    conn, row["iso1"], row["iso2"], sets[drive])
            row["exc45_support"] = {
                "set": drive,
                **_support_at(cache[key], row["as_of"]),
            }
    return rows


def fetch_predictive_sets(iso1: str, iso2: str) -> dict | None:
    """Per-set predictive history for one dyad, or None if unserved.

    Reads ``dyad_set_predictive`` — the predictive build's per-set
    rows (mu, phi_adopted, bar, exc45 per as-of) — grouped by set,
    as-of ascending. Since the D16 build extension the table
    carries all five sets, each stamped ``validated`` (1 for the
    swept escalatory/instability pair, 0 for the context-grade
    sets); pre-D16 files have no such column, and the tier is
    then derived from the set name (the validated pair is exactly
    escalatory + instability in every generation). Powers the
    Vet predictive figures and Dig's per-set panel; the interval
    quantiles are derived client-side from mu / phi_adopted.
    When the DDA pair is configured, each row additionally
    carries the support keys and ``realized_45d`` — the realized
    outcome statistic (the pooled S0m evaluated at as_of + 45d,
    the quantity the D17 sweep scored coverage against), None
    until that horizon has elapsed. D19 builds additionally
    carry a per-row ``grade`` ('validated' / 'indicative' —
    the row's domain grade, spec section 12.3); the key is
    present only when the served file has the column.

    Same optionality contract as ``_attach_predictive``: when
    PREDICTIVE_SQLITE is unset or the file is missing, returns
    None so the caller omits the payload key entirely ("not
    served"); when the build is served but the dyad has no
    per-set rows, returns {} ("served, empty for this dyad").
    """
    pp = predictive_path()
    if pp is None or not pp.exists():
        return None
    out: dict[str, list[dict]] = {}
    with _connect(pp) as conn:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(dyad_set_predictive)")}
        has_validated = "validated" in cols
        has_grade = "grade" in cols
        rows = conn.execute(
            'SELECT as_of, "set", mu, phi_adopted, bar, exc45'
            + (", validated" if has_validated else "")
            + (", grade" if has_grade else "")
            + " FROM dyad_set_predictive "
            "WHERE iso1 = ? AND iso2 = ? "
            'ORDER BY "set", as_of',
            (iso1, iso2),
        ).fetchall()
    for r in rows:
        if has_validated:
            validated = bool(r["validated"])
        else:
            validated = r["set"] in ("escalatory", "instability")
        row = {
            "as_of": r["as_of"],
            "mu": r["mu"],
            "phi_adopted": r["phi_adopted"],
            "bar": r["bar"],
            "exc45": r["exc45"],
            "validated": validated,
        }
        if has_grade:
            row["grade"] = r["grade"]
        out.setdefault(r["set"], []).append(row)
    # Per-row support (genuine_days_90d / last_seen_days_ago, as in
    # _attach_support), when the DDA pair is configured; the keys
    # are simply absent otherwise, so an unserved sidecar never
    # changes this payload's shape.
    sets = _action_sets()
    if sets is not None and out:
        dp = dda_path()
        assert dp is not None  # _action_sets() has just verified it
        with _connect(dp) as conn:
            for name, srows in out.items():
                if name not in sets:
                    continue
                dates = _set_dates(conn, iso1, iso2, sets[name])
                for srow in srows:
                    srow.update(_support_at(dates, srow["as_of"]))
            # Realized 45d outcomes from the same sidecar (see
            # _realized_at): merged per row as realized_45d, None
            # until the row's horizon has elapsed. One grouped
            # pass over the dyad's records serves every set.
            max_date = _dda_max_date(conn, dp)
            dates, vol_prefix, num_prefix = _daily_frame(
                conn, iso1, iso2, sets)
            for name, srows in out.items():
                if name not in sets:
                    continue
                for srow in srows:
                    srow["realized_45d"] = _realized_at(
                        dates, vol_prefix, num_prefix[name],
                        srow["as_of"], max_date)
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
    if row is None:
        return None
    return _attach_predictive([_row_to_dict(row)])[0]


def latest_as_of(path: Path) -> str | None:
    """Return the latest as_of in the rollup, or None when empty."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MAX(as_of) AS m FROM dyad_status_rollup"
        ).fetchone()
    return row["m"] if row is not None else None


def _watchlist_scan_keys(
    conn: sqlite3.Connection,
    effective: str,
    wanted: list[str],
    limit: int,
) -> list[tuple[str, str]]:
    """Step 1 of the two-step watchlist: collect (iso1, iso2) keys in
    final (label-priority, iso1, iso2) order via an iso-ordered scan.

    The scan reads ``ORDER BY iso1, iso2, as_of`` — exactly the
    ``idx_rollup_dyad`` order, so on any serving DB (which carries
    that index) it is a covering-index scan with no sort. Rows are
    bucketed by label in scan order, which IS iso order within each
    label, and buckets are concatenated in label-priority order, so
    the result matches the single-query ORDER BY exactly.

    Early termination: the scan stops as soon as the FIRST priority
    label's bucket reaches ``limit`` — later labels cannot displace
    it, and later rows of that label are all iso-after the ones
    already collected. This is what makes the unfiltered case fast:
    at 2026-09-30 the scan stops after ~8,100 index entries
    (0.10s measured) instead of materializing all 26,030 rows
    (36s measured for the single-query form).

    The scan is CORRECT with or without the index (without it,
    SQLite sorts); the index only makes it fast. Pathological
    case: if the first priority label has fewer than ``limit``
    rows at this as-of, the scan runs to completion — still exact,
    just no faster than the single-query form. At every real
    as-of the first group (insufficient) is in the thousands, so
    early termination fires.
    """
    buckets: dict[str, list[tuple[str, str]]] = {lab: [] for lab in wanted}
    first = wanted[0]
    cur = conn.execute(
        "SELECT rowid, iso1, iso2, as_of FROM dyad_status_rollup "
        "ORDER BY iso1, iso2, as_of"
    )
    for rowid, iso1, iso2, as_of in cur:
        if as_of != effective:
            continue
        lab_row = conn.execute(
            "SELECT label FROM dyad_status_rollup WHERE rowid = ?",
            (rowid,),
        ).fetchone()
        lab = lab_row[0] if lab_row is not None else None
        if lab not in buckets:
            continue
        buckets[lab].append((iso1, iso2))
        if len(buckets[first]) >= limit:
            break
    out: list[tuple[str, str]] = []
    for lab in wanted:
        out.extend(buckets[lab])
        if len(out) >= limit:
            break
    return out[:limit]


def _fetch_rows_by_keys(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
    effective: str,
) -> list[dict]:
    """Step 2 of the two-step watchlist: full rows for ``keys``, in
    key order, via point lookups on ``idx_rollup_dyad``."""
    rows = []
    for iso1, iso2 in keys:
        row = conn.execute(
            "SELECT * FROM dyad_status_rollup "
            "WHERE iso1 = ? AND iso2 = ? AND as_of = ?",
            (iso1, iso2, effective),
        ).fetchone()
        if row is not None:
            rows.append(_row_to_dict(row))
    return rows


def _fetch_watchlist_sorted(
    conn: sqlite3.Connection,
    effective: str,
    labels: list[str] | None,
    limit: int,
) -> list[dict]:
    """The original single-query watchlist (kept for the
    non-insufficient path, where it is already fast: 2.8s measured
    for the 627-row filtered view at 2026-09-30)."""
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
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


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

    Two paths, same payload and order (verified by equivalence
    test, not by reasoning alone):
    - When the requested set includes 'insufficient' (or no labels
      are given, i.e. all labels): the two-step scan path. The
      single-query form materializes the whole as-of slice
      (26,030 rows at 2026-09-30, 36s measured) before LIMIT;
      the scan path stops after the first priority group's first
      ``limit`` keys (0.10s measured unfiltered).
    - Otherwise (e.g. the default filtered view, 627 rows): the
      original single-query path, which is already fast there
      (2.8s measured) and avoids a full-index scan for sparse
      first groups (the scan would take ~14s to find 30 hot rows).
    """
    effective = as_of if as_of is not None else latest_as_of(path)
    if effective is None:
        return None, []
    wanted = [lab for lab in LABEL_PRIORITY if not labels or lab in labels]
    with _connect(path) as conn:
        if "insufficient" in wanted:
            keys = _watchlist_scan_keys(conn, effective, wanted, limit)
            return effective, _attach_predictive(
                _fetch_rows_by_keys(conn, keys, effective))
        return effective, _attach_predictive(_fetch_watchlist_sorted(
            conn, effective, labels, limit))


def fetch_history(path: Path, iso1: str, iso2: str) -> list[dict]:
    """Return all rollup rows for one dyad, oldest as-of first."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM dyad_status_rollup "
            "WHERE iso1 = ? AND iso2 = ? ORDER BY as_of ASC",
            (iso1, iso2),
        ).fetchall()
    return _attach_predictive([_row_to_dict(r) for r in rows])


def build_series(iso1: str, iso2: str, rows: list[dict]) -> dict:
    """Build the v1 PCS-series payload from rollup history rows.

    This is a *rollup-history* series, not the PCS panel series: it
    carries what the rollup stores per as-of (per-set R7/R28
    direction and std_resid, R7 share, temperature, label, the
    R7 support fields, the stored anomaly series, and the
    stored max exceedance (max_exc45) and the set attaining it
    (max_exc45_set), the validated two-set exceedance attached
    from the predictive build (exc45_max / exc45_max_set, when
    served) with its observation support (exc45_support, when
    the DDA pair is also served), and the four component label flags
    (flag_insufficient, flag_hot, flag_drifting, flag_atypical).
    The
    panel-only fields (baseline mu, phi, Beta tails) are not in
    the rollup and are deliberately not invented here. The
    predictive build's per-set rows (mu, phi_adopted, bar, exc45)
    are served alongside this payload as ``predictive_sets`` by
    the /pcs-series endpoint — see ``fetch_predictive_sets``.
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
                "max_exc45": row.get("max_exc45"),
                "max_exc45_set": row.get("max_exc45_set"),
                "exc45_max": row.get("exc45_max"),
                "exc45_max_set": row.get("exc45_max_set"),
                "exc45_calm_max": row.get("exc45_calm_max"),
                "exc45_support": row.get("exc45_support"),
                "flag_insufficient": row.get("flag_insufficient"),
                "flag_hot": row.get("flag_hot"),
                "flag_drifting": row.get("flag_drifting"),
                "flag_atypical": row.get("flag_atypical"),
                "anomaly_composite": row.get("anomaly_composite"),
                "anomaly_composite_raw": row.get("anomaly_composite_raw"),
                "anomaly_max3d": row.get("anomaly_max3d"),
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


def fetch_census(
    path: Path, as_of: str | None
) -> tuple[str | None, dict[str, int], int]:
    """Return (effective as_of, label counts, total) for one as-of.

    Counts are a read-time aggregate over stored rollup rows, not a
    recomputed statistic: the labels themselves are stored, this
    only counts them. Labels with no rows at the as-of count zero.
    With no as_of, the latest as-of in the DB is used. An as-of
    with no rows returns all-zero counts, not an error, mirroring
    the watchlist's empty-list-not-error behaviour.
    """
    effective = as_of if as_of is not None else latest_as_of(path)
    counts = {label: 0 for label in LABEL_PRIORITY}
    total = 0
    if effective is None:
        return None, counts, total
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT label, COUNT(*) FROM dyad_status_rollup "
            "WHERE as_of = ? GROUP BY label",
            (effective,),
        ).fetchall()
    for label, n in rows:
        total += int(n)
        if label in counts:
            counts[label] = int(n)
    return effective, counts, total


def fetch_census_cooling(
    path: Path, effective: str | None
) -> dict | None:
    """Return the census cooling block (LC-03), or None.

    Cooling is preprocessed in the rollup build: ``flag_cooling_005``
    / ``flag_cooling_010`` store the 28-day temperature fall at the
    two thresholds, gated on a sufficient label (seqbayes
    FOUNDATIONS_SPEC §2.4a, decided 2026-11-13). The block is
    served only when the rollup table carries both columns; a
    pre-cooling rollup returns None and the census payload omits
    the ``cooling`` key entirely — the same degradation pattern
    as the predictive grade column, so a caller can tell an old
    build from a slice with no cooling dyads.

    One slice read; the aggregation counts stored flags, exactly
    as ``fetch_census`` counts stored labels. ``labels`` covers
    every label (insufficient rows carry NULL cooling flags by
    construction, so their cooling counts come out 0).
    ``also_flagged_005`` counts, among sufficient dyads cooling
    at the 0.05 threshold, how many carry each stored flag — a
    dyad can carry several, so the counts overlap. ``cross`` is
    the hot x drifting x atypical flag cross over sufficient
    dyads only, all 8 rows always present in (hot, drifting,
    atypical) binary order; its ``n`` sums to the sufficient
    total at the as-of.
    """
    with _connect(path) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(dyad_status_rollup)")}
        if not {"flag_cooling_005", "flag_cooling_010"} <= cols:
            return None
        labels = {
            label: {"n": 0, "cooling_005": 0, "cooling_010": 0}
            for label in LABEL_PRIORITY
        }
        also = {"hot": 0, "drifting": 0, "atypical": 0}
        cross = {
            (h, d, a): {"hot": bool(h), "drifting": bool(d),
                        "atypical": bool(a), "n": 0, "cooling_005": 0}
            for h in (0, 1) for d in (0, 1) for a in (0, 1)
        }
        if effective is not None:
            rows = conn.execute(
                "SELECT label, flag_hot, flag_drifting, "
                "flag_atypical, flag_cooling_005, flag_cooling_010 "
                "FROM dyad_status_rollup WHERE as_of = ?",
                (effective,),
            ).fetchall()
            for label, f_hot, f_drift, f_atyp, c005, c010 in rows:
                if label not in labels:
                    continue
                entry = labels[label]
                entry["n"] += 1
                if c005 == 1:
                    entry["cooling_005"] += 1
                if c010 == 1:
                    entry["cooling_010"] += 1
                if label == "insufficient":
                    continue
                cell = cross[(1 if f_hot == 1 else 0,
                              1 if f_drift == 1 else 0,
                              1 if f_atyp == 1 else 0)]
                cell["n"] += 1
                if c005 == 1:
                    cell["cooling_005"] += 1
                    if f_hot == 1:
                        also["hot"] += 1
                    if f_drift == 1:
                        also["drifting"] += 1
                    if f_atyp == 1:
                        also["atypical"] += 1
    return {
        "labels": labels,
        "also_flagged_005": also,
        "cross": [cross[key] for key in sorted(cross)],
    }


def fetch_census_series(path: Path) -> list[dict]:
    """Return one row per as-of: label counts plus total, oldest first.

    This is the composition-over-time series the Scan composition
    chart reads. It is an aggregate over stored rows (see
    ``fetch_census``); the full-table GROUP BY uses the
    (as_of, label) index and runs in ~2s against the 7M-row
    September rollup on the dev VM.
    """
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT as_of, label, COUNT(*) FROM dyad_status_rollup "
            "GROUP BY as_of, label ORDER BY as_of"
        ).fetchall()
    by_as_of: dict[str, dict[str, int]] = {}
    for as_of, label, n in rows:
        entry = by_as_of.setdefault(
            as_of, {label: 0 for label in LABEL_PRIORITY}
        )
        if label in entry:
            entry[label] = int(n)
    out = []
    for as_of in sorted(by_as_of):
        counts = by_as_of[as_of]
        out.append(
            {"as_of": as_of, "total": sum(counts.values()), **counts}
        )
    return out
