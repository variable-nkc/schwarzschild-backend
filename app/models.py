"""Pydantic models — the request/response boundary only.

These models shape what leaves and enters the API. They do not do
any statistics, and internal code (``app/store.py``) works in plain
dicts and sqlite rows, never in these models. That split is
deliberate: pydantic at the boundary, plain data inside.
"""

from typing import Any

from pydantic import BaseModel


class Provenance(BaseModel):
    """Provenance stamp carried on every response.

    build_id, vintage, and computed_at come from the rollup database
    when one is configured. When no database is configured (or it
    cannot be read), they are null and ``message`` says so plainly
    instead of the response pretending to be stamped.
    """

    build_id: str | None = None
    vintage: int | None = None
    computed_at: str | None = None
    source: str | None = None
    message: str | None = None


class VersionWarning(BaseModel):
    """A version_mismatch warning (warn mode) or error detail."""

    code: str
    message: str
    expected: str | None = None
    served: str


class ApiResponse(BaseModel):
    """The envelope on every response, success or error.

    ``data`` holds the endpoint payload (or null on error).
    ``error`` is null on success and ``{"code": ..., "message": ...}``
    on failure. ``warnings`` carries version_mismatch warnings in
    warn mode and is empty otherwise.
    """

    api_version: str
    provenance: Provenance
    warnings: list[VersionWarning] = []
    data: Any = None
    error: dict | None = None
