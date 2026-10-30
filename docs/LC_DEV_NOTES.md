# LC dev endpoints — notes (dev/dda-sidecar branch, Wave 1)

**Status: dev only, 2026-10-30. Not contract.** The `/lc/`
endpoints serve Look-Closer Wave 1 figures (LC-01, LC-02, LC-05,
LC-07) from the rollup and the DDA sidecar. They are DEV ONLY
and never part of the v1 contract: do not build frontend
features on these paths as contract, and do not promote them
without the formalization step. Every envelope's provenance
message is exactly:

> LC DEV ONLY — not part of the v1 contract.

## What was built

- **Module:** `app/lc_store.py` (plain dicts, same split as
  `app/store.py`) + routes in `app/main.py` (`/lc/lc-01`,
  `/lc/lc-02`, `/lc/lc-05`, `/lc/lc-07`). All GET, iso1/iso2
  required, as_of optional. Windows END AT as-of, never at the
  snapshot end.
- **Env names reused from /dev/** (no new names):
  `ROLLUP_SQLITE` (LC-01/02/05), `DDA_SQLITE` +
  `DDA_SIMPLEXES_JSON` (LC-07, mapped code list only; the sets
  file is not needed). Missing config means 503 with the
  missing detail in the error message (the provenance message
  stays exactly the LC string).
- **Unknown dyads** return 200 with empty rows/nulls, never
  404 — the /dev/ discipline.
- **Version gate (RESOLVED 2026-10-30, Dan flag 1):** `/lc/`
  endpoints apply the SAME gate as `/dev/` (strict 409, warn
  warning). The Wave 1 build was exempt per a brief premise
  that /dev/ is exempt; that premise was wrong (/dev/ is gated,
  pinned by `tests/test_dev_dda.py::test_dev_version_gate_strict`),
  and Dan flipped /lc/ to match /dev/ for now, deferring
  versioning questions until the fast feedback/dev cycle ends.
- **Tests:** `tests/test_lc.py` (9 tests); full suite 38
  passed (29 pre-existing + 9 new).

## Endpoint notes

- **LC-01** — rollup history, 28 as-ofs ending at as-of, one
  row per as-of with the stored triage label + 4 component
  flags (`flag_insufficient`, `flag_hot`, `flag_drifting`,
  `flag_atypical`). Response: `today_label`, `days`, and a
  delta vs the previous as-of (label change + flag changes
  only); when nothing changed the delta text is exactly
  "no change since yesterday". No recent-typical mark is
  computed or returned. Effective as-of = the dyad's latest
  stored as-of <= the requested as-of (latest overall when
  omitted).
- **LC-02** — rollup only, one as-of. Three rows (evidence,
  stability, data), NEVER averaged into a composite — no
  composite field exists in the payload. The data row is
  always dots null with reason exactly: "mirror
  record/source line of effort, not served by rollup or /lc/
  endpoints".
- **LC-05** — rollup only, one as-of. At most 3 chips, ranked
  by |R7 std_resid| descending over sets with a non-null R7
  direction (null std last, ties by stored set order). Chip
  direction is "mixed over the month" when the R28 direction
  is present and differs from R7; otherwise the stored R7
  direction verbatim ("up", "down", or "steady" — the rollup
  stores three direction values, so an agreed steady is
  passed through, not re-labeled; flagged for confirmation
  since the brief's vocabulary listed only up/down/mixed).
  If no set has a direction, `chips` is empty.
- **LC-07** — DDA sidecar daily volumes ONLY (not rollup
  pooled totals, not the PCS panel). One daily-series fetch
  per request (plus one aggregate MIN query for the validity
  gate), never one fetch per day. See the [2.05] definition
  below. Rarity (the tail) is stated for today only; no
  rarity statement is made for the typical level.

## Provisional scoring rules (quoted verbatim from app/lc_store.py)

These rules are PROVISIONAL dev-only scaffolding. They are NOT
formalized methodology and must never be presented as such.

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
  in the last 7 as-ofs (including the effective as-of) on which
  that set's R7 direction equals its R7 direction at the
  effective as-of. Missing directions count as not agreeing.
  Dot per set, in set order: agree 7/7 -> green (held all
  week); 4-6/7 -> yellow (held most of the week); <=3/7 ->
  empty (not held). The R7-vs-R28 rule was retired because it
  conflated a genuine medium-horizon trend change with
  short-term noise. SHORT HISTORY FLAG: when fewer than 7
  as-of rows are available, window_days < 7 and short_history
  is true; the same count thresholds apply (a short history
  can never earn green), and the frontend shows the flag.

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

LC-07 CAUTION RULE (dev-only wording threshold, not
methodology): caution is true exactly when today's volume is
BOTH rare (baseline tail <= 0.05 or >= 0.95) AND large in level
(volume_ratio >= 1.5 or <= 2/3); otherwise false with null text.

## [2.05] volume definition (traced, replicated exactly)

Source: `volume_regime` in the analytics worktree
(`src/seqbayes/gallery_data.py`), gallery figure [2.05]
("Channel indicators", §1.4 aggregate half):

- Daily volume is MAPPED volume only: obs_count summed over
  codes in the mapped list (soc_long / `DDA_SIMPLEXES_JSON`
  code list), NOT total volume over all codes. Days with no
  mapped records count as 0 (0-filled over the calendar, as
  the gallery's `build_daily` does).
- Pooled 7d volume at day d = sum of daily mapped volume over
  [d-6, d] (trailing 7 days ending at d, inclusive).
- Baseline expectation at day d = mean of the 84 rolling
  7-day sums over the 90 days immediately preceding the pooled
  window, i.e. over [d-97, d-8]. Baseline tail at day d =
  share of those 84 windows at or above the pooled volume
  (near 0 = exceptionally loud, near 1 = exceptionally quiet).
- Daily-ratio series (for the 14-day recent-typical median):
  the figure's own daily volume ratio evaluated at each of the
  previous 14 days (today excluded), where each day's ratio is
  that day's pooled-7d volume over that day's own trailing-90d
  baseline expectation — NOT a daily-volume ratio. The median
  is over valid (non-null) ratios only, with a 7-valid-day
  floor: fewer than 7 valid days -> median null, never
  back-filled.
- Validity gate (backend addition, documented in
  `app/lc_store.py`): a day's ratio is valid only if the dyad
  has at least one mapped record dated on or before that day's
  baseline start (day-97). Without this, a dyad first seen
  days ago would get a near-zero baseline and a nonsense
  ratio; instead its ratio is null.

## NOT implemented in this round

- Preprocessing recommendations from the proposal are NOT
  implemented: cooling, per-set exceedance, and channel
  indicators stay on-demand or placeholder. LC-07 serves the
  [2.05] volume regime only; it does not cool, exceed, or
  channel-adjust anything.
- LC-03 / LC-04 / LC-10 are placeholders pending analyst
  feedback (frontend work, not backend). No backend routes
  exist for them in this round.

## Resolved flags (Dan, 2026-10-30)

1. Version gate: flipped to match /dev/ (see above).
2. LC-05 "steady" passthrough: KEPT as built. Dan prefers
   the state made explicit: agreed-steady sets surface as
   chips with direction "steady"; omitting them could read as
   a bug / missing data rather than a real piece of
   information. No change.
3. Effective as-of consistency: LC-07 now shares LC-01/02/05's
   rule — the dyad's latest rollup row <= requested is the
   effective as-of for all four figures. Fallback: a dyad
   with no rollup row (or rollup unavailable) keeps the /dev/
   DDA pattern for LC-07 only; the other figures are empty
   for such a dyad in any case.

## Still open / dev choices (not proposal text)

- LC-07 caution trigger (rarity + level, see above) and the
  validity gate are dev choices, not proposal text.
- LC-01/02/05 effective-as-of resolution (latest stored
  as-of <= requested) vs exact-match: chosen so windows end
  at as-of even for sparse dyads.
