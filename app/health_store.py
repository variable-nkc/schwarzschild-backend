"""Overall system health for the Context page (DEV ONLY — not
part of the v1 contract).

Spec (Dan, 2026-11-03, as reviewed and agreed the same day):
three subsections — Roll-up, DDA, poc_base — because the
stores can drift out of sync, plus a cross-store block
(Muse addition 1, approved 2026-11-03). Health is evaluated
AT the page as-of (Q1): each store's evaluation day is its
latest date <= the requested as-of, and freshness is the age
of that day against the as-of. In live serving with as-of =
today that reduces to the wall-clock reading; in the
September snapshot it does not cry wolf (all three stores
end 2026-09-30, which would read 34 days stale against
today).

Metrics, per store, unless noted:
- (1) freshness: evaluation day + icon (green <= 1 day old,
  yellow 2 days, red 3+ days). Boundary pinned: Dan's
  "within previous 2 days / 2 or more days" overlapped at
  exactly 2.
- (2) dyads present + percent of baseline, capped at 100%
  (Dan's cap rule), icons green >= 90%, yellow >= 75%,
  red below. Rollup "present" is v_7d > 0 (Q2): the panel
  carries a row for every dyad on every as-of, so row
  existence is always 26,030 and meaningless; v_7d > 0
  equals the days_present_7 > 0 count exactly (6,771 at
  2026-09-30) but runs in 0.04s versus 41.87s, because
  days_present_7 sits in no covering index. Temperature
  NOT NULL is ruled out as a proxy: it holds for 24,832
  dyads, nearly the whole panel. Rollup (2) is inherently
  trailing-7-day (rollup has no per-day field — the M1
  lesson); DDA/poc_base (2) are single-day. Captioned,
  never conflated.
- (3) non-insufficient dyads + percent (same icons).
  DDA and poc_base carry no labels, so their (3) is the
  join: dyads present that day whose rollup label that day
  is non-insufficient (Q3). The loose alternative — DDA
  present (2,411) against rollup's sufficient baseline
  (627) — gives 384%, capped at 100%, green forever, so
  it is not implemented. The join's baseline is the
  28-day average of that same joined count.
- (4) rollup only: label balance. Shares over the
  non-insufficient total; deviation is the relative %
  of the share against its 28-day average share;
  bands green within +/-25%, yellow within +/-40%,
  red beyond. Atypical is banded only when its 28-day
  average COUNT is >= 10 (Q5): daily atypical counts run
  1-8 (average 4.4), so the bands are noise on it — at
  2026-09-30 it reads -74.6% red on an otherwise green
  day. Below the floor its count is shown with no icon
  and it is excluded from the overall, which is the worst
  of the banded labels. Insufficient is a raw count only,
  never banded.
- (5) DDA only: dyad-date-action records (row count, not
  obs_count sum) + percent. (6) poc_base only: event
  records (COUNT(*), root + related — events_core is
  ~49% non-root) + percent.

Baselines (Q6): the baseline window is the 28 calendar
days ending the day before the evaluation day, restricted
to days at or after the store's first date (pre-panel
days are excluded, never zero-filled); a baseline needs
at least 14 of 28 days, else it is null (count shown,
no percent, no icon). Single-day metrics (DDA (2)/(5),
poc_base (2)/(6)) use same-weekday baselines instead —
the average of the 4 prior same-weekdays — because
weekend single-day counts run 60-70% of the 28-day
average (measured 2026-11-03: weekend events 11.5k-16k
vs a 19.1k average; weekend dyads ~1,700 vs weekday
~2,400-2,700), so a plain average would paint every
weekend red. Same-weekday baselines need at least 2 of
4 days. Ratios and shares (cross-store ratios, label
shares) are weekend-stable, so they use the plain
28-day average. Windowed rollup counts use the plain
28-day average (a trailing 7 days always contains a
weekend).

Cross-store block (addition 1): volume ratio (DDA
obs_count sum / poc_base event count on the common day
— the latest day both stores cover) and dyad Jaccard
(|intersection| / |union| of the day's DDA dyads and
poc_base pairs), each against its same-weekday baseline,
judged by the tight two-sided band below. Measured
exactly 1.0 / 1.0 at 2026-09-30. ADOPTED (Dan,
2026-11-04): the cross ratios use a two-sided deviation
band, tighter than the label bands — green within ±10%
of baseline, yellow within ±25%, red beyond — so a
ratio ABOVE its baseline (e.g. a DDA double-count
pushing the volume ratio to 2.0) flags instead of
reading 100% green. Evidence (measured 2026-11-04, VM
stores, 92 days 2026-07-01..09-30): both ratios are
exactly 1.0 every day, so the tighter band essentially
never false-alarms on current data.

Trend strips (addition 3): every count metric carries
its series (baseline days + evaluation day, zeros for
days with no rows), so the frontend can draw one strip
per subsection from data already fetched for baselines.

Provenance header (addition 2): per-store date ranges
plus the rollup vintage/build_id when the table carries
exactly one of each. The known-holes note (addition 4)
was deferred by Dan to a future FAQ-like section and is
NOT built here. The over-count alarm was considered
and rejected (genuine news spikes would trip it).

Performance (measured 2026-11-03 on the VM serving
copy + sidecar + mirror): the endpoint totals ~3.7s
warm. Two shapes were measured slow and are NOT used:
row iteration over the 29-as-of window in Python
(~754k rows fetched) and DISTINCT vintage/build_id
over the whole table (a 7M-row full scan, ~15s — it
was the endpoint's dominant cost before the fix).
All rollup aggregates are GROUP BY in SQL (covering
indexes, ~200 rows out); the slowest remaining query
is the DDA 29-day count+sum GROUP BY at 1.31s. No
hardcoded baseline is needed at current scale;
Dan's hardcoded allowance stands as a fallback only,
with a "baseline as of" caption and refresh rule if it
is ever used, since a stale baseline masks real drops.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

BASELINE_DAYS = 28
MIN_BASELINE_DAYS = 14
MIN_SAME_WEEKDAY_DAYS = 2
GREEN_MIN_PCT = 90.0
YELLOW_MIN_PCT = 75.0
BAND_GREEN_PCT = 25.0
BAND_YELLOW_PCT = 40.0
# Cross-store ratios use a tighter two-sided band (Dan, 2026-11-04):
# the ratios are exactly 1.0 by construction, so any deviation is a
# pipeline break and the band should alarm sooner than the label bands.
CROSS_BAND_GREEN_PCT = 10.0
CROSS_BAND_YELLOW_PCT = 25.0
ATYPICAL_FLOOR_COUNT = 10.0
LABELS_4 = ("hot", "drifting", "atypical", "steady")


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _parse_requested(as_of: str | None) -> date | None:
    if as_of is None:
        return None
    return date.fromisoformat(as_of)  # raises ValueError on garbage


def _latest(con: sqlite3.Connection, table: str, col: str,
            requested: date | None) -> date | None:
    if requested is None:
        row = con.execute(
            f"SELECT MAX({col}) AS m FROM {table}").fetchone()
    else:
        row = con.execute(
            f"SELECT MAX({col}) AS m FROM {table}"
            f" WHERE {col} <= ?", (requested.isoformat(),)).fetchone()
    raw = row["m"] if row is not None else None
    return date.fromisoformat(raw) if raw else None


def _earliest(con: sqlite3.Connection, table: str, col: str) -> date | None:
    row = con.execute(f"SELECT MIN({col}) AS m FROM {table}").fetchone()
    raw = row["m"] if row is not None else None
    return date.fromisoformat(raw) if raw else None


def _pct(count: float, baseline: float | None) -> float | None:
    if baseline is None or baseline <= 0:
        return None
    return min(100.0, 100.0 * count / baseline)


def _icon_pct(pct: float | None) -> str | None:
    if pct is None:
        return None
    if pct >= GREEN_MIN_PCT:
        return "green"
    if pct >= YELLOW_MIN_PCT:
        return "yellow"
    return "red"


def _band_icon(deviation: float | None,
               green: float = BAND_GREEN_PCT,
               yellow: float = BAND_YELLOW_PCT) -> str | None:
    if deviation is None:
        return None
    if abs(deviation) <= green:
        return "green"
    if abs(deviation) <= yellow:
        return "yellow"
    return "red"


def _deviation(ratio: float | None, baseline: float | None) -> float | None:
    """Signed percent deviation of a ratio from its baseline."""
    if ratio is None or baseline is None or baseline <= 0:
        return None
    return 100.0 * (ratio / baseline - 1.0)


def _worst_icon(icons: list) -> str | None:
    order = {"green": 0, "yellow": 1, "red": 2}
    present = [i for i in icons if i in order]
    if not present:
        return None
    return max(present, key=lambda i: order[i])


def _baseline_days(eval_day: date, first_day: date | None,
                   same_weekday: bool) -> list[date]:
    """Baseline calendar days for one evaluation day.

    Plain: the 28 days ending the day before eval.
    Same-weekday: eval-7, -14, -21, -28. Days before the
    store's first date are excluded (never zero-filled).
    """
    if same_weekday:
        days = [eval_day - timedelta(days=7 * k) for k in (1, 2, 3, 4)]
    else:
        days = [eval_day - timedelta(days=k)
                for k in range(BASELINE_DAYS, 0, -1)]
    if first_day is not None:
        days = [d for d in days if d >= first_day]
    return sorted(days)


def _avg(values: list[float], minimum: int) -> float | None:
    if len(values) < minimum:
        return None
    return sum(values) / len(values)


def _metric(count: int | None, baseline: float | None,
            series: list) -> dict:
    pct = _pct(count, baseline) if count is not None else None
    return {"count": count, "baseline": baseline, "pct": pct,
            "icon": _icon_pct(pct), "series": series}


def _series(daily: dict, days: list[date], eval_day: date) -> list:
    """[[iso-date, value], ...] for baseline days + eval day.

    A day absent from `daily` is a real zero (the store
    emitted nothing that day), not a gap in the series.
    """
    out = [[d.isoformat(), daily.get(d, 0)] for d in days]
    out.append([eval_day.isoformat(), daily.get(eval_day, 0)])
    return out


def _freshness(eval_day: date | None, ref_day: date) -> dict:
    if eval_day is None:
        return {"eval_date": None, "age_days": None, "icon": None}
    age = (ref_day - eval_day).days
    icon = "green" if age <= 1 else ("yellow" if age == 2 else "red")
    return {"eval_date": eval_day.isoformat(), "age_days": age,
            "icon": icon}


def fetch_health(rollup_conn: sqlite3.Connection,
                 dda_conn: sqlite3.Connection,
                 mirror_conn: sqlite3.Connection,
                 as_of: str | None) -> dict:
    requested = _parse_requested(as_of)

    rollup_eval = _latest(rollup_conn, "dyad_status_rollup", "as_of",
                          requested)
    dda_eval = _latest(dda_conn, "dda", "obs_date", requested)
    pb_eval = _latest(mirror_conn, "gdelt_events_core", "event_date",
                      requested)
    evals = [e for e in (rollup_eval, dda_eval, pb_eval) if e is not None]
    if not evals:
        return {"status": "no_data", "requested_as_of": as_of,
                "as_of": None, "rollup": None, "dda": None,
                "poc_base": None, "cross_store": None,
                "provenance": None}

    # Union window: every store's own window sits inside it,
    # and so do the cross-store common days.
    win_start = min(evals) - timedelta(days=BASELINE_DAYS)
    win_end = max(evals)
    win_iso = (win_start.isoformat(), win_end.isoformat())

    rollup_first = _earliest(rollup_conn, "dyad_status_rollup", "as_of")
    dda_first = _earliest(dda_conn, "dda", "obs_date")
    pb_first = _earliest(mirror_conn, "gdelt_events_core", "event_date")

    # --- rollup per-as-of aggregates (covering indexes) ---
    # GROUP BY in SQL, not row iteration in Python: the
    # window holds ~754k rows, and fetching them all cost
    # seconds. The grouped forms return ~200 rows.
    present_by: dict = {}
    label_by: dict = {}
    for as_of_d, cnt in rollup_conn.execute(
            "SELECT as_of, COUNT(*) FROM dyad_status_rollup"
            " WHERE as_of >= ? AND as_of <= ? AND v_7d > 0"
            " GROUP BY as_of", win_iso):
        present_by[date.fromisoformat(as_of_d)] = cnt
    for as_of_d, label, cnt in rollup_conn.execute(
            "SELECT as_of, label, COUNT(*) FROM dyad_status_rollup"
            " WHERE as_of >= ? AND as_of <= ?"
            " GROUP BY as_of, label", win_iso):
        label_by.setdefault(
            date.fromisoformat(as_of_d), {})[label] = cnt
    insuff_by = {d: counts.get("insufficient", 0)
                 for d, counts in label_by.items()}
    noninsuff_by = {d: sum(v for k, v in counts.items()
                           if k != "insufficient")
                    for d, counts in label_by.items()}
    # Non-insufficient (iso1, iso2) pairs per as-of, for the
    # DDA/poc_base (3) joins (covering index A: as_of, label,
    # temperature, iso1, iso2).
    rollup_pairs: dict = {}
    for as_of_d, iso1, iso2 in rollup_conn.execute(
            "SELECT as_of, iso1, iso2 FROM dyad_status_rollup"
            " WHERE as_of >= ? AND as_of <= ?"
            " AND label != 'insufficient'", win_iso):
        rollup_pairs.setdefault(
            date.fromisoformat(as_of_d), set()).add((iso1, iso2))

    # --- DDA per-day aggregates + pair sets ---
    dda_present: dict = {}
    dda_records: dict = {}
    dda_sums: dict = {}
    dda_pairs: dict = {}
    for obs_d, cnt, s in dda_conn.execute(
            "SELECT obs_date, COUNT(*), SUM(obs_count) FROM dda"
            " WHERE obs_date >= ? AND obs_date <= ?"
            " GROUP BY obs_date", win_iso):
        d = date.fromisoformat(obs_d)
        dda_records[d] = cnt
        dda_sums[d] = s if s is not None else 0
    for obs_d, iso1, iso2 in dda_conn.execute(
            "SELECT obs_date, iso1, iso2 FROM dda"
            " WHERE obs_date >= ? AND obs_date <= ?"
            " GROUP BY obs_date, iso1, iso2", win_iso):
        dda_pairs.setdefault(date.fromisoformat(obs_d), set()).add(
            (iso1, iso2))
    for d, pairs in dda_pairs.items():
        dda_present[d] = len(pairs)

    # --- poc_base per-day aggregates + pair sets ---
    pb_present: dict = {}
    pb_events: dict = {}
    pb_pairs: dict = {}
    for ev_d, cnt in mirror_conn.execute(
            "SELECT event_date, COUNT(*) FROM gdelt_events_core"
            " WHERE event_date >= ? AND event_date <= ?"
            " GROUP BY event_date", win_iso):
        pb_events[date.fromisoformat(ev_d)] = cnt
    for ev_d, a1, a2 in mirror_conn.execute(
            "SELECT event_date, actor1_country_code,"
            " actor2_country_code FROM gdelt_events_core"
            " WHERE event_date >= ? AND event_date <= ?"
            " AND actor1_country_code IS NOT NULL"
            " AND actor2_country_code IS NOT NULL"
            " AND actor1_country_code != actor2_country_code"
            " GROUP BY event_date, actor1_country_code,"
            " actor2_country_code", win_iso):
        pb_pairs.setdefault(date.fromisoformat(ev_d), set()).add((a1, a2))
    for d, pairs in pb_pairs.items():
        pb_present[d] = len(pairs)

    def _store_block(eval_day: date | None, first_day: date | None,
                     ref_day: date, present: dict, noninsuff: dict,
                     present_same_weekday: bool,
                     noninsuff_same_weekday: bool) -> dict | None:
        if eval_day is None:
            return {"status": "no_data", "freshness": _freshness(
                None, ref_day)}
        base_days = _baseline_days(eval_day, first_day, False)
        sw_days = _baseline_days(eval_day, first_day, True)
        present_base = _avg(
            [present.get(d, 0) for d in base_days], MIN_BASELINE_DAYS)
        present_sw = _avg(
            [present.get(d, 0) for d in sw_days], MIN_SAME_WEEKDAY_DAYS)
        # (3) follows the same baseline rule as (2) for its
        # store: rollup (3) is a trailing-window count (plain
        # average); DDA/poc_base (3) is a single-day joined
        # count (same-weekday average, like (2)).
        noninsuff_days = sw_days if noninsuff_same_weekday else base_days
        noninsuff_min = (MIN_SAME_WEEKDAY_DAYS
                         if noninsuff_same_weekday else MIN_BASELINE_DAYS)
        noninsuff_base = _avg(
            [noninsuff.get(d, 0) for d in noninsuff_days], noninsuff_min)
        block = {
            "status": "ok",
            "freshness": _freshness(eval_day, ref_day),
            "baseline_start": (base_days[0].isoformat()
                               if base_days else None),
            "baseline_end": (base_days[-1].isoformat()
                             if base_days else None),
            "dyads_present": _metric(
                present.get(eval_day, 0),
                present_sw if present_same_weekday else present_base,
                _series(present, base_days, eval_day)),
            "non_insufficient": _metric(
                noninsuff.get(eval_day, 0), noninsuff_base,
                _series(noninsuff, base_days, eval_day)),
        }
        return block

    def _joined_series(pairs_by_day: dict) -> dict:
        """Joined (3) counts per day: present dyads/pairs whose
        rollup label that day is non-insufficient."""
        out = {}
        for d, pairs in pairs_by_day.items():
            out[d] = len(pairs & rollup_pairs.get(d, set()))
        return out

    # Rollup block (windowed metrics: plain 28-day baselines).
    rollup_block = None
    if rollup_eval is not None:
        ref = requested if requested is not None else rollup_eval
        rollup_block = _store_block(
            rollup_eval, rollup_first, ref, present_by, noninsuff_by,
            False, False)
        # (4) label balance.
        base_days = _baseline_days(rollup_eval, rollup_first, False)
        labels = {}
        band_icons = []
        total_eval = noninsuff_by.get(rollup_eval, 0)
        for lab in LABELS_4:
            count_eval = label_by.get(rollup_eval, {}).get(lab, 0)
            share_eval = (count_eval / total_eval
                          if total_eval else None)
            avg_counts = [label_by.get(d, {}).get(lab, 0)
                          for d in base_days]
            avg_count = _avg(avg_counts, MIN_BASELINE_DAYS)
            totals = [noninsuff_by.get(d, 0) for d in base_days]
            shares = [label_by.get(d, {}).get(lab, 0) / t
                      for d, t in zip(base_days, totals) if t]
            avg_share = _avg(shares, MIN_BASELINE_DAYS)
            deviation = ((share_eval - avg_share) / avg_share * 100.0
                         if share_eval is not None
                         and avg_share else None)
            if lab == "atypical" and (avg_count is None
                                     or avg_count < ATYPICAL_FLOOR_COUNT):
                icon = None  # small-number exemption (Q5)
            else:
                icon = _band_icon(deviation)
            if icon is not None:
                band_icons.append(icon)
            labels[lab] = {"count": count_eval, "share": share_eval,
                           "avg_share": avg_share,
                           "avg_count": avg_count,
                           "deviation_pct": deviation, "icon": icon}
        rollup_block["labels"] = labels
        rollup_block["labels_overall_icon"] = _worst_icon(band_icons)
        rollup_block["insufficient_count"] = insuff_by.get(
            rollup_eval, 0)

    # DDA block (single-day metrics: same-weekday baselines).
    dda_block = None
    if dda_eval is not None:
        ref = requested if requested is not None else dda_eval
        joined = _joined_series(dda_pairs)
        dda_block = _store_block(
            dda_eval, dda_first, ref, dda_present, joined, True, True)
        base_days = _baseline_days(dda_eval, dda_first, False)
        sw_days = _baseline_days(dda_eval, dda_first, True)
        rec_base = _avg([dda_records.get(d, 0) for d in sw_days],
                        MIN_SAME_WEEKDAY_DAYS)
        dda_block["records"] = _metric(
            dda_records.get(dda_eval, 0), rec_base,
            _series(dda_records, base_days, dda_eval))

    # poc_base block (single-day metrics: same-weekday baselines).
    pb_block = None
    if pb_eval is not None:
        ref = requested if requested is not None else pb_eval
        joined = _joined_series(pb_pairs)
        pb_block = _store_block(
            pb_eval, pb_first, ref, pb_present, joined, True, True)
        base_days = _baseline_days(pb_eval, pb_first, False)
        sw_days = _baseline_days(pb_eval, pb_first, True)
        ev_base = _avg([pb_events.get(d, 0) for d in sw_days],
                       MIN_SAME_WEEKDAY_DAYS)
        pb_block["events"] = _metric(
            pb_events.get(pb_eval, 0), ev_base,
            _series(pb_events, base_days, pb_eval))

    # Cross-store block on the common day (latest day both
    # DDA and poc_base cover). Ratios use same-weekday
    # baselines, per the approved proposal.
    cross = None
    if dda_eval is not None and pb_eval is not None:
        common = min(dda_eval, pb_eval)
        first_common = max(x for x in (dda_first, pb_first)
                           if x is not None) \
            if dda_first is not None and pb_first is not None else None
        sw_days = _baseline_days(common, first_common, True)
        vol_ratios = {}
        jac_ratios = {}
        for d in sw_days + [common]:
            pb_c = pb_events.get(d, 0)
            if pb_c:
                vol_ratios[d] = dda_sums.get(d, 0) / pb_c
            d_pairs = dda_pairs.get(d, set())
            p_pairs = pb_pairs.get(d, set())
            union = d_pairs | p_pairs
            if union:
                jac_ratios[d] = len(d_pairs & p_pairs) / len(union)
        vol_base = _avg([vol_ratios[d] for d in sw_days
                         if d in vol_ratios], MIN_SAME_WEEKDAY_DAYS)
        jac_base = _avg([jac_ratios[d] for d in sw_days
                         if d in jac_ratios], MIN_SAME_WEEKDAY_DAYS)
        vol_now = vol_ratios.get(common)
        jac_now = jac_ratios.get(common)
        cross = {
            "status": "ok",
            "common_date": common.isoformat(),
            "dda_events_sum": dda_sums.get(common, 0),
            "poc_base_events": pb_events.get(common, 0),
            "dda_dyads": len(dda_pairs.get(common, set())),
            "poc_base_pairs": len(pb_pairs.get(common, set())),
            "intersection": len(dda_pairs.get(common, set())
                                & pb_pairs.get(common, set())),
            "union": len(dda_pairs.get(common, set())
                         | pb_pairs.get(common, set())),
            "volume_ratio": {"ratio": vol_now, "baseline": vol_base,
                             "deviation_pct": _deviation(vol_now,
                                                         vol_base),
                             "icon": _band_icon(
                                 _deviation(vol_now, vol_base),
                                 green=CROSS_BAND_GREEN_PCT,
                                 yellow=CROSS_BAND_YELLOW_PCT),
                             "series": [[d.isoformat(), vol_ratios[d]]
                                        for d in sorted(vol_ratios)]},
            "dyad_jaccard": {"ratio": jac_now, "baseline": jac_base,
                             "deviation_pct": _deviation(jac_now,
                                                         jac_base),
                             "icon": _band_icon(
                                 _deviation(jac_now, jac_base),
                                 green=CROSS_BAND_GREEN_PCT,
                                 yellow=CROSS_BAND_YELLOW_PCT),
                             "series": [[d.isoformat(), jac_ratios[d]]
                                        for d in sorted(jac_ratios)]},
        }

    # Provenance header (addition 2).
    prov = {
        "rollup_as_of_range": [
            rollup_first.isoformat() if rollup_first else None,
            rollup_eval.isoformat() if rollup_eval else None,
        ],
        "dda_obs_range": [
            dda_first.isoformat() if dda_first else None,
            dda_eval.isoformat() if dda_eval else None,
        ],
        "events_range": [
            pb_first.isoformat() if pb_first else None,
            pb_eval.isoformat() if pb_eval else None,
        ],
        "rollup_vintage": None,
        "rollup_build_id": None,
    }
    # Vintage/build from ONE row at the evaluation as-of,
    # never DISTINCT over the table: the table carries no
    # index on those columns, so DISTINCT is a 7M-row full
    # scan (~15s on this VM's storage — measured 2026-11-03
    # as the endpoint's dominant cost before this fix).
    # The serving copy is single-build, so one row's values
    # are the panel's; a mixed-build table would need a
    # different rule, not a full scan.
    if rollup_eval is not None:
        try:
            row = rollup_conn.execute(
                "SELECT vintage, build_id FROM dyad_status_rollup"
                " WHERE as_of = ? LIMIT 1",
                (rollup_eval.isoformat(),)).fetchone()
            if row is not None:
                prov["rollup_vintage"] = row[0]
                prov["rollup_build_id"] = row[1]
        except sqlite3.Error:
            pass

    return {"status": "ok", "requested_as_of": as_of,
            "as_of": (rollup_eval.isoformat()
                      if rollup_eval is not None else None),
            "rollup": rollup_block, "dda": dda_block,
            "poc_base": pb_block, "cross_store": cross,
            "provenance": prov}
