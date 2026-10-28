"""schwarzschild-backend v1 — a thin serving layer.

This app serves precomputed schwarzschild PoC analytics products
(the rollup SQLite database) to a Streamlit frontend. It does not
recompute any statistics: PCS, rollup, temperature, and anomaly
logic live in the analytics repo, and this service only reads the
products they build. If a value is not in the rollup, v1 does not
invent it (see ``app/store.py:build_series``).

Endpoints (all responses carry the provenance envelope; see
``app/models.py:ApiResponse``):

- ``GET /health`` — liveness only. Always 200 with ``status: ok``,
  even with no database configured. It is exempt from the version
  gate: a liveness probe must not fail because a caller forgot to
  declare a version.
- ``GET /dyad-status?iso1=&iso2=&as_of=`` — one dyad at one as-of,
  the full rollup row. ``as_of`` is optional; without it the dyad's
  latest as-of is used.
- ``GET /watchlist?as_of=&limit=&labels=`` — rollup rows at one
  as-of, full rows with support fields, in hot-first label-priority
  order as a first pass (see config.LABEL_PRIORITY). ``as_of`` is
  optional (latest as-of when omitted). ``labels`` repeats
  (``?labels=hot&labels=drifting``) or takes a comma-separated
  value; unknown labels are a 400.
- ``GET /pcs-series?iso1=&iso2=`` — the dyad's rollup history,
  oldest as-of first. This is a rollup-history series, not the PCS
  panel series (panel-only fields are not in the rollup). Each row
  carries the stored anomaly series (``anomaly_composite``,
  ``anomaly_composite_raw``, ``anomaly_max3d``) alongside the
  per-set blocks.
- ``GET /census?as_of=`` — label counts at one as-of (latest
  as-of when omitted). An as-of with no rows returns all-zero
  counts, not an error, mirroring the watchlist.
- ``GET /census-series`` — label counts per as-of, oldest first:
  the composition-over-time series.

Census counts are read-time aggregates over stored rollup rows,
not recomputed statistics: the labels are stored, the endpoints
only count them. Additive changes (new fields, new endpoints)
do not bump the payload contract version; a bump is reserved
for breaking changes to existing fields or endpoints.

Version checking: data endpoints accept an optional ``api_version``
query parameter (the frontend's expected payload contract
version). See ``app/versioning.py`` for strict vs warn behaviour.

Experimental ``/exp/`` endpoints (this branch only, never v1):
``GET /exp/dda-records``, ``GET /exp/corpus``, ``GET /exp/rollup-at``,
``GET /exp/rollup-dyad``, ``GET /exp/census-series``,
``GET /exp/notable``, ``GET /exp/meta``. They use the same envelope
shape but with ``api_version: "exp"``, are EXEMPT from the version
gate (they take no ``api_version`` parameter and never 409), and
every response carries the provenance message "EXPERIMENTAL — not
part of the v1 contract; never promote." They serve raw rows only:
no shares, joints, PCS, anomalies, or forecasts are computed
server-side, and there is deliberately no ``/exp/pcs-slice``.
See README, "Experimental /exp/ endpoints (never promote)".
"""

import sqlite3

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import config, exp_store, store
from .config import API_VERSION, LABEL_PRIORITY
from .models import ApiResponse, Provenance, VersionWarning
from .versioning import check_version


def _provenance() -> tuple[Provenance, object]:
    """Load the provenance stamp and the usable DB path (or None).

    Returns (provenance, path_or_None). A missing or unreadable
    database is reported in the stamp's message, not raised: the
    caller decides whether that is a 503 (data endpoints) or just
    a stamp with nulls (health).
    """
    path = config.rollup_path()
    if path is None:
        return (
            Provenance(
                source=None,
                message="No rollup DB configured: "
                "set ROLLUP_SQLITE to a rollup_poc_base.sqlite path.",
            ),
            None,
        )
    if not path.exists():
        return (
            Provenance(
                source="rollup_sqlite",
                message="Rollup DB configured via ROLLUP_SQLITE "
                "but the file was not found.",
            ),
            None,
        )
    try:
        prov = store.fetch_provenance(path)
    except sqlite3.Error as exc:
        return (
            Provenance(
                source="rollup_sqlite",
                message=f"Could not read rollup DB provenance: {exc}",
            ),
            None,
        )
    if prov is None:
        return (
            Provenance(
                source="rollup_sqlite",
                message="Rollup DB contains no dyad_status_rollup rows.",
            ),
            path,
        )
    build_id, vintage, computed_at = prov
    return (
        Provenance(
            build_id=build_id,
            vintage=vintage,
            computed_at=computed_at,
            source="rollup_sqlite",
        ),
        path,
    )


def _version_gate(
    provenance: Provenance, expected: str | None
) -> tuple[list[VersionWarning], JSONResponse | None]:
    """Apply the version check. Returns (warnings, error_response).

    error_response is None when the request may proceed.
    """
    ok, detail = check_version(expected, config.version_mode())
    if ok:
        warnings = [VersionWarning(**detail)] if detail else []
        return warnings, None
    error = JSONResponse(
        status_code=409,
        content=ApiResponse(
            api_version=API_VERSION,
            provenance=provenance,
            warnings=[],
            data=None,
            error={
                "code": "version_mismatch",
                "message": detail["message"],
                "expected": detail["expected"],
                "served": detail["served"],
            },
        ).model_dump(),
    )
    return [], error


def _db_required(provenance: Provenance, path: object) -> JSONResponse | None:
    """Return a 503 envelope when no usable DB exists, else None."""
    if path is not None:
        return None
    return JSONResponse(
        status_code=503,
        content=ApiResponse(
            api_version=API_VERSION,
            provenance=provenance,
            warnings=[],
            data=None,
            error={
                "code": "db_not_configured",
                "message": provenance.message
                or "No rollup DB configured.",
            },
        ).model_dump(),
    )


def _ok(
    provenance: Provenance, warnings: list[VersionWarning], data: object
) -> dict:
    return ApiResponse(
        api_version=API_VERSION,
        provenance=provenance,
        warnings=warnings,
        data=data,
    ).model_dump()


#: Payload-contract marker for the experimental endpoints. It is
#: deliberately not "1": an ``/exp/`` response must never be
#: mistaken for a v1 response, and the ``/exp/`` routes are exempt
#: from the v1 version gate (they take no ``api_version`` parameter).
EXP_API_VERSION = "exp"

#: Provenance message on EVERY ``/exp/`` response, success or
#: error. The specific failure reason, when there is one, lives in
#: ``error.message``; this message stays exactly this string so a
#: client can always tell experimental data from v1 data.
EXP_MESSAGE = "EXPERIMENTAL — not part of the v1 contract; never promote."


def _exp_provenance() -> tuple[Provenance, object, object]:
    """Load the ``/exp/`` provenance stamp and usable DB paths.

    Returns (provenance, rollup_path_or_None, dda_path_or_None).
    The stamp's build/vintage/computed_at come from the rollup when
    one is configured and readable (the rollup is the stamped
    product); the DDA sidecar carries no such stamp. ``source``
    names the DBs actually available. A missing or unreadable DB is
    reported by the caller as a 503, not raised here. The message is
    always exactly ``EXP_MESSAGE``: the ``/exp/`` fence must survive
    even the error responses.
    """
    rollup = config.rollup_path()
    dda = config.dda_path()
    usable_rollup: object = None
    build_id = vintage = computed_at = None
    if rollup is not None and rollup.exists():
        try:
            prov = store.fetch_provenance(rollup)  # type: ignore[arg-type]
        except sqlite3.Error:
            prov = None
        # A readable-but-empty DB still counts as configured: the
        # endpoints then return empty rows, as v1 does.
        usable_rollup = rollup
        if prov is not None:
            build_id, vintage, computed_at = prov
    usable_dda: object = None
    if dda is not None and dda.exists():
        usable_dda = dda
    if usable_rollup is not None and usable_dda is not None:
        source = "rollup_sqlite+dda_sidecar"
    elif usable_rollup is not None:
        source = "rollup_sqlite"
    elif usable_dda is not None:
        source = "dda_sidecar"
    else:
        source = None
    return (
        Provenance(
            build_id=build_id,
            vintage=vintage,
            computed_at=computed_at,
            source=source,
            message=EXP_MESSAGE,
        ),
        usable_rollup,
        usable_dda,
    )


def _exp_ok(provenance: Provenance, data: object) -> dict:
    return ApiResponse(
        api_version=EXP_API_VERSION,
        provenance=provenance,
        warnings=[],
        data=data,
    ).model_dump()


def _exp_fail(
    provenance: Provenance, status: int, code: str, message: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ApiResponse(
            api_version=EXP_API_VERSION,
            provenance=provenance,
            warnings=[],
            data=None,
            error={"code": code, "message": message},
        ).model_dump(),
    )


def _exp_db_required(
    provenance: Provenance, path: object, env_var: str
) -> JSONResponse | None:
    """Return a 503 ``/exp/`` envelope when a DB is missing."""
    if path is not None:
        return None
    return _exp_fail(
        provenance,
        503,
        "db_not_configured",
        f"No {env_var} DB configured or the file was not found: "
        f"set {env_var} to the data file path.",
    )


def _exp_read(
    provenance: Provenance, fn, *args
) -> tuple[list[dict] | dict | None, JSONResponse | None]:
    """Run an exp_store fetch, mapping sqlite errors to 503."""
    try:
        return fn(*args), None
    except sqlite3.Error as exc:
        return None, _exp_fail(
            provenance, 503, "db_error", f"Could not read DB: {exc}"
        )


def _fail(
    provenance: Provenance,
    warnings: list[VersionWarning],
    status: int,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ApiResponse(
            api_version=API_VERSION,
            provenance=provenance,
            warnings=warnings,
            data=None,
            error={"code": code, "message": message},
        ).model_dump(),
    )


def create_app() -> FastAPI:
    """Application factory (keeps tests able to build a fresh app)."""
    app = FastAPI(
        title="schwarzschild-backend",
        description=(
            "Thin serving layer over precomputed schwarzschild "
            "analytics products (rollup SQLite). See README for the "
            "payload contract version and the endpoints."
        ),
    )

    @app.get("/health")
    def health() -> dict:
        provenance, path = _provenance()
        return _ok(
            provenance,
            [],
            {
                "status": "ok",
                "db_configured": path is not None,
            },
        )

    @app.get("/dyad-status", response_model=None)
    def dyad_status(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _provenance()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        missing = _db_required(provenance, path)
        if missing is not None:
            return missing
        try:
            row = store.fetch_dyad(path, iso1, iso2, as_of)  # type: ignore[arg-type]
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        if row is None:
            return _fail(
                provenance, warnings, 404, "dyad_not_found",
                f"No rollup row for {iso1}_{iso2}"
                + (f" at {as_of}" if as_of else ""),
            )
        return _ok(provenance, warnings, row)

    @app.get("/watchlist", response_model=None)
    def watchlist(
        as_of: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        labels: list[str] | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _provenance()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        # Accept both repeated params and a single comma-separated one.
        wanted: list[str] | None = None
        if labels:
            wanted = []
            for item in labels:
                wanted.extend(
                    part.strip() for part in item.split(",") if part.strip()
                )
            unknown = sorted(set(wanted) - set(LABEL_PRIORITY))
            if unknown:
                return _fail(
                    provenance, warnings, 400, "unknown_label",
                    "Unknown label(s): " + ", ".join(unknown)
                    + ". Valid labels: " + ", ".join(LABEL_PRIORITY) + ".",
                )
        missing = _db_required(provenance, path)
        if missing is not None:
            return missing
        try:
            effective, rows = store.fetch_watchlist(
                path, as_of, wanted, limit  # type: ignore[arg-type]
            )
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        return _ok(
            provenance,
            warnings,
            {"as_of": effective, "count": len(rows), "rows": rows},
        )

    @app.get("/pcs-series", response_model=None)
    def pcs_series(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _provenance()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        missing = _db_required(provenance, path)
        if missing is not None:
            return missing
        try:
            rows = store.fetch_history(path, iso1, iso2)  # type: ignore[arg-type]
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        if not rows:
            return _fail(
                provenance, warnings, 404, "dyad_not_found",
                f"No rollup history for {iso1}_{iso2}.",
            )
        return _ok(
            provenance, warnings, store.build_series(iso1, iso2, rows)
        )

    @app.get("/census", response_model=None)
    def census(
        as_of: str | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _provenance()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        missing = _db_required(provenance, path)
        if missing is not None:
            return missing
        try:
            effective, counts, total = store.fetch_census(
                path, as_of  # type: ignore[arg-type]
            )
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        return _ok(
            provenance,
            warnings,
            {"as_of": effective, "total": total, "counts": counts},
        )

    @app.get("/census-series", response_model=None)
    def census_series(
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _provenance()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        missing = _db_required(provenance, path)
        if missing is not None:
            return missing
        try:
            rows = store.fetch_census_series(path)  # type: ignore[arg-type]
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        return _ok(
            provenance,
            warnings,
            {
                "dates": [r["as_of"] for r in rows],
                "rows": rows,
            },
        )

    # ---------------------------------------------------------- /exp/
    # Experimental raw-data endpoints. Same envelope shape as v1
    # but api_version "exp", NO version gate, and the fence message
    # on every response. Unknown dyads / empty as-ofs return empty
    # row lists, not errors: raw delivery has no "not found" for a
    # dyad that simply has no rows. No endpoint here computes
    # anything: see app/exp_store.py.

    @app.get("/exp/dda-records", response_model=None)
    def exp_dda_records(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
    ) -> JSONResponse | dict:
        provenance, _rollup, dda = _exp_provenance()
        missing = _exp_db_required(provenance, dda, "DDA_SQLITE")
        if missing is not None:
            return missing
        rows, error = _exp_read(
            provenance, exp_store.fetch_dda_records, dda, iso1, iso2
        )
        if error is not None:
            return error
        return _exp_ok(
            provenance, {"iso1": iso1, "iso2": iso2, "rows": rows}
        )

    @app.get("/exp/corpus", response_model=None)
    def exp_corpus(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
    ) -> JSONResponse | dict:
        provenance, _rollup, dda = _exp_provenance()
        missing = _exp_db_required(provenance, dda, "DDA_SQLITE")
        if missing is not None:
            return missing
        rows, error = _exp_read(
            provenance, exp_store.fetch_corpus, dda, iso1, iso2
        )
        if error is not None:
            return error
        return _exp_ok(
            provenance, {"iso1": iso1, "iso2": iso2, "rows": rows}
        )

    @app.get("/exp/rollup-at", response_model=None)
    def exp_rollup_at(
        as_of: str = Query(..., min_length=1),
    ) -> JSONResponse | dict:
        provenance, rollup, _dda = _exp_provenance()
        missing = _exp_db_required(provenance, rollup, "ROLLUP_SQLITE")
        if missing is not None:
            return missing
        rows, error = _exp_read(
            provenance, exp_store.fetch_rollup_at, rollup, as_of
        )
        if error is not None:
            return error
        return _exp_ok(provenance, {"as_of": as_of, "rows": rows})

    @app.get("/exp/rollup-dyad", response_model=None)
    def exp_rollup_dyad(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
    ) -> JSONResponse | dict:
        provenance, rollup, _dda = _exp_provenance()
        missing = _exp_db_required(provenance, rollup, "ROLLUP_SQLITE")
        if missing is not None:
            return missing
        rows, error = _exp_read(
            provenance, exp_store.fetch_rollup_dyad, rollup, iso1, iso2
        )
        if error is not None:
            return error
        return _exp_ok(
            provenance, {"iso1": iso1, "iso2": iso2, "rows": rows}
        )

    @app.get("/exp/census-series", response_model=None)
    def exp_census_series() -> JSONResponse | dict:
        provenance, rollup, _dda = _exp_provenance()
        missing = _exp_db_required(provenance, rollup, "ROLLUP_SQLITE")
        if missing is not None:
            return missing
        rows, error = _exp_read(
            provenance, store.fetch_census_series, rollup
        )
        if error is not None:
            return error
        return _exp_ok(
            provenance,
            {
                "dates": [r["as_of"] for r in rows],  # type: ignore[union-attr]
                "rows": rows,
            },
        )

    @app.get("/exp/notable", response_model=None)
    def exp_notable(
        limit: int = Query(default=60, ge=1, le=1000),
    ) -> JSONResponse | dict:
        provenance, rollup, _dda = _exp_provenance()
        missing = _exp_db_required(provenance, rollup, "ROLLUP_SQLITE")
        if missing is not None:
            return missing
        rows, error = _exp_read(
            provenance, exp_store.fetch_notable, rollup, limit
        )
        if error is not None:
            return error
        return _exp_ok(provenance, {"rows": rows})

    @app.get("/exp/meta", response_model=None)
    def exp_meta() -> JSONResponse | dict:
        provenance, rollup, dda = _exp_provenance()
        missing = _exp_db_required(provenance, rollup, "ROLLUP_SQLITE")
        if missing is not None:
            return missing
        missing = _exp_db_required(provenance, dda, "DDA_SQLITE")
        if missing is not None:
            return missing
        data, error = _exp_read(
            provenance, exp_store.fetch_dda_stats, dda
        )
        if error is not None:
            return error
        rollup_meta, error = _exp_read(
            provenance, exp_store.fetch_rollup_meta, rollup
        )
        if error is not None:
            return error
        data.update(rollup_meta)  # type: ignore[union-attr]
        return _exp_ok(provenance, data)

    return app


app = create_app()
