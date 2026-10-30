"""DEV ONLY Look-Closer data access (/lc/ endpoints, Waves 1-2).

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
- LC-06 (Wave 2): DDA sidecar daily joints ONLY. Two pooled
  mixes: now (60 days ending at the effective as-of) and then
  (60 days ending 90 days before it). No largest-movement
  sentence — the frontend computes it from the mixes.
- LC-08 (Wave 2): rollup only. Per set and net: last week's
  R7 std-resid against its p10-p90 band over the trailing
  60 rollup as-ofs (bullet data, Option A).
- LC-09 (Wave 2, Part 1 ONLY — ghost strip parked): rollup
  only. Peer strip: dyads sharing the focal dyad's first
  actor, temperature today + typical temperature (median
  over the previous 14 as-ofs, today excluded), ranks in
  words' numeric form. One peer-history query, never one
  per peer.

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

LC-02 STABILITY RULE (provisional dev-only, revised Dan
2026-10-30 feedback round 1): per analyst set, count the days
in the last STABILITY_WINDOW_DAYS as-ofs (including the
effective as-of) on which that set's R7 direction equals its
R7 direction at the effective as-of. Missing directions count
as not agreeing. Insufficient NEVER agrees (Dan 2026-10-30):
a day counts only when today's direction is a real direction
(up/down/steady) and the day's direction equals it;
"insufficient" on either side is a miss, even
insufficient-vs-insufficient — insufficient is absence of
evidence, not a direction, so a set insufficient all week
scores 0/7, never 7/7. Dot per set, in SETS order:
  agree == 7 -> "green" (held all week);
  4 <= agree <= 6 -> "yellow" (held most of the week);
  agree <= 3 -> "empty" (not held).
Dots are per-set states, NOT a count: the row's dots are
listed in set order, and the reason lists each set's
agree/window count, so colour is never the sole carrier.
SHORT HISTORY FLAG: when fewer than 7 as-of rows are
available for the dyad, window_days < 7 and short_history is
true; the same thresholds apply to the count (so a short
history can never earn green, and a 3-day fully-agreeing
history reads as empty). The frontend must show the
short-history flag whenever short_history is true.

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
As-of resolution (LC-07, Dan 2026-10-30 flag 3 — RESOLVED):
the effective as-of is shared across all four figures. The
/lc/lc-07 route resolves it from the rollup when the dyad has
a rollup row at or before the requested as-of (see
resolve_rollup_as_of); only when the dyad has no rollup row,
or the rollup is unavailable, does LC-07 fall back to the
/dev/ DDA pattern above. The payload's "as_of" is always the
effective as-of.
"""

import math
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
STABILITY_WINDOW_DAYS = 7
STABILITY_GREEN_MIN = 7
STABILITY_YELLOW_MIN = 4
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


def resolve_rollup_as_of(
    conn: sqlite3.Connection, iso1: str, iso2: str, as_of: str | None
) -> str | None:
    """Dyad's latest as-of <= requested (or latest overall).

    Public wrapper so the /lc/lc-07 route can share the same
    effective-as-of rule as the rollup endpoints.
    """
    return _resolve_rollup_as_of(conn, iso1, iso2, as_of)


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
    # Stability (revised 2026-10-30): per-set R7-direction
    # agreement over the last 7 as-ofs including the effective
    # as-of. R7-vs-R28 agreement was retired because it
    # conflated a genuine medium-horizon trend change with
    # short-term noise (Dan feedback round 1). Insufficient
    # never agrees (Dan 2026-10-30): "insufficient" on either
    # side is a miss, even insufficient-vs-insufficient.
    hist = conn.execute(
        "SELECT as_of, "
        + ", ".join(f"dir_r7_{s}" for s in SETS)
        + " FROM dyad_status_rollup WHERE iso1 = ? AND iso2 = ? "
        "AND as_of <= ? ORDER BY as_of DESC LIMIT ?",
        (iso1, iso2, effective, STABILITY_WINDOW_DAYS),
    ).fetchall()
    window_days = len(hist)
    short_history = window_days < STABILITY_WINDOW_DAYS
    per_set = {}
    dot_states = []
    for s in SETS:
        today_dir = row.get(f"dir_r7_{s}")
        agree_days = sum(
            1 for h in hist
            if today_dir is not None and today_dir != "insufficient"
            and h[f"dir_r7_{s}"] == today_dir
        )
        if agree_days >= STABILITY_GREEN_MIN:
            dot = "green"
        elif agree_days >= STABILITY_YELLOW_MIN:
            dot = "yellow"
        else:
            dot = "empty"
        per_set[s] = {
            "today_dir": today_dir,
            "agree_days": agree_days,
            "window_days": window_days,
            "dot": dot,
        }
        dot_states.append(dot)
    stability_reason = (
        "R7 direction agreement over the last "
        f"{window_days} as-of(s) (including today): "
        + ", ".join(
            f"{s} {per_set[s]['agree_days']}/{window_days} "
            f"({per_set[s]['dot']})" for s in SETS
        )
        + (
            f". Short history: stability assessed over only "
            f"{window_days} of {STABILITY_WINDOW_DAYS} days — "
            "interpret with caution."
            if short_history
            else ""
        )
        + ". Provisional dev-only scoring (see LC_DEV_NOTES.md)."
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
                "dots": None,
                "dot_states": dot_states,
                "reason": stability_reason,
                "inputs": {
                    "per_set": per_set,
                    "window_days": window_days,
                    "short_history": short_history,
                    "dot_states": dot_states,
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


# ------------------------------------------------------------------
# LC-06 (Wave 2) — then-and-now Lund mix bars
# ------------------------------------------------------------------

#: Pooled window length for each LC-06 bar (Dan 2026-10-30:
#: 60 days for as-of flexibility in the current dev dataset,
#: NOT a methodological decision).
LC06_WINDOW_DAYS = 60
#: Comparison point: the then window ends this many days
#: before the effective as-of (proposal default, kept).
LC06_COMPARISON_DAYS_AGO = 90
#: Low-support coverage floor (provisional dev-only): a window
#: whose joint coverage falls below this is low-support.
LC06_LOW_SUPPORT_COVERAGE = 0.50


def _lc06_window(
    joint_rows: list[dict],
) -> dict:
    """One LC-06 bar's pooled mix from fetch_joint daily rows.

    Pooled mix = volume-weighted closed geometric mean over
    days with a non-null joint, weights = joint_volume —
    the frontend graphs.dda_pooled_joint log-domain logic,
    mirrored here (daily joints are closed with all phases
    > 0, so the per-phase skip in the frontend version never
    triggers on real rows). days_with_joint counts rows with
    a non-null joint; coverage = joint_volume / volume_total
    (null when the window has no volume at all); mix is null
    when no day has a joint, never a zero mix.
    """
    joint_volume = 0
    volume_total = 0
    days_with_joint = 0
    acc = {p: 0.0 for p in ("p1", "p2", "p3", "p4", "p5")}
    total_w = 0.0
    for row in joint_rows:
        joint_volume += row.get("joint_volume") or 0
        volume_total += row.get("volume_total") or 0
        joint = row.get("joint")
        vol = row.get("joint_volume") or 0
        if joint is None or vol <= 0:
            continue
        days_with_joint += 1
        for p in acc:
            value = joint.get(p)
            if value is None or value <= 0:
                continue
            acc[p] += vol * math.log(value)
        total_w += vol
    mix = None
    if total_w > 0:
        raw = {p: math.exp(acc[p] / total_w) for p in acc}
        total = sum(raw.values())
        if total > 0:
            mix = {p: raw[p] / total for p in acc}
    return {
        "days_with_joint": days_with_joint,
        "joint_volume": joint_volume,
        "volume_total": volume_total,
        "coverage": (
            (joint_volume / volume_total) if volume_total > 0 else None
        ),
        "mix": mix,
    }


def fetch_lc06(
    conn: sqlite3.Connection,
    simplexes: dict,
    iso1: str,
    iso2: str,
    as_of: str | None,
) -> dict:
    """Then-and-now Lund mix bars (DDA sidecar only).

    Effective as-of is resolved by the ROUTE (rollup latest
    <= requested when the dyad has one, else the /dev/ DDA
    pattern — same as LC-07 flag 3); here a None as-of
    resolves to the dyad's latest DDA obs_date, and an
    unknown dyad yields as_of null with null windows.
    Two fetch_joint calls, one per window (never a
    per-day fetch): now = LC06_WINDOW_DAYS ending at the
    effective as-of; then = the same length ending
    LC06_COMPARISON_DAYS_AGO days earlier. No
    largest-movement sentence is produced — the frontend
    computes it from the two mixes. low_support is true
    when either window's coverage is below
    LC06_LOW_SUPPORT_COVERAGE (a null coverage — an empty
    window — is not low-support, it is no data).
    """
    from . import dev_dda_store

    if as_of is None:
        as_of = _latest_dda_date(conn, iso1, iso2)
    if as_of is None:
        empty = {
            "window_start": None,
            "window_end": None,
            "days_with_joint": 0,
            "joint_volume": 0,
            "volume_total": 0,
            "coverage": None,
            "mix": None,
        }
        return {
            "dyad": f"{iso1}_{iso2}",
            "as_of": None,
            "provisional": True,
            "window_days": LC06_WINDOW_DAYS,
            "comparison_days_ago": LC06_COMPARISON_DAYS_AGO,
            "now": dict(empty),
            "then": dict(empty),
            "low_support": False,
            "low_support_reason": None,
        }
    end = date.fromisoformat(as_of)
    then_end = end - timedelta(days=LC06_COMPARISON_DAYS_AGO)
    _, now_rows = dev_dda_store.fetch_joint(
        conn, simplexes, iso1, iso2, end.isoformat(), LC06_WINDOW_DAYS
    )
    _, then_rows = dev_dda_store.fetch_joint(
        conn, simplexes, iso1, iso2, then_end.isoformat(), LC06_WINDOW_DAYS
    )
    now = _lc06_window(now_rows)
    now["window_start"] = (
        end - timedelta(days=LC06_WINDOW_DAYS - 1)
    ).isoformat()
    now["window_end"] = end.isoformat()
    then = _lc06_window(then_rows)
    then["window_start"] = (
        then_end - timedelta(days=LC06_WINDOW_DAYS - 1)
    ).isoformat()
    then["window_end"] = then_end.isoformat()
    # Reorder window fields first (payload readability only).
    for window in (now, then):
        ordered = {
            "window_start": window["window_start"],
            "window_end": window["window_end"],
            "days_with_joint": window["days_with_joint"],
            "joint_volume": window["joint_volume"],
            "volume_total": window["volume_total"],
            "coverage": window["coverage"],
            "mix": window["mix"],
        }
        window.clear()
        window.update(ordered)
    low_parts = []
    for name, window in (("now", now), ("then", then)):
        coverage = window["coverage"]
        if coverage is not None and coverage < LC06_LOW_SUPPORT_COVERAGE:
            low_parts.append(
                f"{name} window coverage {coverage:.2f} < "
                f"{LC06_LOW_SUPPORT_COVERAGE:.2f}"
            )
    return {
        "dyad": f"{iso1}_{iso2}",
        "as_of": as_of,
        "provisional": True,
        "window_days": LC06_WINDOW_DAYS,
        "comparison_days_ago": LC06_COMPARISON_DAYS_AGO,
        "now": now,
        "then": then,
        "low_support": bool(low_parts),
        "low_support_reason": "; ".join(low_parts) if low_parts else None,
    }


# ------------------------------------------------------------------
# LC-08 (Wave 2) — usual-range bullet data
# ------------------------------------------------------------------

#: Trailing rollup-as-of window for LC-08 bands (Dan
#: 2026-10-30: 60 days, matching LC-06's flexibility choice).
LC08_WINDOW_DAYS = 60
#: Minimum present values for a band; fewer -> lo/hi null.
LC08_BAND_MIN_N = 10


def _nearest_rank_band(values: list[float]) -> tuple[float | None, float | None]:
    """(p10, p90) by nearest rank over sorted present values.

    Index ceil(q*n)-1, the same nearest-rank precedent as the
    frontend dda_volume_band p90. Returns (None, None) when
    fewer than LC08_BAND_MIN_N values are present.
    """
    if len(values) < LC08_BAND_MIN_N:
        return None, None
    ordered = sorted(values)
    n = len(ordered)
    lo = ordered[max(0, math.ceil(0.10 * n) - 1)]
    hi = ordered[max(0, math.ceil(0.90 * n) - 1)]
    return lo, hi


def _lc08_row(
    last_week: float | None, values: list[float]
) -> dict:
    """One LC-08 bullet row: last week vs its usual band.

    inside is False whenever the comparison cannot be made
    (null value or null band); distance_outside is 0 when
    inside or when the value is null (per the brief), and
    None — never 0 — when a value exists but its band does
    not, because 0 would falsely read as inside.
    """
    lo, hi = _nearest_rank_band(values)
    if last_week is None or lo is None or hi is None:
        inside = False
        distance = 0 if last_week is None else None
    elif lo <= last_week <= hi:
        inside = True
        distance = 0
    elif last_week > hi:
        inside = False
        distance = last_week - hi
    else:
        inside = False
        distance = last_week - lo
    return {
        "last_week": last_week,
        "lo": lo,
        "hi": hi,
        "n": len(values),
        "inside": inside,
        "distance_outside": distance,
    }


def fetch_lc08(
    conn: sqlite3.Connection,
    iso1: str,
    iso2: str,
    as_of: str | None,
) -> dict:
    """Usual-range bullet data (rollup only).

    History = the last LC08_WINDOW_DAYS rollup rows <= the
    effective as-of, including it. Per set: last_week is the
    stored std_resid_r7 at the effective row (null if
    missing, never zero-filled); the band is p10-p90 over
    that set's present std_resid_r7 values in the history.
    Net: per as-of R = (esc+inst+CI)-(stab+deesc) over
    std_resid_r7, computed ONLY on as-ofs where all five
    stds are present (the frontend residual_values skipping
    rule); net last_week is R at the effective row (null
    when any std is missing there), and the net band is
    p10-p90 over the computable R values. Unknown dyad ->
    as_of null with null rows, mirroring LC-01.
    """
    effective = _resolve_rollup_as_of(conn, iso1, iso2, as_of)
    empty_row = {
        "last_week": None,
        "lo": None,
        "hi": None,
        "n": 0,
        "inside": False,
        "distance_outside": 0,
    }
    if effective is None:
        return {
            "dyad": f"{iso1}_{iso2}",
            "as_of": None,
            "provisional": True,
            "window_days": LC08_WINDOW_DAYS,
            "history_rows": 0,
            "band_method": (
                "p10-p90 nearest-rank over trailing 60 rollup "
                "as-ofs incl. effective"
            ),
            "per_set": {s: dict(empty_row) for s in SETS},
            "net": dict(empty_row),
        }
    cols = ", ".join(f"std_r7_{s}" for s in SETS)
    rows = conn.execute(
        f"SELECT as_of, {cols} FROM dyad_status_rollup "
        "WHERE iso1 = ? AND iso2 = ? AND as_of <= ? "
        "ORDER BY as_of DESC LIMIT ?",
        (iso1, iso2, effective, LC08_WINDOW_DAYS),
    ).fetchall()
    rows = list(reversed(rows))
    effective_row = next(
        (r for r in rows if r["as_of"] == effective), None
    )
    per_set = {}
    for s in SETS:
        values = [
            r[f"std_r7_{s}"] for r in rows
            if r[f"std_r7_{s}"] is not None
        ]
        last_week = (
            effective_row[f"std_r7_{s}"] if effective_row is not None
            else None
        )
        per_set[s] = _lc08_row(last_week, values)
    net_values = []
    net_last_week = None
    for r in rows:
        stds = [r[f"std_r7_{s}"] for s in SETS]
        if any(v is None for v in stds):
            continue
        value = (
            stds[0] + stds[2] + stds[4] - stds[1] - stds[3]
        )
        net_values.append(value)
        if r["as_of"] == effective:
            net_last_week = value
    return {
        "dyad": f"{iso1}_{iso2}",
        "as_of": effective,
        "provisional": True,
        "window_days": LC08_WINDOW_DAYS,
        "history_rows": len(rows),
        "band_method": (
            "p10-p90 nearest-rank over trailing 60 rollup "
            "as-ofs incl. effective"
        ),
        "per_set": per_set,
        "net": _lc08_row(net_last_week, net_values),
    }


# ------------------------------------------------------------------
# LC-09 (Wave 2, Part 1 ONLY) — peer strip
# ------------------------------------------------------------------

#: Previous as-ofs (excluding effective) for typical temps.
LC09_TYPICAL_DAYS = 14
#: Valid-temperature floor for a typical temperature.
LC09_TYPICAL_FLOOR = 7
#: Support floors (borrowed from the LC-02 evidence floors:
#: days_present_7 >= 4, v_7d >= 30 — provisional dev anchors,
#: not formalization decisions).
LC09_SUPPORT_DAYS_FLOOR = 4
LC09_SUPPORT_VOLUME_FLOOR = 30


def _lc09_supported(days_present, v_7d) -> bool:
    """Supported = both evidence floors met (nulls fail)."""
    return (
        isinstance(days_present, (int, float))
        and not isinstance(days_present, bool)
        and days_present >= LC09_SUPPORT_DAYS_FLOOR
        and isinstance(v_7d, (int, float))
        and not isinstance(v_7d, bool)
        and v_7d >= LC09_SUPPORT_VOLUME_FLOOR
    )


def _lc09_typical(temps_latest_first: list) -> float | None:
    """Median over a dyad's latest LC09_TYPICAL_DAYS as-ofs.

    Caller passes temperatures-or-None in latest-first as-of
    order, already capped at LC09_TYPICAL_DAYS rows: present
    temps among those rows are the valid set, and fewer
    than LC09_TYPICAL_FLOOR valid temps -> null. A null
    temperature inside the window does NOT extend the
    window further back — the window is as-ofs, not
    valid temperatures.
    """
    temps = [t for t in temps_latest_first if t is not None]
    if len(temps) < LC09_TYPICAL_FLOOR:
        return None
    return statistics.median(temps)


def fetch_lc09(
    conn: sqlite3.Connection,
    iso1: str,
    iso2: str,
    as_of: str | None,
) -> dict:
    """Peer strip, Part 1 ONLY (rollup only; ghost strip parked).

    Peer rule (proposal, named on the figure): all dyads with
    the same first actor. Peers = dyads with iso1 == focal
    iso1, iso2 != focal iso2, with a rollup row at exactly
    the effective as-of — ONE indexed query. Typical
    temperatures come from ONE peer-history query for the
    whole peer set (iso1 = focal iso1, as_of < effective,
    grouped in Python) — never one query per peer. Rank =
    1 + peers strictly greater (today: temperature; typical:
    typical_temperature, over peers with a non-null value in
    each case); a null focal value -> null rank. Peers sort
    by temperature descending, nulls last, dyad ascending
    as the deterministic tie-break. Unknown dyad -> as_of
    null with null focal values and no peers.
    """
    effective = _resolve_rollup_as_of(conn, iso1, iso2, as_of)
    focal_dyad = f"{iso1}_{iso2}"
    if effective is None:
        return {
            "dyad": focal_dyad,
            "as_of": None,
            "provisional": True,
            "peer_rule": "all dyads with the same first actor",
            "focal": {
                "dyad": focal_dyad,
                "temperature": None,
                "typical_temperature": None,
                "rank": None,
                "typical_rank": None,
                "peers_with_temperature": 0,
                "peers_with_typical": 0,
            },
            "peers": [],
        }
    peer_rows = conn.execute(
        # INDEXED BY is load-bearing, not cosmetic: without
        # it the planner picks idx_rollup_asof_label (as_of=?)
        # for this iso1+as_of query and walks 26,030 scattered
        # table rows in the 3.9 GB rollup — 42s for USA on the
        # VM, over the frontend's 30s client timeout. With
        # the dyad index the same query is 0.02s. Real rollup
        # DBs carry idx_rollup_dyad (iso1, iso2, as_of).
        "SELECT iso1, iso2, temperature, days_present_7, v_7d "
        "FROM dyad_status_rollup INDEXED BY idx_rollup_dyad "
        "WHERE iso1 = ? AND iso2 != ? "
        "AND as_of = ?",
        (iso1, iso2, effective),
    ).fetchall()
    focal_row = _rollup_row(conn, iso1, iso2, effective)
    focal_temp = (
        focal_row.get("temperature") if focal_row is not None else None
    )
    # ONE history query for focal + peers (and any other
    # same-first-actor dyad — filtered to focal+peers below).
    # Bounded to 45 days before effective: the rollup panel
    # is daily (270 as-ofs, 2026-01-04..09-30), so the latest
    # 14 as-ofs always fall inside the bound, and the bound
    # keeps the scan to ~45 days x peers instead of the full
    # history (USA: 214 peers x 270 as-ofs took ~42s on the
    # VM's I/O-bound rollup, over the frontend's 30s client
    # timeout; bounded, it must stay well inside it). A
    # sparse dyad with fewer rows inside the bound uses
    # what is available, under the same 7-valid-day floor.
    wanted = {(iso1, iso2)} | {
        (r["iso1"], r["iso2"]) for r in peer_rows
    }
    temps_by_dyad: dict[tuple[str, str], list] = {}
    for r in conn.execute(
        "SELECT iso1, iso2, temperature FROM dyad_status_rollup "
        "WHERE iso1 = ? AND as_of < ? AND as_of >= date(?, '-45 days') "
        "ORDER BY as_of DESC",
        (iso1, effective, effective),
    ):
        key = (r["iso1"], r["iso2"])
        if key not in wanted:
            continue
        # Cap at the latest LC09_TYPICAL_DAYS ROWS per dyad,
        # null temperatures included: the window is as-ofs,
        # and a null inside it does not extend it backwards.
        rows_for_dyad = temps_by_dyad.setdefault(key, [])
        if len(rows_for_dyad) < LC09_TYPICAL_DAYS:
            rows_for_dyad.append(r["temperature"])
    focal_typical = _lc09_typical(
        temps_by_dyad.get((iso1, iso2), [])
    )
    peers = []
    for r in peer_rows:
        key = (r["iso1"], r["iso2"])
        peers.append(
            {
                "dyad": f"{r['iso1']}_{r['iso2']}",
                "temperature": r["temperature"],
                "typical_temperature": _lc09_typical(
                    temps_by_dyad.get(key, [])
                ),
                "supported": _lc09_supported(
                    r["days_present_7"], r["v_7d"]
                ),
                "days_present_7": r["days_present_7"],
                "v_7d": r["v_7d"],
            }
        )
    peers.sort(
        key=lambda p: (
            p["temperature"] is None,
            -(p["temperature"]) if p["temperature"] is not None else 0,
            p["dyad"],
        )
    )
    peers_with_temp = sum(
        1 for p in peers if p["temperature"] is not None
    )
    peers_with_typical = sum(
        1 for p in peers if p["typical_temperature"] is not None
    )
    rank = None
    if focal_temp is not None:
        rank = 1 + sum(
            1 for p in peers
            if p["temperature"] is not None
            and p["temperature"] > focal_temp
        )
    typical_rank = None
    if focal_typical is not None:
        typical_rank = 1 + sum(
            1 for p in peers
            if p["typical_temperature"] is not None
            and p["typical_temperature"] > focal_typical
        )
    return {
        "dyad": focal_dyad,
        "as_of": effective,
        "provisional": True,
        "peer_rule": "all dyads with the same first actor",
        "focal": {
            "dyad": focal_dyad,
            "temperature": focal_temp,
            "typical_temperature": focal_typical,
            "rank": rank,
            "typical_rank": typical_rank,
            "peers_with_temperature": peers_with_temp,
            "peers_with_typical": peers_with_typical,
            "label": (
                focal_row.get("label")
                if focal_row is not None else None
            ),
        },
        "peers": peers,
    }
