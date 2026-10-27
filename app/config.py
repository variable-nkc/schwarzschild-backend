"""Configuration for schwarzschild-backend v1.

Environment variables are read at request time, not import time, so a
test can point the app at a temporary fixture database by monkeypatching
the environment, and so an operator can change the data source without
re-importing the app.

Environment variables
---------------------
ROLLUP_SQLITE
    Full path to a ``rollup_poc_base.sqlite`` file (table
    ``dyad_status_rollup``). If unset, the app still starts and
    ``GET /health`` still returns ok, but the data endpoints return
    503 with a clear message. There is deliberately no hardcoded
    default path: any default that only works on one machine would
    silently serve the wrong database (or none) everywhere else.
    The README shows a placeholder path as an example only.

API_VERSION_MODE
    ``strict`` (default) or ``warn``. See ``app/versioning.py``.

The payload contract version lives in ``API_VERSION`` below. It
versions the *payload shape*, not code releases: it is bumped only
when a response shape changes in a breaking way (a field removed,
renamed, or retyped). See the README, "Versioning".
"""

import os
from pathlib import Path

# NOTE(capability): os.getenv reads one environment variable, returning
# the fallback when it is unset — the standard way to make a service
# configurable without a config file.
API_VERSION = "1"

#: Analyst sets, in the order the rollup stores them. Used to build the
#: per-set blocks in the PCS-series response.
SETS = (
    "escalatory",
    "stability",
    "instability",
    "deescalatory",
    "critical_incidents",
)

#: Rollup columns stored as SQLite INTEGER 0/1 that are booleans in the
#: JSON API. Everything else is returned exactly as stored.
BOOLEAN_COLUMNS = frozenset(
    {
        "flag_insufficient",
        "flag_drifting",
        "flag_hot",
        "flag_atypical",
        "gap_flag",
        "low_volume",
        "stability_stable",
        *(f"above_bar_{s}" for s in SETS),
    }
)

#: Labels in hot-first label-priority order (see README, "Labels").
#: This is the *label assignment* priority: when several flags fire,
#: the label is the first flag in this order. It is also the order
#: the v1 watchlist listing uses as a first pass.
LABEL_PRIORITY = (
    "insufficient",
    "hot",
    "drifting",
    "atypical",
    "steady",
)


def rollup_path() -> Path | None:
    """Return the configured rollup path, or None when unset."""
    raw = os.getenv("ROLLUP_SQLITE", "").strip()
    return Path(raw) if raw else None


def version_mode() -> str:
    """Return 'strict' or 'warn', defaulting to strict (fail closed).

    An unrecognised value also falls back to strict rather than
    silently weakening the version check.
    """
    mode = os.getenv("API_VERSION_MODE", "strict").strip().lower()
    return mode if mode in ("strict", "warn") else "strict"
