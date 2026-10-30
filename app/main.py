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

DEV ONLY Look-Closer endpoints (``/lc/``, see ``app/lc_store.py``
and ``docs/LC_DEV_NOTES.md``): ``GET /lc/lc-01`` (rollup history
+ delta), ``GET /lc/lc-02`` (evidence/stability/data rows),
``GET /lc/lc-05`` (top-3 move chips), ``GET /lc/lc-07``
(DDA-sidecar volume regime). All are version-gate exempt and
carry the provenance message "LC DEV ONLY — not part of the v1
contract." They are not part of the v1 contract.

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

    # ------------------------------------------------------------------
    # DEV ONLY — DDA sidecar endpoints (dev/dda-sidecar branch).
    # NOT part of the v1 contract: do not build frontend features on
    # these paths, do not document them as contract, and do not
    # promote them without the formalization step (VET_DDA_NEEDS.md
    # Part C). They exist to measure the spike shapes (Part D):
    # query plans, latencies, payload sizes, index requirements.
    # They read the DDA sidecar (DDA_SQLITE) plus the CAMEO sets
    # (DDA_SETS_JSON); either unset means 503, same discipline as
    # the v1 data endpoints. Daily set shares are computed here
    # (see dev_dda_store) — a dev decision, flagged for
    # formalization, not a v1 rule.
    # ------------------------------------------------------------------
    from . import dev_dda_store

    def _dev_provenance() -> tuple[Provenance, object, object, object]:
        """Sidecar + sets + simplexes paths, or a 503-grade provenance.

        Returns (provenance, dda_path_or_None, sets_path_or_None,
        simplexes_path_or_None). The simplexes file is read as a
        CODE LIST ONLY (mapped set); its p-values are not read.
        """
        dda = config.dda_path()
        sets_p = config.dda_sets_path()
        simplexes_p = config.dda_simplexes_path()
        if dda is None or sets_p is None or simplexes_p is None:
            missing = [
                n
                for n, p in (
                    ("DDA_SQLITE", dda),
                    ("DDA_SETS_JSON", sets_p),
                    ("DDA_SIMPLEXES_JSON", simplexes_p),
                )
                if p is None
            ]
            return (
                Provenance(
                    source="dda_sidecar",
                    message="DEV ONLY — not part of the v1 contract. "
                    f"Missing configuration: {', '.join(missing)}.",
                ),
                None,
                None,
                None,
            )
        if not dda.exists() or not sets_p.exists() or not simplexes_p.exists():
            return (
                Provenance(
                    source="dda_sidecar",
                    message="DEV ONLY — not part of the v1 contract. "
                    "Configured DDA sidecar, sets, or simplexes file "
                    "not found.",
                ),
                None,
                None,
                None,
            )
        return (
            Provenance(
                source="dda_sidecar",
                message="DEV ONLY — not part of the v1 contract.",
            ),
            dda,
            sets_p,
            simplexes_p,
        )

    @app.get("/dev/dda-daily", response_model=None)
    def dev_dda_daily(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        window: int = Query(default=60, ge=1, le=365),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, dda, sets_p, simplexes_p = _dev_provenance()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        missing = _db_required(provenance, dda)
        if missing is not None:
            return missing
        try:
            conn = dev_dda_store.connect(dda)  # type: ignore[arg-type]
            try:
                sets = dev_dda_store.load_sets(sets_p)  # type: ignore[arg-type]
                mapped = dev_dda_store.load_mapped(simplexes_p)  # type: ignore[arg-type]
                meta = dev_dda_store.fetch_sidecar_meta(conn)
                header, rows = dev_dda_store.fetch_daily(
                    conn, sets, mapped, iso1, iso2, as_of, window
                )
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read DDA sidecar: {exc}",
            )
        return _ok(
            provenance,
            warnings,
            {**header, "sidecar": meta, "rows": rows},
        )

    @app.get("/dev/dda-codes", response_model=None)
    def dev_dda_codes(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        window: int = Query(default=60, ge=1, le=365),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, dda, sets_p, simplexes_p = _dev_provenance()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        missing = _db_required(provenance, dda)
        if missing is not None:
            return missing
        try:
            conn = dev_dda_store.connect(dda)  # type: ignore[arg-type]
            try:
                sets = dev_dda_store.load_sets(sets_p)  # type: ignore[arg-type]
                mapped = dev_dda_store.load_mapped(simplexes_p)  # type: ignore[arg-type]
                meta = dev_dda_store.fetch_sidecar_meta(conn)
                header, rows = dev_dda_store.fetch_codes(
                    conn, sets, mapped, iso1, iso2, as_of, window
                )
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read DDA sidecar: {exc}",
            )
        return _ok(
            provenance,
            warnings,
            {**header, "sidecar": meta, "rows": rows},
        )

    @app.get("/dev/dda-joint", response_model=None)
    def dev_dda_joint(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        window: int = Query(default=60, ge=1, le=365),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        # DEV ONLY: daily joint compositions (simplex extension,
        # Dan 2026-10-28). Reads simplex VALUES via
        # DDA_SIMPLEXES_JSON — the first endpoint to do so; the
        # daily/codes endpoints use that file as a code list only.
        provenance, dda, sets_p, simplexes_p = _dev_provenance()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        missing = _db_required(provenance, dda)
        if missing is not None:
            return missing
        try:
            conn = dev_dda_store.connect(dda)  # type: ignore[arg-type]
            try:
                simplexes = dev_dda_store.load_simplexes(simplexes_p)  # type: ignore[arg-type]
                meta = dev_dda_store.fetch_sidecar_meta(conn)
                header, rows = dev_dda_store.fetch_joint(
                    conn, simplexes, iso1, iso2, as_of, window
                )
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read DDA sidecar: {exc}",
            )
        return _ok(
            provenance,
            warnings,
            {**header, "sidecar": meta, "rows": rows},
        )

    # ------------------------------------------------------------------
    # DEV ONLY — Look-Closer endpoints (Waves 1-2, /lc/ prefix).
    # NOT part of the v1 contract: do not build frontend features
    # on these paths as contract, and do not promote them without
    # the formalization step. Every envelope's provenance message
    # is exactly "LC DEV ONLY — not part of the v1 contract."
    # Version gate: /lc/ endpoints apply the SAME gate as /dev/
    # (Dan, 2026-10-30, flag 1: flip to match /dev/ for now — the
    # original brief's premise that /dev/ is exempt was wrong, and
    # versioning questions are deferred until the fast
    # feedback/dev cycle ends). A mismatched api_version 409s in
    # strict mode and warns in warn mode, exactly as on /dev/.
    # LC-07 effective as-of (Dan, 2026-10-30, flag 3): when the
    # dyad has a rollup row at or before the requested as-of, the
    # rollup's latest such as-of is the effective as-of for LC-07
    # too, so all four figures describe the same day. When the
    # dyad has no rollup row (DDA-only dev fixture, or rollup
    # unavailable), LC-07 falls back to the /dev/ DDA pattern
    # (requested verbatim, or latest DDA date when omitted) —
    # the other figures are empty for such a dyad in any case.
    # LC-01/02/05 read the rollup (ROLLUP_SQLITE); LC-07 reads
    # the DDA sidecar (DDA_SQLITE) plus the mapped code list
    # (DDA_SIMPLEXES_JSON). Wave 2: LC-06 reads the DDA sidecar
    # plus the simplex list (DDA_SIMPLEXES_JSON) with the same
    # rollup-effective-as-of fallback as LC-07; LC-08 and
    # LC-09 read the rollup only. Missing config means 503, same
    # discipline as the v1 data endpoints. Unknown dyads return
    # 200 with empty rows/nulls, never 404.
    # ------------------------------------------------------------------
    from . import lc_store

    def _lc_rollup() -> tuple[Provenance, object]:
        """Rollup path + LC provenance (message exactly LC_DEV)."""
        path = config.rollup_path()
        if path is None or not path.exists():
            return (
                Provenance(
                    source="rollup_sqlite",
                    message=lc_store.LC_PROVENANCE_MESSAGE,
                ),
                None,
            )
        try:
            prov = store.fetch_provenance(path)
        except sqlite3.Error:
            return (
                Provenance(
                    source="rollup_sqlite",
                    message=lc_store.LC_PROVENANCE_MESSAGE,
                ),
                None,
            )
        if prov is None:
            return (
                Provenance(
                    source="rollup_sqlite",
                    message=lc_store.LC_PROVENANCE_MESSAGE,
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
                message=lc_store.LC_PROVENANCE_MESSAGE,
            ),
            path,
        )

    def _lc_dda() -> tuple[Provenance, object, object]:
        """Sidecar + mapped-list paths + LC provenance.

        LC-07 needs DDA_SQLITE and DDA_SIMPLEXES_JSON only (the
        mapped code list); it does not need DDA_SETS_JSON.
        """
        dda = config.dda_path()
        simplexes_p = config.dda_simplexes_path()
        if dda is None or simplexes_p is None:
            return (
                Provenance(
                    source="dda_sidecar",
                    message=lc_store.LC_PROVENANCE_MESSAGE,
                ),
                None,
                None,
            )
        if not dda.exists() or not simplexes_p.exists():
            return (
                Provenance(
                    source="dda_sidecar",
                    message=lc_store.LC_PROVENANCE_MESSAGE,
                ),
                None,
                None,
            )
        return (
            Provenance(
                source="dda_sidecar",
                message=lc_store.LC_PROVENANCE_MESSAGE,
            ),
            dda,
            simplexes_p,
        )

    def _lc_missing(
        provenance: Provenance, detail: str
    ) -> JSONResponse:
        """503 envelope: provenance message stays exactly LC_DEV;
        the missing-config detail lives in the error message."""
        return JSONResponse(
            status_code=503,
            content=ApiResponse(
                api_version=API_VERSION,
                provenance=provenance,
                warnings=[],
                data=None,
                error={"code": "db_not_configured", "message": detail},
            ).model_dump(),
        )

    @app.get("/lc/lc-01", response_model=None)
    def lc_01(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _lc_rollup()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        if path is None:
            return _lc_missing(
                provenance,
                "LC DEV ONLY — no usable rollup DB: set ROLLUP_SQLITE "
                "to a rollup_poc_base.sqlite path.",
            )
        try:
            conn = lc_store.connect(path)  # type: ignore[arg-type]
            try:
                data = lc_store.fetch_lc01(conn, iso1, iso2, as_of)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        return _ok(provenance, warnings, data)

    @app.get("/lc/lc-02", response_model=None)
    def lc_02(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _lc_rollup()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        if path is None:
            return _lc_missing(
                provenance,
                "LC DEV ONLY — no usable rollup DB: set ROLLUP_SQLITE "
                "to a rollup_poc_base.sqlite path.",
            )
        try:
            conn = lc_store.connect(path)  # type: ignore[arg-type]
            try:
                data = lc_store.fetch_lc02(conn, iso1, iso2, as_of)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        return _ok(provenance, warnings, data)

    @app.get("/lc/lc-05", response_model=None)
    def lc_05(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _lc_rollup()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        if path is None:
            return _lc_missing(
                provenance,
                "LC DEV ONLY — no usable rollup DB: set ROLLUP_SQLITE "
                "to a rollup_poc_base.sqlite path.",
            )
        try:
            conn = lc_store.connect(path)  # type: ignore[arg-type]
            try:
                data = lc_store.fetch_lc05(conn, iso1, iso2, as_of)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        return _ok(provenance, warnings, data)

    @app.get("/lc/lc-07", response_model=None)
    def lc_07(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, dda, simplexes_p = _lc_dda()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        if dda is None or simplexes_p is None:
            return _lc_missing(
                provenance,
                "LC DEV ONLY — no usable DDA sidecar: set DDA_SQLITE "
                "and DDA_SIMPLEXES_JSON (mapped code list).",
            )
        # Effective as-of consistency (flag 3): if the dyad has a
        # rollup row at or before the requested as-of, that
        # rollup date is the effective as-of for LC-07 too.
        # Otherwise fall back to the /dev/ DDA pattern (requested
        # verbatim, or latest DDA date when omitted).
        effective = None
        rollup_path = config.rollup_path()
        if rollup_path is not None and rollup_path.exists():
            try:
                rconn = lc_store.connect(rollup_path)  # type: ignore[arg-type]
                try:
                    effective = lc_store.resolve_rollup_as_of(
                        rconn, iso1, iso2, as_of)
                finally:
                    rconn.close()
            except sqlite3.Error:
                effective = None
        call_as_of = effective if effective is not None else as_of
        try:
            conn = lc_store.connect(dda)  # type: ignore[arg-type]
            try:
                mapped = dev_dda_store.load_mapped(simplexes_p)  # type: ignore[arg-type]
                meta = dev_dda_store.fetch_sidecar_meta(conn)
                data = lc_store.fetch_lc07(
                    conn, mapped, iso1, iso2, call_as_of)
                data["sidecar"] = meta
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read DDA sidecar: {exc}",
            )
        return _ok(provenance, warnings, data)

    @app.get("/lc/lc-06", response_model=None)
    def lc_06(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, dda, simplexes_p = _lc_dda()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        if dda is None or simplexes_p is None:
            return _lc_missing(
                provenance,
                "LC DEV ONLY — no usable DDA sidecar: set DDA_SQLITE "
                "and DDA_SIMPLEXES_JSON (simplex list).",
            )
        # Same effective-as-of discipline as LC-07 (flag 3).
        effective = None
        rollup_path = config.rollup_path()
        if rollup_path is not None and rollup_path.exists():
            try:
                rconn = lc_store.connect(rollup_path)  # type: ignore[arg-type]
                try:
                    effective = lc_store.resolve_rollup_as_of(
                        rconn, iso1, iso2, as_of)
                finally:
                    rconn.close()
            except sqlite3.Error:
                effective = None
        call_as_of = effective if effective is not None else as_of
        try:
            conn = lc_store.connect(dda)  # type: ignore[arg-type]
            try:
                simplexes = dev_dda_store.load_simplexes(simplexes_p)  # type: ignore[arg-type]
                data = lc_store.fetch_lc06(
                    conn, simplexes, iso1, iso2, call_as_of)
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read DDA sidecar: {exc}",
            )
        return _ok(provenance, warnings, data)

    @app.get("/lc/lc-08", response_model=None)
    def lc_08(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _lc_rollup()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        if path is None:
            return _lc_missing(
                provenance,
                "LC DEV ONLY — no usable rollup DB: set ROLLUP_SQLITE "
                "to a rollup_poc_base.sqlite path.",
            )
        try:
            conn = lc_store.connect(path)  # type: ignore[arg-type]
            try:
                data = lc_store.fetch_lc08(conn, iso1, iso2, as_of)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        return _ok(provenance, warnings, data)

    @app.get("/lc/lc-09", response_model=None)
    def lc_09(
        iso1: str = Query(..., min_length=1),
        iso2: str = Query(..., min_length=1),
        as_of: str | None = Query(default=None),
        api_version: str | None = Query(
            default=None,
            description="Expected payload contract version.",
        ),
    ) -> JSONResponse | dict:
        provenance, path = _lc_rollup()
        warnings, error = _version_gate(provenance, api_version)
        if error is not None:
            return error
        if path is None:
            return _lc_missing(
                provenance,
                "LC DEV ONLY — no usable rollup DB: set ROLLUP_SQLITE "
                "to a rollup_poc_base.sqlite path.",
            )
        try:
            conn = lc_store.connect(path)  # type: ignore[arg-type]
            try:
                data = lc_store.fetch_lc09(conn, iso1, iso2, as_of)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return _fail(
                provenance, warnings, 503, "db_error",
                f"Could not read rollup DB: {exc}",
            )
        return _ok(provenance, warnings, data)

    return app


app = create_app()
