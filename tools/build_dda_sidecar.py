"""Build the DDA sidecar SQLite (dev branch tool).

The sidecar is JUST the DDA with appropriate indexing: one table,
copied verbatim from the SQLite mirror's DDA table, plus a
dyad-scoped serving index. It is NOT the mirror itself (the
mirror is the full verbatim poc_base, 35 tables); it is a new,
narrow, DDA-derived delivery file that sits alongside the rollup
SQLite the backend serves.

Source of truth stays the mirror: the sidecar is regenerated
from the mirror whenever DDA extends, never hand-edited.

Usage (from the backend repo root):
    python tools/build_dda_sidecar.py [--mirror PATH] [--out PATH] [--force]

Defaults point at the VM mirror and the VM sidecar location.
Refuses to overwrite an existing sidecar unless --force is given.
Verifies row count + date range + index before reporting success.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

MIRROR_DEFAULT = os.path.expanduser(
    "~/workspace/postgres/poc_base.sqlite")
OUT_DEFAULT = os.path.expanduser(
    "~/workspace/user/files/dda_sidecar/dda_poc_base.sqlite")
DDA_TABLE = "analytics_dyad_date_actions"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mirror", default=MIRROR_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        print(f"refusing to overwrite existing {args.out} (pass --force)",
              file=sys.stderr)
        return 1
    if not os.path.exists(args.mirror):
        print(f"mirror not found: {args.mirror}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if os.path.exists(args.out):
        os.remove(args.out)

    # URI connections so the mirror attaches read-only (the builder
    # must never write to the archive it reads from).
    con = sqlite3.connect(f"file:{args.out}?mode=rwc", uri=True)
    try:
        con.execute(f"ATTACH DATABASE 'file:{args.mirror}?mode=ro' AS mirror")
        # Verbatim copy of the DDA table (all columns, including the
        # mirror-internal id) — "just the DDA", no projection.
        con.execute(
            "CREATE TABLE dda ("
            "  id INTEGER,"
            "  iso1 TEXT,"
            "  iso2 TEXT,"
            "  obs_date DATE,"
            "  cameo_code TEXT,"
            "  obs_count INTEGER"
            ")"
        )
        con.execute(
            f"INSERT INTO dda SELECT id, iso1, iso2, obs_date, cameo_code,"
            f" obs_count FROM mirror.{DDA_TABLE}"
        )
        # Serving index, dyad first (reads are dyad-scoped).
        con.execute(
            "CREATE INDEX idx_dda_dyad_date ON dda (iso1, iso2, obs_date)"
        )
        src = con.execute(
            "SELECT COUNT(*), MIN(obs_date), MAX(obs_date)"
            f" FROM mirror.{DDA_TABLE}"
        ).fetchone()
        try:
            dump = con.execute(
                "SELECT value FROM mirror._mirror_meta"
                " WHERE key = 'source_dump'"
            ).fetchone()[0]
        except Exception:
            dump = "unknown"
        # Scoped to main: bare ANALYZE would try to write stats
        # into the read-only mirror as well as the sidecar.
        con.execute("ANALYZE main")
        con.execute(
            "CREATE TABLE _sidecar_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        meta = {
            "source_mirror": args.mirror,
            "source_dump": dump,
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
            "dda_rows": str(src[0]),
            "obs_min": src[1],
            "obs_max": src[2],
            "indexes": "idx_dda_dyad_date (iso1, iso2, obs_date)",
        }
        con.executemany(
            "INSERT INTO _sidecar_meta VALUES (?, ?)", meta.items()
        )
        con.commit()
        # Verify against the source before reporting success.
        got = con.execute(
            "SELECT COUNT(*), MIN(obs_date), MAX(obs_date) FROM dda"
        ).fetchone()
        idx = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index'"
            " AND tbl_name = 'dda'"
        ).fetchone()[0]
        assert tuple(got) == tuple(src), f"mismatch: {got} != {src}"
        assert idx >= 1, "serving index missing"
    finally:
        con.close()

    size = os.path.getsize(args.out)
    print(f"sidecar: {args.out}")
    print(f"  rows: {src[0]}, dates: {src[1]} .. {src[2]}, "
          f"size: {size} bytes, indexes: {idx}")
    print(f"  source: {args.mirror} (dump {dump})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
