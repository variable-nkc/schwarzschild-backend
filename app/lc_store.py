"""DEV ONLY Look-Closer data access (/lc/ endpoints, Wave 1).

Not part of the v1 contract. Plain dicts in, plain dicts out —
same split as ``app/store.py`` (pydantic lives at the boundary
in ``app/main.py``). Uses the SAME environment names as /dev/
(ROLLUP_SQLITE, DDA_SQLITE, DDA_SIMPLEXES_JSON); no new env names.

Endpoints served from here (all GET, iso1/iso2 required, as_of
optional — windows END AT as-of, never at the snapshot end):

- LC-01: rollup history, ~28 as-ofs ending at as-of, one row per
  as-of with the stored triage label + 4 component flags. No
  recent-typical mark is computed or returned.
- LC-02: rollup only. Three rows (evidence, stability, data),
  NEVER averaged into a composite. The data row is always dots
  null with a fixed reason (see DATA_REASON).
- LC-05: rollup only, as-of only. At most 3 chips, ranked by
  |R7 std_resid| descending.
- LC-07: DDA sidecar daily volumes ONLY (not rollup pooled
  totals, not the PCS panel). One fetch per request.

PROVISIONAL dev-only scoring rules (NOT formalized methodology —
never present these as such; they exist so the dev endpoints have
something transparent to serve while formalization is pending):

LC-02 EVIDENCE RULE (provisional dev-only): dots (0-5) =
  +2 if days_present_7 >= 4;
  +1 more if days_present_7 >= 6;
  +1 if v_7d >= 30;
  +1 if no set is listed in insufficient_sets (all 5 analyst sets
  have sufficient support).
  A missing (null) v_7d or days_present_7 scores 0 on that
  component. The 4-day floor borrows the PCS R7 presence floor
  and the 30-volume floor borrows the PCS V_R >= 30 floor as
  convenient dev anchors; neither borrowing is a formalization
  decision.

LC-02 STABILITY RULE (provisional dev-only): dots (0-5) = one dot
  per analyst set (of 5) whose R7 direction and R28 direction are
  both present and equal. Sets missing either direction are not
  counted and do not score.

LC-05 DISTANCE RULE (provisional dev-only): from |std_r7|:
  |std| >= 3 -> "well above usual" (std > 0) / "well below usual"
  (std < 0); 1 <= |std| < 3 -> "above usual" / "below usual";
  |std| < 1 -> "near usual"; null std -> null distance.
  LC-05 DIRECTION RULE: chip direction is "mixed over the month"
  when r28_dir is present and differs from r7_dir; otherwise it is
  the stored r7_dir verbatim ("up", "down", or "steady"). The
  rollup stores three direction values, so "steady" is passed
  through when R7 and R28 agree on it; it is not re-labeled up
  or down. Only sets with a non-null r7_dir are chip candidates.

LC-07 VOLUME DEFINITION (traced from gallery figure [2.05],
``volume_regime`` in the analytics worktree
src/seqbayes/gallery_data.py, replicated exactly):
  - Daily volume is MAPPED volume only: obs_count summed over
    codes in the mapped list (DDA_SIMPLEXES_JSON code list, the
    same 318-code soc_long list /dev/ uses), NOT total volume
    over all codes. Days with no mapped records count as 0
    (0-filled over the calendar, as the gallery's build_daily
    does) — a gap is a zero-volume day here, because [2.05]
    0-fills; this differs deliberately from /dev/dda-daily,
    where a gap day carries null shares.
  - Pooled 7d volume at day d = sum of daily mapped volume over
    [d-6, d] (trailing 7 days ending at d, inclusive).
  - Baseline expectation at day d = mean of the 84 rolling 7-day
    sums over the 90 days immediately preceding the pooled
    window, i.e. over [d-97, d-8]. Baseline tail at day d =
    share of those 84 windows at or above the pooled volume
    (near 0 = exceptionally loud, near 1 = exceptionally
    quiet). A ratio needs a full 97-day lead-in; without it
    the ratio (and its median) is null, never back-filled.
  - VALIDITY GATE: a day's ratio is valid only if the dyad has
    at least one mapped record dated on or before that day's
    baseline start (day-97) — i.e. the dyad was already active
    when the baseline began. Without that, a dyad first seen
    days ago would get a near-zero baseline and a nonsense
    ratio; instead its ratio is null. This is what makes the
    7-valid-day floor meaningful (a short-history dyad has
    zero valid days). The gate needs one aggregate MIN query
    in addition to the single daily-series fetch — it is not
    a per-day fetch. Gap days inside an active history still
    count as 0 (0-fill, as above).
  - DAILY-RATIO SERIES (for the 14-day recent-typical median):
    the figure's own daily volume ratio evaluated at each of
    the previous 14 days (today excluded), where each day's
    ratio is that day's pooled-7d volume over that day's own
    trailing-90d baseline expectation, exactly as defined
    above — NOT a daily-volume ratio. The median is over
    valid (non-null) ratios only, with a 7-valid-day floor:
    fewer than 7 valid days -> median null, never back-filled.
    Rarity (the tail) is stated for today only; no rarity
    statement is made for the typical level.
  - CAUTION RULE (dev-only wording threshold, not methodology):
    caution is true exactly when today's volume is BOTH rare
    (baseline tail <= 0.05 or >= 0.95) AND large in level
    (volume_ratio >= 1.5 or <= 2/3); otherwise false with null
    text. Rationale: only exceptional volume plausibly
    explains part of a move on its own, and the level clause
    keeps degenerate ties (e.g. a flat series, tail 1.0 at
    ratio exactly 1.0) from firing it.

As-of resolution (rollup endpoints): the effective as-of is the
dyad's latest stored as-of <= the requested as-of (or the dyad's
latest as-of when none is requested). A requested as-of with no
rollup row at or before it yields an empty response, not an
error — mirroring the /dev/ unknown-dyad discipline (200 with
empty rows/nulls). LC-07 instead follows the /dev/ DDA pattern:
a requested as-of is used as the window end verbatim, and an
omitted as-of resolves to the dyad's latest DDA obs_date.
An unknown dyad with no as-of yields as_of null and null
values; with an as-of given, the requested as-of is echoed
with null values (same echo discipline as /dev/dda-daily).
"""

import sqlite3
import statistics
from datetime import date, timedelta
from pathlib import Path

from .config import SETS

#: Exact provenance message on every /lc/ envelope.
LC_PROVENANCE_MESSAGE = "LC DEV ONLY — not part of the v1 contract."

#: LC-02 data row: dots null, reason verbatim (per the brief).
DATA_REASON = (
    "mirror record/source line of effort, "
    "not served by rollup or /lc/ endpoints"
)

#: LC-07 caution text, shown only when caution applies.
CAUTION_TEXT = (
    "volume alone could explain part of today's move — "
    "check the mix before the level."
)

#: Component flags, in stored order.
FLAGS = (
    "flag_insufficient",
    "flag_hot",
    "flag_drifting",
    "flag_atypical",
)

#: Days of lead-in a [2.05] ratio needs: 90 baseline + 7 pooled.
LC07_LEAD_IN_DAYS = 97
#: Days before today whose ratios feed the recent-typical median.
LC07_TYPICAL_DAYS = 14
#: Valid-day floor for the recent-typical median.
LC07_TYPICAL_FLOOR = 7
#: Tails at or beyond these bounds count as exceptional (caution).
LC07_TAIL_LO = 0.05
LC07_TAIL_HI = 0.95
#: Level bounds for the caution rule (see module docstring).
LC07_RATIO_HI = 1.5
LC07_RATIO_LO = 2.0 / 3.0

# LC-02 provisional evidence thresholds (see module docstring).
EVIDENCE_DAYS_FLOOR = 4
EVIDENCE_DAYS_HIGH = 6
EVIDENCE_VOLUME_FLOOR = 30

# LC-05 provisional distance thresholds (see module docstring).
DISTANCE_WELL = 3.0
DISTANCE_ABOVE = 1.0


def connect(path: Path) -> sqlite3.Connection:
    """Fresh read-only connection (same discipline as store.py)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_insufficient(raw: object) -> list[str]:
    """insufficient_sets is comma-separated set names ('' = none)."""
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _resolve_rollup_as_of(
    conn: sqlite3.Connection, iso1: str, iso2: str, as_of: str | None
) -> str | None:
    """Dyad's latest as-of <= requested (or latest overall)."""
    if as_of is None:
        row = conn.execute(
            "SELECT MAX(as_of) AS m FROM dyad_status_rollup "
            "WHERE iso1 = ? AND iso2 = ?",
            (iso1, iso2),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(as_of) AS m FROM dyad_status_rollup "
            "WHERE iso1 = ? AND iso2 = ? AND as_of <= ?",
            (iso1, iso2, as_of),
        ).fetchone()
    return row["m"] if row and row["m"] is not None else None


def _rollup_row(
    conn: sqlite3.Connection, iso1: str, iso2: str, as_of: str
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM dyad_status_rollup "
        "WHERE iso1 = ? AND iso2 = ? AND as_of = ?",
        (iso1, iso2, as_of),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    for col in FLAGS:
        if col in out and out[col] is not None:
            out[col] = bool(out[col])
    return out


# ------------------------------------------------------------------
# LC-01
# ------------------------------------------------------------------

def fetch_lc01(
    conn: sqlite3.Connection,
    iso1: str,
    iso2: str,
    as_of: str | None,
    window: int = 28,
) -> dict:
    """~28 as-ofs ending at as-of, label + 4 flags per as-of.

    Days are oldest first. Delta compares the effective as-of
    against the previous as-of in the dyad's history (label
    change + flag changes only); when nothing changed the delta
    text is exactly "no change since yesterday". With fewer
    than two days there is no previous as-of and delta text is
    null. No recent-typical mark is computed or returned.
    """
    effective = _resolve_rollup_as_of(conn, iso1, iso2, as_of)
    if effective is None:
        return {
            "dyad": f"{iso1}_{iso2}",
            "as_of": None,
            "today_label": None,
            "days": [],
            "delta": {
                "previous_as_of": None,
                "label_change": None,
                "flag_changes": [],
                "text": None,
            },
        }
    rows = conn.execute(
        "SELECT as_of, label, flag_insufficient, flag_hot, "
        "flag_drifting, flag_atypical FROM dyad_status_rollup "
        "WHERE iso1 = ? AND iso2 = ? AND as_of <= ? "
        "ORDER BY as_of DESC LIMIT ?",
        (iso1, iso2, effective, window),
    ).fetchall()
    days = []
    for r in reversed(rows):
        days.append(
            {
                "as_of": r["as_of"],
                "label": r["label"],
                "flag_insufficient": (
                    bool(r["flag_insufficient"])
                    if r["flag_insufficient"] is not None
                    else None
                ),
                "flag_hot": (
                    bool(r["flag_hot"]) if r["flag_hot"] is not None else None
                ),
                "flag_drifting": (
                    bool(r["flag_drifting"])
                    if r["flag_drifting"] is not None
                    else None
                ),
                "flag_atypical": (
                    bool(r["flag_atypical"])
                    if r["flag_atypical"] is not None
                    else None
                ),
            }
        )
    today = days[-1]
    if len(days) < 2:
        delta = {
            "previous_as_of": None,
            "label_change": None,
            "flag_changes": [],
            "text": None,
        }
    else:
        prev = days[-2]
        label_change = None
        if prev["label"] != today["label"]:
            label_change = {"from": prev["label"], "to": today["label"]}
        flag_changes = []
        for flag in FLAGS:
            if prev[flag] != today[flag]:
                flag_changes.append(
                    {"flag": flag, "from": prev[flag], "to": today[flag]}
                )
        if label_change is None and not flag_changes:
            text = "no change since yesterday"
        else:
            parts = []
            if label_change is not None:
                parts.append(
                    f"label {label_change['from']} -> {label_change['to']}"
                )
            for change in flag_changes:
                before = "on" if change["from"] else "off"
                after = "on" if change["to"] else "off"
                parts.append(f"{change['flag']} {before} -> {after}")
            text = "; ".join(parts)
        delta = {
            "previous_as_of": prev["as_of"],
            "label_change": label_change,
            "flag_changes": flag_changes,
            "text": text,
        }
    return {
        "dyad": f"{iso1}_{iso2}",
        "as_of": effective,
        "today_label": today["label"],
        "days": days,
        "delta": delta,
    }


# ------------------------------------------------------------------
# LC-02
# ------------------------------------------------------------------

def _evidence_dots(
    days_present_7: float | None, v_7d: float | None, insufficient: list[str]
) -> int:
    """Provisional dev-only evidence rule (see module docstring)."""
    dots = 0
    if days_present_7 is not None and days_present_7 >= EVIDENCE_DAYS_FLOOR:
        dots += 2
        if days_present_7 >= EVIDENCE_DAYS_HIGH:
            dots += 1
    if v_7d is not None and v_7d >= EVIDENCE_VOLUME_FLOOR:
        dots += 1
    if not insufficient:
        dots += 1
    return dots


def fetch_lc02(
    conn: sqlite3.Connection, iso1: str, iso2: str, as_of: str | None
) -> dict:
    """Evidence / stability / data rows at one as-of (rollup only).

    Rows are NEVER averaged into a composite: no composite field
    is produced anywhere in this payload.
    """
    effective = _resolve_rollup_as_of(conn, iso1, iso2, as_of)
    if effective is None:
        return {
            "dyad": f"{iso1}_{iso2}",
            "as_of": None,
            "provisional": True,
            "rows": [
                {
                    "row": "evidence",
                    "dots": None,
                    "reason": "No rollup row for this dyad at this as-of.",
                    "inputs": None,
                    "provisional": True,
                },
                {
                    "row": "stability",
                    "dots": None,
                    "reason": "No rollup row for this dyad at this as-of.",
                    "inputs": None,
                    "provisional": True,
                },
                {
                    "row": "data",
                    "dots": None,
                    "reason": DATA_REASON,
                    "inputs": None,
                    "provisional": False,
                },
            ],
        }
    row = _rollup_row(conn, iso1, iso2, effective)
    assert row is not None  # resolved as-of guarantees a row
    insufficient = _parse_insufficient(row.get("insufficient_sets"))
    sufficient = [s for s in SETS if s not in insufficient]
    days_present_7 = row.get("days_present_7")
    v_7d = row.get("v_7d")
    dots_ev = _evidence_dots(days_present_7, v_7d, insufficient)
    evidence_reason = (
        f"7-day volume v_7d={v_7d}, days present "
        f"{days_present_7}/7, {len(sufficient)} of 5 sets with "
        "sufficient support"
        + (
            f" (insufficient: {', '.join(insufficient)})"
            if insufficient
            else ""
        )
        + ". Provisional dev-only scoring (see LC_DEV_NOTES.md)."
    )
    per_set = {}
    agree = 0
    compared = 0
    for s in SETS:
        r7 = row.get(f"dir_r7_{s}")
        r28 = row.get(f"dir_r28_{s}")
        is_agree = r7 is not None and r28 is not None and r7 == r28
        if r7 is not None and r28 is not None:
            compared += 1
            if is_agree:
                agree += 1
        per_set[s] = {"r7_dir": r7, "r28_dir": r28, "agree": is_agree}
    if compared:
        stability_reason = (
            f"R7 and R28 directions agree on {agree} of {compared} "
            "sets with both directions present"
            + (
                " (disagree: "
                + ", ".join(s for s in SETS if not per_set[s]["agree"]
                            and per_set[s]["r7_dir"] is not None
                            and per_set[s]["r28_dir"] is not None)
                + ")"
                if agree < compared
                else ""
            )
            + ". Provisional dev-only scoring (see LC_DEV_NOTES.md)."
        )
    else:
        stability_reason = (
            "No set has both an R7 and an R28 direction present. "
            "Provisional dev-only scoring (see LC_DEV_NOTES.md)."
        )
    return {
        "dyad": f"{iso1}_{iso2}",
        "as_of": effective,
        "provisional": True,
        "rows": [
            {
                "row": "evidence",
                "dots": dots_ev,
                "reason": evidence_reason,
                "inputs": {
                    "v_7d": v_7d,
                    "days_present_7": days_present_7,
                    "sets_with_sufficient_support": sufficient,
                    "sets_with_insufficient_support": insufficient,
                },
                "provisional": True,
            },
            {
                "row": "stability",
                "dots": agree,
                "reason": stability_reason,
                "inputs": {
                    "per_set": per_set,
                    "agree_count": agree,
                    "compared_count": compared,
                },
                "provisional": True,
            },
            {
                "row": "data",
                "dots": None,
                "reason": DATA_REASON,
                "inputs": None,
                "provisional": False,
            },
        ],
    }


# ------------------------------------------------------------------
# LC-05
# ------------------------------------------------------------------

def _distance_word(std: float | None) -> str | None:
    """Provisional dev-only distance word (see module docstring)."""
    if std is None:
        return None
    magnitude = abs(std)
    if magnitude >= DISTANCE_WELL:
        return "well above usual" if std > 0 else "well below usual"
    if magnitude >= DISTANCE_ABOVE:
        return "above usual" if std > 0 else "below usual"
    return "near usual"


def fetch_lc05(
    conn: sqlite3.Connection, iso1: str, iso2: str, as_of: str | None
) -> dict:
    """At most 3 chips at one as-of (rollup only).

    Candidates are sets with a non-null R7 direction, ranked by
    |R7 std_resid| descending (null std last), ties broken by
    the stored set order. Direction is "mixed over the month"
    when R28 disagrees with R7, else the R7 direction verbatim.
    """
    effective = _resolve_rollup_as_of(conn, iso1, iso2, as_of)
    if effective is None:
        return {
            "dyad": f"{iso1}_{iso2}",
            "as_of": None,
            "provisional": True,
            "chips": [],
        }
    row = _rollup_row(conn, iso1, iso2, effective)
    assert row is not None
    candidates = []
    for order, s in enumerate(SETS):
        r7 = row.get(f"dir_r7_{s}")
        if r7 is None:
            continue
        r28 = row.get(f"dir_r28_{s}")
        std = row.get(f"std_r7_{s}")
        if r28 is not None and r28 != r7:
            direction = "mixed over the month"
        else:
            direction = r7
        candidates.append(
            (
                -(abs(std) if std is not None else -1.0),
                order,
                {
                    "set": s,
                    "direction": direction,
                    "distance": _distance_word(std),
                    "std_resid": std,
                    "share": row.get(f"share_r7_{s}"),
                    "r7_dir": r7,
                    "r28_dir": r28,
                },
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    return {
        "dyad": f"{iso1}_{iso2}",
        "as_of": effective,
        "provisional": True,
        "chips": [chip for _, _, chip in candidates[:3]],
    }


# ------------------------------------------------------------------
# LC-07
# ------------------------------------------------------------------

def _latest_dda_date(
    conn: sqlite3.Connection, iso1: str, iso2: str
) -> str | None:
    row = conn.execute(
        "SELECT MAX(obs_date) AS m FROM dda WHERE iso1 = ? AND iso2 = ?",
        (iso1, iso2),
    ).fetchone()
    return row["m"] if row and row["m"] is not None else None


def _pooled_ratio(
    daily: dict[str, float], day: date, earliest_mapped: str | None
) -> tuple[float | None, float | None, float | None]:
    """(pooled7, baseline_expectation, ratio) at one day.

    daily maps ISO dates to mapped volume (0-filled over the
    calendar). Baseline windows are the 84 rolling 7-day sums
    over the 90 days preceding the pooled window; a ratio needs
    the full 97-day lead-in inside daily AND the validity gate
    (earliest mapped record on or before the baseline start),
    else all three are None. Returns (pooled, expected, ratio);
    the caller derives the tail separately (it needs the window
    list).
    """
    baseline_start = (day - timedelta(days=LC07_LEAD_IN_DAYS)).isoformat()
    if earliest_mapped is None or earliest_mapped > baseline_start:
        return None, None, None
    pooled_start = day - timedelta(days=6)
    base_start = day - timedelta(days=LC07_LEAD_IN_DAYS)
    # Days actually needed: the 90 baseline days [day-97, day-8]
    # plus the 7 pooled days [day-6, day] (day-7 belongs to
    # neither window and is never read).
    base_days = []
    for i in range(90):
        key = (base_start + timedelta(days=i)).isoformat()
        if key not in daily:
            return None, None, None
        base_days.append(daily[key])
    pooled = 0.0
    for i in range(7):
        key = (pooled_start + timedelta(days=i)).isoformat()
        if key not in daily:
            return None, None, None
        pooled += daily[key]
    windows = [
        sum(base_days[k:k + 7]) for k in range(90 - 7 + 1)
    ]
    expected = sum(windows) / len(windows)
    ratio = (pooled / expected) if expected > 0 else None
    return pooled, expected, ratio


def _pooled_tail(
    daily: dict[str, float], day: date, earliest_mapped: str | None
) -> float | None:
    """Share of baseline 7-day windows at or above today's pooled."""
    pooled, _, _ = _pooled_ratio(daily, day, earliest_mapped)
    if pooled is None:
        return None
    base_start = day - timedelta(days=LC07_LEAD_IN_DAYS)
    base_days = [
        daily[(base_start + timedelta(days=i)).isoformat()]
        for i in range(90)
    ]
    windows = [sum(base_days[k:k + 7]) for k in range(90 - 7 + 1)]
    return sum(1 for w in windows if w >= pooled) / len(windows)


def _rarity_word(tail: float) -> str:
    if tail <= LC07_TAIL_LO:
        return "exceptionally loud"
    if tail <= 0.25:
        return "louder than usual"
    if tail < 0.75:
        return "in the usual range"
    if tail < LC07_TAIL_HI:
        return "quieter than usual"
    return "exceptionally quiet"


def fetch_lc07(
    conn: sqlite3.Connection,
    mapped: set[str],
    iso1: str,
    iso2: str,
    as_of: str | None,
) -> dict:
    """Volume-regime readout at one as-of (DDA sidecar only).

    Fetches the daily mapped-volume series ONCE for the whole
    window ([as_of-111, as_of]: 97-day lead-in for today plus
    14 previous days' ratios) and derives everything from it.
    Unknown dyad with no as-of -> as_of null and null values;
    with an as_of given, the requested as-of is echoed with
    null values.
    """
    if as_of is None:
        as_of = _latest_dda_date(conn, iso1, iso2)
    if as_of is None:
        return {
            "dyad": f"{iso1}_{iso2}",
            "as_of": None,
            "pooled_7d_volume": None,
            "baseline_expectation": None,
            "baseline_window_count": None,
            "volume_ratio": None,
            "baseline_tail": None,
            "recent_typical_median": None,
            "typical_valid_days": 0,
            "recent_daily_ratios": [],
            "verdict": None,
            "caution": False,
            "caution_text": None,
            "volume_definition": (
                "pooled 7d MAPPED volume (DDA sidecar, [2.05] definition)"
            ),
            "sidecar": None,
        }
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=LC07_LEAD_IN_DAYS + LC07_TYPICAL_DAYS)
    # ONE fetch: per-code counts over the whole window; mapped
    # filtering happens in Python (the mapped list is a JSON
    # code list, not a table).
    daily: dict[str, float] = {}
    day = start
    while day <= end:
        daily[day.isoformat()] = 0.0
        day += timedelta(days=1)
    for r in conn.execute(
        "SELECT obs_date, cameo_code, obs_count FROM dda "
        "WHERE iso1 = ? AND iso2 = ? AND obs_date >= ? AND obs_date <= ?",
        (iso1, iso2, start.isoformat(), end.isoformat()),
    ):
        if r["cameo_code"] in mapped:
            daily[r["obs_date"]] = daily.get(r["obs_date"], 0.0) + float(
                r["obs_count"]
            )
    # Validity gate only: the dyad's earliest mapped record
    # (one aggregate query, not a per-day fetch).
    earliest_mapped = None
    if mapped:
        placeholders = ", ".join("?" * len(mapped))
        row = conn.execute(
            "SELECT MIN(obs_date) AS m FROM dda WHERE iso1 = ? "
            f"AND iso2 = ? AND cameo_code IN ({placeholders})",
            (iso1, iso2, *sorted(mapped)),
        ).fetchone()
        if row is not None:
            earliest_mapped = row["m"]
    pooled, expected, ratio = _pooled_ratio(daily, end, earliest_mapped)
    tail = _pooled_tail(daily, end, earliest_mapped)
    recent = []
    valid_ratios = []
    for back in range(LC07_TYPICAL_DAYS, 0, -1):
        d = end - timedelta(days=back)
        _, _, r = _pooled_ratio(daily, d, earliest_mapped)
        recent.append({"as_of": d.isoformat(), "volume_ratio": r})
        if r is not None:
            valid_ratios.append(r)
    typical_valid = len(valid_ratios)
    median = (
        statistics.median(valid_ratios)
        if typical_valid >= LC07_TYPICAL_FLOOR
        else None
    )
    if ratio is not None and tail is not None:
        verdict = (
            f"{ratio:.2f} times the usual volume; {_rarity_word(tail)}"
        )
    else:
        verdict = None
    caution = (
        tail is not None
        and ratio is not None
        and (tail <= LC07_TAIL_LO or tail >= LC07_TAIL_HI)
        and (ratio >= LC07_RATIO_HI or ratio <= LC07_RATIO_LO)
    )
    return {
        "dyad": f"{iso1}_{iso2}",
        "as_of": as_of,
        "pooled_7d_volume": pooled,
        "baseline_expectation": expected,
        "baseline_window_count": 84 if pooled is not None else None,
        "volume_ratio": ratio,
        "baseline_tail": tail,
        "recent_typical_median": median,
        "typical_valid_days": typical_valid,
        "recent_daily_ratios": recent,
        "verdict": verdict,
        "caution": caution,
        "caution_text": CAUTION_TEXT if caution else None,
        "volume_definition": (
            "pooled 7d MAPPED volume (DDA sidecar, [2.05] definition)"
        ),
        "sidecar": None,  # filled by the route with _sidecar_meta
    }
