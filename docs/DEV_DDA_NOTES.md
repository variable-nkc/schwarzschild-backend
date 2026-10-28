# DEV DDA spike — notes (dev/dda-sidecar branch)

**Status: dev measurements, 2026-10-28. Not contract.** This is
the Part D spike output for VET_DDA_NEEDS.md: what was built,
what it measured, and what formalization still has to decide.
VM numbers only — the ~2s tripwire is judged on Dan's machine.

## What was built

- **Sidecar:** `/home/hatch/workspace/user/files/dda_sidecar/dda_poc_base.sqlite`
  (builder: `tools/build_dda_sidecar.py`, rerunnable, refuses
  to overwrite without `--force`). Verbatim copy of the
  mirror's `analytics_dyad_date_actions` (all 6 columns) +
  `idx_dda_dyad_date (iso1, iso2, obs_date)` + `_sidecar_meta`
  stamp. 2,511,769 rows, 2025-09-30 .. 2026-09-30, 161,554,432
  bytes (~154 MB vs the mirror's 3.06 GB — 19× smaller).
  Source mirror dump `poc_base_20260930.sql`, built
  2026-10-28T10:55:21Z. Regenerate from the mirror on DDA
  extension; never hand-edit.
- **Dev endpoints** (`/dev/dda-daily`, `/dev/dda-codes`, in
  `app/main.py` DEV section + `app/dev_dda_store.py`, config
  `DDA_SQLITE` / `DDA_SETS_JSON`): dyad-scoped, windowed
  (default 60, 1..365), as-of optional (latest dyad date),
  envelope + version gate + 503 discipline identical to v1,
  provenance message marked DEV ONLY. Daily shares computed
  server-side (dev decision, see below). Tests:
  `tests/test_dev_dda.py` (5 tests); full suite 28 passed.
- **Dev server used for measurement:** port 8125, stopped
  after measurement (port verified free).

## Source facts learned (check before assuming)

- The mirror's DDA table has **no p1..p5**: columns are
  `id, iso1, iso2, obs_date, cameo_code, obs_count`. The
  simplex lives in the crosswalk tables; the wide form
  (`gdelt_codes_rel_cameo_soc_wide`, p1_prob..p5_prob) is
  EMPTY in this mirror, the long form (`soc_long`, 1590 rows
  = 318 codes × 5) is the usable one. Round 1 correctly uses
  membership-based set shares, not simplex composition.
- DDA range 2025-09-30 .. 2026-09-30 covers every Vet window
  (60d, 270d) plus baseline lead-in. Longer history is a data
  question (DDA extension), not a sidecar question.
- The mirror ALREADY carries `idx_dda_dyad_date
  (iso1, iso2, obs_date)` — so mirror-direct is not an
  unindexed baseline. See the consequence below.
- Thin dyads are sparse, not empty, at DDA grain: ETH_DJI
  has 155 DDA rows total, 63 days with data in 270d, 3 codes
  on 2026-09-30 (vs USA_CAN 9,884 rows, AFG_PAK 4,612).
  AFG_PAK's 270d volume has 269 days — one gap day even for
  a dense dyad.

## Measurements (VM, first-run + warm median)

SQL shapes (mirror table vs sidecar table, same queries):

| dyad | shape | mirror first/warm | sidecar first/warm |
|---|---|---|---|
| USA_CAN | daily 60d (1,829 code-rows) | 0.007s / 0.002s | 0.008s / 0.002s |
| USA_CAN | codes (25 day / 118 window) | 0.001s / 0.001s | 0.003s / 0.001s |
| USA_CAN | volume 270d (270 days) | 0.008s / 0.002s | 0.006s / 0.003s |
| AFG_PAK | daily 60d (528 code-rows) | 0.003s / 0.001s | 0.012s / 0.001s |
| AFG_PAK | codes (11 / 67) | 0.001s / 0.001s | 0.001s / 0.000s |
| AFG_PAK | volume 270d (269 days) | 0.006s / 0.008s | 0.005s / 0.001s |
| ETH_DJI | daily 60d (28 code-rows) | 0.003s / 0.000s | 0.002s / 0.000s |
| ETH_DJI | codes (3 / 18) | 0.000s / 0.000s | 0.000s / 0.000s |
| ETH_DJI | volume 270d (63 days) | 0.000s / 0.000s | 0.000s / 0.000s |

HTTP via the dev server (sidecar): daily60 0.069s first /
0.008s warm (USA_CAN), codes60 0.006s / 0.006s; all dyads
≤ 0.069s first, ≤ 0.008s warm. Payloads are small: daily60
= 60 rows; codes60 = 118 / 67 / 18 rows for the three
dyads.

**EXPLAIN (both files, both shapes):** daily and codes
queries `SEARCH ... USING INDEX idx_dda_dyad_date
(iso1=? AND iso2=? AND obs_date>? AND obs_date<?)` on
mirror and sidecar alike. Codes GROUP BY adds `USE TEMP
B-TREE FOR GROUP BY` — harmless at these set sizes
(tens of codes).

## Findings (what the spike actually says)

1. **No latency case for the sidecar at dyad scope.**
   Mirror-direct and sidecar measure identically (all
   ≤ 0.012s SQL, ≤ 0.069s HTTP on the VM) because the
   mirror already has the serving index. The sidecar's case
   rests on separation, size (19× smaller), and freedom to
   add indexes without touching the archive — not speed.
   Do not sell it as faster.
2. **No second index needed for round 1.** The single
   `(iso1, iso2, obs_date)` index serves daily, codes, and
   270d-volume shapes. A code-keyed index would only
   matter for code-across-time queries (round 2 daily code
   series) — deferred, not built.
3. **Tripwire NOT tripped on the VM.** Everything is two
   orders of magnitude under ~2s. The Dan's-machine check
   is still owed at pickup (VM numbers are indicative).
4. **Payloads are small.** No pagination needed for round-1
   shapes; the deferred daily code series is the shape that
   could change that.

## Open formalization decisions (do not ship without these)

- Server computes daily shares (dev) vs serves raw counts
  and lets the frontend derive (R28 precedent). The dev
  endpoints chose server-side for prototyping speed.
- "Mapped" = union of the five action sets (dev
  approximation) — confirm against the soc_long 318-code
  denominator before contracting.
- Codes baseline: dev uses mean daily count over window
  length (provisional, labeled). Part B2 candidates still
  open.
- Backend sets location: dev reads the analytics repo's
  `cameo_groups.json` via env (dev shortcut). The contract
  needs a real home for the sets (backend copy? shared
  location?).
- Second provenance stamp shape: dev puts the sidecar meta
  inside `data.sidecar`; the contract decides whether the
  envelope carries two stamps.
- Env names `DDA_SQLITE` / `DDA_SETS_JSON` are dev names;
  confirm or rename at contract time.

## Future simplex extension (prepared, not used)

- Env: `DDA_SIMPLEXES_JSON` (config accessor present, no
  endpoint reads it). Intended source: `cameo_tokens.json`.
- That file is NOT present in the VM analytics checkout
  (checked 2026-10-28: `data-refs/` holds only
  `cameo_groups.json`); its location is Dan's to confirm
  when the extension is picked up.
- Pending: Dan's confirmation of the soc_long table as the
  simplex source. Until then, no simplex-based composition
  is served anywhere; round 1 uses membership-based set
  shares only, and no schema decision about simplex fields
  is made here.
