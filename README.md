# schwarzschild-backend

Realtime UI processing & data delivery — a thin FastAPI serving
layer over precomputed schwarzschild analytics products, for a
Streamlit frontend.

## What this is (and is not)

This service **serves** analytics products. It does not compute
them. PCS, rollup, temperature, and anomaly logic live in the
analytics repo (`schwarzschild`); this repo only reads the SQLite
rollup database those builders produce and returns rows (or
history projections of rows) as JSON.

Consequences of that split, by design:

- **No statistics are reimplemented here.** If a value is not in
  the rollup, v1 does not invent it.
- **No pandas dependency in v1.** Data access is plain `sqlite3`
  plus dataclasses/dicts internally; pydantic models exist only at
  the request/response boundary.
- **No data products are committed to this repo.** The rollup
  database is pointed at with an environment variable (below), and
  `*.sqlite` / `*.db` files are gitignored. A snapshot is
  identified by its provenance stamp, not by a file checked in
  here.

## Running

Python 3.12. Create a virtual environment for *this* repo (do not
reuse the analytics environment — the split is the point):

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Point the app at a rollup database. The simplest way is a local
`.env` file in this repo root (the app loads it at startup via
`load_dotenv` in `app/config.py`; shell variables still win over
`.env`, so an inline export overrides the file when you need it):

```bash
cp .env.example .env   # then edit ROLLUP_SQLITE to your rollup path
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Or, without a `.env` file, the old inline form still works:

```bash
ROLLUP_SQLITE=/path/to/rollup_poc_base.sqlite \
API_VERSION_MODE=strict \
.venv/bin/python -m uvicorn app.main:app --port 8000
```

`/path/to/rollup_poc_base.sqlite` is a placeholder: replace it with
the full path to a real `rollup_poc_base.sqlite` file (table
`dyad_status_rollup`). There is no hardcoded default path, on
purpose — a default that only works on one machine would silently
serve the wrong database (or none) everywhere else. With
`ROLLUP_SQLITE` unset, the app still starts and `GET /health`
still returns ok, but the data endpoints return 503 with a clear
message.

Interactive docs (OpenAPI) are at `http://127.0.0.1:8000/docs`
once the server is running. Note: the OpenAPI document's own
version field is not the payload contract version — see
"Versioning" below.

## Endpoints (v1 skeleton)

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness. Always 200 with `status: ok`, even with no DB configured. Exempt from the version gate (a liveness probe must not fail for want of a version declaration). |
| `GET /dyad-status?iso1=&iso2=&as_of=` | One dyad at one as-of: the **full** rollup row as JSON, support fields included. `as_of` is optional; without it the dyad's latest as-of is used. 404 when the row does not exist. |
| `GET /watchlist?as_of=&limit=&labels=` | Rollup rows at one as-of, full rows with support fields. `as_of` is optional (latest as-of when omitted). `limit` defaults to 100 (1–1000). `labels` repeats (`?labels=hot&labels=drifting`) or takes a comma-separated value; unknown labels are a 400. |
| `GET /pcs-series?iso1=&iso2=` | The dyad's rollup history, oldest as-of first: per-set R7/R28 direction and std_resid, R7 share, temperature, label, the stored max exceedance, and the stored anomaly series per as-of. See "PCS series" below. |
| `GET /census?as_of=` | Label counts at one as-of (latest as-of when omitted). An as-of with no rows returns all-zero counts, not an error. Counts are read-time aggregates over stored rows, not recomputed statistics. |
| `GET /census-series` | Label counts per as-of, oldest first: the composition-over-time series. Same aggregate as `/census`, over all as-ofs. |

### Watchlist ordering (v1 first pass)

Watchlist rows are returned in hot-first **label-priority** order:

```
insufficient -> hot -> drifting -> atypical -> steady
```

then by dyad within a label. This is the label *assignment*
priority (when several flags fire, the label is the first flag in
this order), used here as a listing order for a first pass. In
practice a client will usually filter out `insufficient` (via the
`labels` parameter) — the priority order is about which label a
row carries, not a claim that insufficient dyads are the most
interesting watchlist entries.

### Labels

The current label priority is hot-first, as decided for the
September products onward. The August 13 golden pins were built
under the earlier literal priority (insufficient → drifting →
hot → atypical → steady) and are grandfathered as a literal-era
record; they were not re-cut. Component flags
(`flag_insufficient`, `flag_drifting`, `flag_hot`,
`flag_atypical`) are stored on every row, so a future priority
change is a relabel, not a rebuild.

## Response envelope

**Every** response — success or error — carries this envelope:

```json
{
  "api_version": "1",
  "provenance": {
    "build_id": "a3d9769",
    "vintage": 366,
    "computed_at": "2026-10-27T10:32:25+00:00",
    "source": "rollup_sqlite",
    "message": null
  },
  "warnings": [],
  "data": { "...": "endpoint payload, or null on error" },
  "error": null
}
```

`build_id`, `vintage`, and `computed_at` are read from the rollup
database when one is configured (a rollup build stamps them
uniformly, so v1 reads the first row). When no database is
configured — or it cannot be read — those fields are null and
`message` says so plainly, instead of the response pretending to
be stamped. On errors, `data` is null and `error` is
`{"code": ..., "message": ...}`.

## Versioning

`api_version` is the **payload contract version**, currently
`"1"`. It versions the shape of responses, not code releases:

- Bump it **only** when a response shape changes in a breaking
  way: a field removed, renamed, or retyped.
- Do **not** bump it for frontend-only changes, backend changes
  that leave payload shapes untouched, bug fixes, performance
  work, or **additive** changes: new fields on existing responses
  and entirely new endpoints. Neither side has to be released in
  lockstep with the other for ordinary changes.

The frontend may send its expected version as the `api_version`
query parameter on data endpoints. Behaviour is controlled by
`API_VERSION_MODE` (default `strict`):

- `strict`: any mismatch fails outright (HTTP 409) with an
  explicit error naming expected vs served, e.g.
  `"expected api_version 2, served api_version 1"`.
- `warn`: the request continues with its data, but the response
  carries a prominent `version_mismatch` warning object in
  `warnings`. Warn mode is a local-development tool: the
  integration test runs in strict mode, because a test that passes
  with a version warning is not a sync check, and a warning must
  be surfaced to the user (a banner), not buried in a log.

When no expected version is sent, no check is performed.

Contract changelog:

- `1` — initial v1 skeleton: `/health`, `/dyad-status`,
  `/watchlist`, `/pcs-series`, provenance envelope, version
  strict/warn handling. Later additive change under the same
  version: `/pcs-series` gained the stored anomaly fields, and
  `/census` + `/census-series` were added (no existing field
  changed, so no bump). Later additive change under the same
  version: `/pcs-series` gained the stored max exceedance
  (`max_exc45`), for the Scan exceedance multiples (no existing
  field changed, so no bump). The multiples grid itself was
  removed from the frontend 2026-10-28, superseded by per-item
  previews; the field is still served and now feeds the Dig
  exceedance pair chart and the Scan row bars. Later additive
  change under the same version: `/pcs-series` gained the set
  attaining the max
  (`max_exc45_set`), for the Dig driving-set strip (no existing
  field changed, so no bump). Later additive change under the
  same version: `/pcs-series` gained the four component label
  flags (`flag_insufficient`, `flag_hot`, `flag_drifting`,
  `flag_atypical`), for the Dig flag lanes (no existing field
  changed, so no bump).

## PCS series: rollup history, not the panel

`GET /pcs-series` serves the dyad's **rollup history**. Per as-of
it carries what the rollup stores: per-set R7/R28 direction, R7
share, and R7/R28 std_resid, plus temperature, label, R7 support
(`v_7d`, `days_present_7`), the stored max exceedance
(`max_exc45`) and the set attaining it (`max_exc45_set`),
the four component label flags (`flag_insufficient`,
`flag_hot`, `flag_drifting`, `flag_atypical`),
and the stored anomaly series
(`anomaly_composite`, `anomaly_composite_raw`, `anomaly_max3d`).
It does **not** carry PCS panel fields (baseline mu, phi, Beta
tails), because those live in the PCS panel parquet, not in the
rollup — and v1 has no pandas dependency with which to read that
parquet. Serving the true panel series (with mu/phi/tails) is a
later object; see "Not built yet". Nothing here should be read
as a panel series.

One more rollup quirk, stated so it is not misread: the rollup
stores an R7 share per set but no R28 share, so the series
carries `share_r7` only. R28 is direction and std_resid only
from this endpoint; the frontend reconstructs the R28 share
exactly from weekly R7 shares (see the frontend's
`docs/DERIVED_FIGURES.md`), it is not served here.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Tests build their own small SQLite fixture (a few
`dyad_status_rollup` rows) in a temporary directory per test.
They cover health (with and without a DB), dyad-status (found,
latest-as-of fallback, not found), watchlist ordering / limit /
label filter / unknown label, PCS-series ordering and per-set
fields (including the stored anomaly fields, max exceedance,
max set, and label flags), census at one as-of / latest
fallback / unknown-as-of zeros / series over time,
provenance envelope presence (including on errors),
version strict vs warn behaviour, and the no-DB 503 path. No test
reads a real analytics product.

## Project layout

```
app/
  main.py        FastAPI app factory + endpoints (HTTP only)
  models.py      Pydantic models, request/response boundary only
  store.py       SQLite access (plain sqlite3/dicts, no pandas)
  config.py      Env vars, contract version, sets, label priority
  versioning.py  Payload-contract version check (strict/warn)
tests/
  test_api.py    Pytest suite with a temporary SQLite fixture
requirements.txt
```

## Not built yet (intentionally)

This is a skeleton, not the full 24-object contract in the
analytics repo's `docs/API_PAYLOAD_LOG.md`. Not built in v1:

- The remaining payload-log objects: panel-scope objects
  (`scan.choropleth`,
  `scan.temperature_direction_scatter`,
  `scan.exceedance_small_multiples` — `scan.label_census` is
  now served as `/census` + `/census-series`), dyad-status
  objects (`brief.figure`, `brief.export_preview`,
  `brief.what_changed`),
  PCS objects (`vet.pooled_evidence`, `vet.daily_vs_pooled`,
  `vet.set_directions`, `dig.pooled_leads`, `dig.triage_table`),
  exceedance/outlook objects (`vet.exceedance_trajectory`,
  `dig.forecast_ladder`), composition/anomaly objects
  (`dig.state_trends`,
  `dig.decomposition`, `dig.driver_summary` — the anomaly
  series behind `dig.anomaly_modulation` is now served as
  `/pcs-series` fields, not as a separate object), code/event grain
  (`dig.code_table`, `dig.event_evidence` — the latter needs
  cursor pagination from day one), and context objects
  (`vet.peers`, `vet.prior_episodes`, `scan.divergence`,
  `vet.channel_indicators`).
- The true PCS panel series (mu/phi/Beta tails from the panel
  parquet). The v1 `/pcs-series` is rollup history only, by
  design (see above).
- Watchlist persistence and tripwire/brief-packet/export
  workflow: those are frontend/workflow features, deferred by
  decision, not backend v1 scope.
- Caching, authentication, and deployment configuration: none
  in v1. This service is for local integration development.

## Future decision points (recorded, not decided here)

- Partial-day rule before continuous ingest goes live: publish
  only complete days vs flag a partial latest day, and who owns
  that rule. The current integration data is a frozen snapshot,
  so this does not gate v1.
- Whether the version check gains major/minor (breaking vs
  additive) semantics, so warn mode can distinguish "backend
  ahead with extra fields" from "frontend expects fields the
  backend does not serve". v1 keeps a single strict/warn switch.
