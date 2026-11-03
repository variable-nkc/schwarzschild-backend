"""Tests for GET /dev/health (dev/dda-sidecar only).

Same discipline as test_country.py: small SQLite fixtures in
a temporary directory, monkeypatched environment, no real
products. Covers the health spec (Dan, 2026-11-03, as
reviewed): freshness icons, rollup (2) as v_7d > 0, (3)
join counts, (4) label bands with the atypical small-number
exemption, same-weekday baselines for single-day metrics,
cross-store ratios, no_data payloads, and 503s on missing
configuration — not the shape as contract (formalization
does that).

Fixture calendar: as-ofs / obs dates 2026-09-02..09-30
daily (2026-09-30 is a Wednesday; prior Wednesdays are
09-02, 09-09, 09-16, 09-23). Rollup dyads: A_B hot,
C_D steady, E_F drifting, G_H insufficient (v_7d 0),
K_L insufficient (v_7d 10 — volume but no call),
I_J atypical on 09-29 only. DDA dyads A_B, C_D, E_F
daily (one row each, obs_count 1), no rows on 09-27
(a gap day: zeros, never skipped). Mirror events:
pairs (A,B), (C,D), (E,F) daily plus one domestic
(X,X) and one null-code row daily (counted in events,
excluded from pairs), no rows on 09-27.
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

START = date(2026, 9, 2)
END = date(2026, 9, 30)
ROLLUP_DYADS = [
    ("A", "B", 100, "hot"),
    ("C", "D", 50, "steady"),
    ("E", "F", 30, "drifting"),
    ("G", "H", 0, "insufficient"),
    ("K", "L", 10, "insufficient"),
    ("I", "J", 0, "insufficient"),
]


def _days():
    day = START
    while day <= END:
        yield day
        day += timedelta(days=1)


@pytest.fixture()
def health_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    rollup = tmp_path / "rollup.sqlite"
    con = sqlite3.connect(rollup)
    con.execute(
        "CREATE TABLE dyad_status_rollup (iso1 TEXT, iso2 TEXT,"
        " as_of TEXT, v_7d REAL, temperature REAL, label TEXT,"
        " vintage INTEGER, build_id TEXT)"
    )
    for day in _days():
        for iso1, iso2, v_7d, label in ROLLUP_DYADS:
            lab = label
            if (iso1, iso2) == ("I", "J") and day == date(2026, 9, 29):
                lab = "atypical"
            con.execute(
                "INSERT INTO dyad_status_rollup VALUES (?, ?, ?, ?,"
                " 0.5, ?, 366, 'test-build')",
                (iso1, iso2, day.isoformat(), v_7d, lab),
            )
    con.commit()
    con.close()

    dda = tmp_path / "dda.sqlite"
    con = sqlite3.connect(dda)
    con.execute(
        "CREATE TABLE dda (iso1 TEXT, iso2 TEXT, obs_date TEXT,"
        " cameo_code TEXT, obs_count INTEGER)"
    )
    for day in _days():
        if day == date(2026, 9, 27):
            continue
        for iso1, iso2 in (("A", "B"), ("C", "D"), ("E", "F")):
            con.execute(
                "INSERT INTO dda VALUES (?, ?, ?, '001', 1)",
                (iso1, iso2, day.isoformat()),
            )
    con.commit()
    con.close()

    mirror = tmp_path / "mirror.sqlite"
    con = sqlite3.connect(mirror)
    con.execute(
        "CREATE TABLE gdelt_events_core (event_date TEXT,"
        " actor1_country_code TEXT, actor2_country_code TEXT,"
        " is_root_event INTEGER)"
    )
    for day in _days():
        if day == date(2026, 9, 27):
            continue
        for a1, a2 in (("A", "B"), ("C", "D"), ("E", "F")):
            con.execute(
                "INSERT INTO gdelt_events_core VALUES (?, ?, ?, 1)",
                (day.isoformat(), a1, a2),
            )
        con.execute(
            "INSERT INTO gdelt_events_core VALUES (?, 'X', 'X', 1)",
            (day.isoformat(),),
        )
        con.execute(
            "INSERT INTO gdelt_events_core VALUES (?, NULL, 'B', 0)",
            (day.isoformat(),),
        )
    con.commit()
    con.close()

    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup))
    monkeypatch.setenv("DDA_SQLITE", str(dda))
    monkeypatch.setenv("MIRROR_SQLITE", str(mirror))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def _get(client: TestClient, as_of: str) -> dict:
    resp = client.get("/dev/health",
                      params={"as_of": as_of, "api_version": "1"})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_rollup_block(health_env: TestClient):
    data = _get(health_env, "2026-09-30")
    roll = data["rollup"]
    assert roll["status"] == "ok"
    assert roll["freshness"] == {"eval_date": "2026-09-30",
                                 "age_days": 0, "icon": "green"}
    # (2): v_7d > 0 counts A_B, C_D, E_F, K_L — G_H (0) and
    # I_J (0) excluded. Baseline is flat 4.0.
    assert roll["dyads_present"]["count"] == 4
    assert roll["dyads_present"]["baseline"] == pytest.approx(4.0)
    assert roll["dyads_present"]["pct"] == pytest.approx(100.0)
    assert roll["dyads_present"]["icon"] == "green"
    # (3): non-insufficient is 3 at eval (I_J insufficient
    # on 09-30); baseline averages the 09-29 atypical day in.
    assert roll["non_insufficient"]["count"] == 3
    assert roll["non_insufficient"]["baseline"] == pytest.approx(
        85 / 28)
    assert roll["non_insufficient"]["icon"] == "green"
    # (4): shares stable; atypical exempt (avg count 1/28).
    assert roll["labels"]["hot"]["count"] == 1
    assert roll["labels"]["hot"]["icon"] == "green"
    assert roll["labels"]["atypical"]["icon"] is None
    assert roll["labels_overall_icon"] == "green"
    assert roll["insufficient_count"] == 3
    assert data["provenance"]["rollup_vintage"] == 366
    assert data["provenance"]["rollup_build_id"] == "test-build"


def test_dda_and_poc_base_blocks(health_env: TestClient):
    data = _get(health_env, "2026-09-30")
    dda = data["dda"]
    assert dda["dyads_present"]["count"] == 3
    assert dda["dyads_present"]["baseline"] == pytest.approx(3.0)
    assert dda["dyads_present"]["icon"] == "green"
    assert dda["records"]["count"] == 3
    assert dda["records"]["baseline"] == pytest.approx(3.0)
    # (3) join: DDA dyads are all rollup-non-insufficient.
    assert dda["non_insufficient"]["count"] == 3
    assert dda["non_insufficient"]["baseline"] == pytest.approx(3.0)
    # Gap day 09-27 is a zero in the records series, not a skip.
    series = dict(dda["records"]["series"])
    assert series["2026-09-27"] == 0
    pb = data["poc_base"]
    assert pb["dyads_present"]["count"] == 3
    assert pb["events"]["count"] == 5  # 3 pairs + domestic + null
    assert pb["events"]["baseline"] == pytest.approx(5.0)
    assert pb["non_insufficient"]["count"] == 3


def test_cross_store_block(health_env: TestClient):
    data = _get(health_env, "2026-09-30")
    cross = data["cross_store"]
    assert cross["common_date"] == "2026-09-30"
    assert cross["dda_events_sum"] == 3
    assert cross["poc_base_events"] == 5
    assert cross["volume_ratio"]["ratio"] == pytest.approx(0.6)
    assert cross["volume_ratio"]["baseline"] == pytest.approx(0.6)
    assert cross["volume_ratio"]["icon"] == "green"
    assert cross["dyad_jaccard"]["ratio"] == pytest.approx(1.0)
    assert cross["intersection"] == 3
    assert cross["union"] == 3


def test_freshness_icons(health_env: TestClient):
    data = _get(health_env, "2026-10-02")
    assert data["rollup"]["freshness"]["icon"] == "yellow"
    assert data["rollup"]["freshness"]["age_days"] == 2
    data = _get(health_env, "2026-10-03")
    assert data["rollup"]["freshness"]["icon"] == "red"
    assert data["rollup"]["freshness"]["age_days"] == 3


def test_no_data(health_env: TestClient):
    data = _get(health_env, "2026-01-01")
    assert data["status"] == "no_data"
    assert data["rollup"] is None


def test_missing_config_503(tmp_path: Path,
                            monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    monkeypatch.setenv("ROLLUP_SQLITE", str(tmp_path / "r.sqlite"))
    monkeypatch.setenv("MIRROR_SQLITE", str(tmp_path / "m.sqlite"))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    client = TestClient(create_app())
    resp = client.get("/dev/health",
                      params={"as_of": "2026-09-30", "api_version": "1"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "db_missing"
