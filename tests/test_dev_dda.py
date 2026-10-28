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
    """Tiny sidecar (3 days, 1 gap day) + tiny sets/tokens JSON.

    Code 040 is the pin for the mapped denominator: present in
    the tokens list (mapped) but in no action set. Under the
    old union denominator it would be excluded from mapped
    volume; under the tokens denominator it counts.
    """
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
        # 2026-09-30: esc 010 (9); unmapped 999 (3, excluded);
        # mapped-but-unset 040 (6, counts toward mapped).
        (4, "AAA", "BBB", "2026-09-30", "010", 9),
        (5, "AAA", "BBB", "2026-09-30", "999", 3),
        (6, "AAA", "BBB", "2026-09-30", "040", 6),
    ]
    con.executemany("INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE _sidecar_meta (key TEXT, value TEXT)")
    con.executemany(
        "INSERT INTO _sidecar_meta VALUES (?, ?)",
        [("dda_rows", "6"), ("obs_max", "2026-09-30")],
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
    tokens = tmp_path / "cameo_tokens.json"
    tokens.write_text(
        json.dumps(
            [
                {"code": "010", "p1": 0, "p2": 0, "p3": 0.2, "p4": 0.3, "p5": 0.5},
                {"code": "020", "p1": 0, "p2": 0, "p3": 0.2, "p4": 0.3, "p5": 0.5},
                {"code": "030", "p1": 0.5, "p2": 0.3, "p3": 0.2, "p4": 0, "p5": 0},
                # Mapped but in no set; p-values are not read.
                {"code": "040", "p1": None, "p2": None, "p3": None,
                 "p4": None, "p5": None},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DDA_SQLITE", str(db))
    monkeypatch.setenv("DDA_SETS_JSON", str(sets))
    monkeypatch.setenv("DDA_SIMPLEXES_JSON", str(tokens))
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
    # 2026-09-30: total 18 (9 + 3 + 6); mapped 15 (9 + 6 —
    # unmapped 999 excluded, mapped-but-unset 040 included);
    # esc share 9/15. Under the old union denominator mapped
    # would be 9 and esc share 1.0 — this pins the switch.
    assert d30["volume_total"] == 18
    assert d30["volume_mapped"] == 15
    assert d30["per_set_share"]["escalatory"] == pytest.approx(0.6)
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
    # Mapped-but-unset code: in no set, but counts toward
    # mapped volume (share 6/15) and its own baseline.
    assert by_code["040"]["sets"] == []
    assert by_code["040"]["count_at_asof"] == 6
    assert by_code["040"]["share_at_asof"] == pytest.approx(0.4)
    assert by_code["040"]["count_window"] == 6
    assert by_code["040"]["baseline_daily_mean"] == pytest.approx(2.0)


def test_joint_values_gap_and_coverage(dev_env):
    # 2026-09-28: 010 (4) + 020 (6) share simplex [0,0,0.2,0.3,0.5],
    # 030 (10) has [0.5,0.3,0.2,0,0]. Delta-replaced (0 -> 0.005,
    # rescaled by 1.01) then obs-weighted closed geometric mean
    # (10 units each side): raw = [sqrt(.00495*.49505),
    # sqrt(.00495*.29703), .19802, sqrt(.29703*.00495),
    # sqrt(.49505*.00495)] = [.04950, .03835, .19802, .03835,
    # .04950], closed by /0.37372 (hand-computed; an earlier
    # version of this test had p2 wrong and the code was right).
    r = dev_env.get("/dev/dda-joint", params={
        "iso1": "AAA", "iso2": "BBB", "as_of": "2026-09-30", "window": 3})
    assert r.status_code == 200, r.text
    rows = r.json()["data"]["rows"]
    assert [row["as_of"] for row in rows] == [
        "2026-09-28", "2026-09-29", "2026-09-30"]
    j28 = rows[0]["joint"]
    assert j28 is not None
    for phase, expected in (("p1", 0.1325), ("p2", 0.1026),
                            ("p3", 0.5299), ("p4", 0.1026),
                            ("p5", 0.1325)):
        assert j28[phase] == pytest.approx(expected, abs=1e-3), j28
    assert abs(sum(j28.values()) - 1.0) < 1e-9
    assert rows[0]["joint_volume"] == 20
    assert rows[0]["volume_total"] == 20
    # Gap day: null joint, never carried forward.
    assert rows[1]["joint"] is None
    assert rows[1]["joint_volume"] == 0
    assert rows[1]["volume_total"] == 0
    # 2026-09-30: only 010 is joint-able (999 unmapped, 040
    # null-simplex excluded) — joint IS 010's replaced simplex,
    # and the coverage gap (9 of 18) stays visible.
    j30 = rows[2]["joint"]
    assert j30 is not None
    assert j30["p1"] == pytest.approx(0.005 / 1.01, abs=1e-9)
    assert j30["p5"] == pytest.approx(0.5 / 1.01, abs=1e-9)
    assert rows[2]["joint_volume"] == 9
    assert rows[2]["volume_total"] == 18
    assert r.json()["provenance"]["message"].startswith("DEV ONLY")
    # Unknown dyad: empty rows (same shape as daily/codes).
    r2 = dev_env.get("/dev/dda-joint", params={
        "iso1": "ZZZ", "iso2": "QQQ"})
    assert r2.json()["data"]["rows"] == []


def test_unknown_dyad_returns_empty_rows(dev_env):
    r = dev_env.get("/dev/dda-daily?iso1=ZZZ&iso2=YYY&window=3")
    assert r.status_code == 200
    assert r.json()["data"]["rows"] == []


def test_dev_endpoints_503_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SETS_JSON", raising=False)
    monkeypatch.delenv("DDA_SIMPLEXES_JSON", raising=False)
    client = TestClient(create_app())
    r = client.get("/dev/dda-daily?iso1=AAA&iso2=BBB")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "db_not_configured"


def test_dev_version_gate_strict(dev_env):
    r = dev_env.get("/dev/dda-daily?iso1=AAA&iso2=BBB&api_version=99")
    assert r.status_code == 409
