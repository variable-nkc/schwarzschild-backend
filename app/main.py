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
"""

import sqlite3

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import config, store
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

    return app


app = create_app()
