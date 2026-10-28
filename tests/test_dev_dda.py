"""Tests for the DEV DDA endpoints (dev/dda-sidecar branch only).

Same discipline as test_api.py: every test builds its own small
SQLite fixture in a temporary directory and monkeypatches the
environment per test. No test reads a real product. These tests
cover the dev endpoints' arithmetic and gating only — they do
not bless the shapes as contract (formalization does that).
"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def dev_env(tmp_path, monkeypatch):
    """Tiny sidecar (3 days, 1 gap day) + tiny sets JSON."""
    db = tmp_path / "dda.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE dda (id INTEGER, iso1 TEXT, iso2 TEXT,"
        " obs_date DATE, cameo_code TEXT, obs_count INTEGER)"
    )
    con.execute(
        "CREATE INDEX idx_dda_dyad_date ON dda (iso1, iso2, obs_date)"
    )
    rows = [
        # 2026-09-28: esc codes 010 (4) + 020 (6) = 10; stab 030 (10).
        (1, "AAA", "BBB", "2026-09-28", "010", 4),
        (2, "AAA", "BBB", "2026-09-28", "020", 6),
        (3, "AAA", "BBB", "2026-09-28", "030", 10),
        # 2026-09-29: gap day (no rows at all).
        # 2026-09-30: esc 010 (9); unmapped 999 (3, excluded).
        (4, "AAA", "BBB", "2026-09-30", "010", 9),
        (5, "AAA", "BBB", "2026-09-30", "999", 3),
    ]
    con.executemany("INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE _sidecar_meta (key TEXT, value TEXT)")
    con.executemany(
        "INSERT INTO _sidecar_meta VALUES (?, ?)",
        [("dda_rows", "5"), ("obs_max", "2026-09-30")],
    )
    con.commit()
    con.close()
    sets = tmp_path / "cameo_groups.json"
    sets.write_text(
        json.dumps(
            {
                "escalatory": ["010", "020"],
                "instability": [],
                "stability": ["030"],
                "deescalatory": [],
                "critical_incidents": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DDA_SQLITE", str(db))
    monkeypatch.setenv("DDA_SETS_JSON", str(sets))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def test_daily_shares_and_gap(dev_env):
    r = dev_env.get(
        "/dev/dda-daily?iso1=AAA&iso2=BBB&as_of=2026-09-30&window=3"
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["as_of"] == "2026-09-30"
    assert [row["as_of"] for row in data["rows"]] == [
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
    ]
    d28, d29, d30 = data["rows"]
    # 2026-09-28: mapped volume 20; esc 10/20, stab 10/20.
    assert d28["volume_mapped"] == 20
    assert d28["per_set_share"]["escalatory"] == pytest.approx(0.5)
    assert d28["per_set_share"]["stability"] == pytest.approx(0.5)
    # Gap day: volumes zero, shares null (never zero-filled).
    assert d29["volume_mapped"] == 0
    assert d29["per_set_share"]["escalatory"] is None
    # 2026-09-30: mapped 9 (unmapped 999 excluded from mapped,
    # but counted in total); esc share 1.0.
    assert d30["volume_total"] == 12
    assert d30["volume_mapped"] == 9
    assert d30["per_set_share"]["escalatory"] == pytest.approx(1.0)
    assert r.json()["provenance"]["message"].startswith("DEV ONLY")


def test_codes_rows_and_provisional_baseline(dev_env):
    r = dev_env.get(
        "/dev/dda-codes?iso1=AAA&iso2=BBB&as_of=2026-09-30&window=3"
    )
    assert r.status_code == 200
    rows = r.json()["data"]["rows"]
    by_code = {row["cameo_code"]: row for row in rows}
    # Sorted by count at as-of desc: 010 (9) first.
    assert rows[0]["cameo_code"] == "010"
    assert by_code["010"]["count_at_asof"] == 9
    assert by_code["010"]["count_window"] == 13
    assert by_code["010"]["baseline_daily_mean"] == pytest.approx(13 / 3)
    assert by_code["010"]["sets"] == ["escalatory"]
    # Unmapped code still listed, with empty sets.
    assert by_code["999"]["sets"] == []
    assert by_code["999"]["count_at_asof"] == 3


def test_unknown_dyad_returns_empty_rows(dev_env):
    r = dev_env.get("/dev/dda-daily?iso1=ZZZ&iso2=YYY&window=3")
    assert r.status_code == 200
    assert r.json()["data"]["rows"] == []


def test_dev_endpoints_503_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SETS_JSON", raising=False)
    client = TestClient(create_app())
    r = client.get("/dev/dda-daily?iso1=AAA&iso2=BBB")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "db_not_configured"


def test_dev_version_gate_strict(dev_env):
    r = dev_env.get("/dev/dda-daily?iso1=AAA&iso2=BBB&api_version=99")
    assert r.status_code == 409
