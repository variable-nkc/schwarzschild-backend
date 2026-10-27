"""Payload-contract version checking.

``api_version`` versions the *payload shape*, not code releases.
It is bumped only when a response shape changes in a breaking way
(a field removed, renamed, or retyped). A frontend-only change or a
backend change that leaves payload shapes untouched does not bump
it, so neither side has to be released in lockstep with the other
for ordinary changes.

Modes (``API_VERSION_MODE`` env var, default ``strict``):

- ``strict``: any mismatch between the frontend's expected version
  and the served version fails outright with an explicit error
  naming expected vs served.
- ``warn``: the request continues, but the response carries a
  prominent ``version_mismatch`` warning object. Warn mode is a
  local-development tool; the integration test runs in strict mode,
  because a test that passes with a version warning is not a sync
  check. In warn mode the warning must still be surfaced to the
  user (a banner), not buried in a log.

When no expected version is sent, no check is performed: a client
that does not declare an expectation cannot be in conflict with
the server.
"""

from .config import API_VERSION


def check_version(expected: str | None, mode: str) -> tuple[bool, dict | None]:
    """Check an expected version against the served version.

    Returns (ok, warning_or_error). When ok is False, the caller
    must fail the request and surface the returned object as the
    error detail. When ok is True and the object is not None, the
    caller must include it in the response warnings.
    """
    if expected is None or expected == API_VERSION:
        return True, None
    detail = {
        "code": "version_mismatch",
        "message": (
            f"expected api_version {expected}, "
            f"served api_version {API_VERSION}"
        ),
        "expected": expected,
        "served": API_VERSION,
    }
    if mode == "warn":
        return True, detail
    return False, detail
