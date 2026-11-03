"""Tests for GET /lc/hybrid (dev/dda-sidecar branch only).

Same discipline as test_dev_dda.py: every test builds its own
small SQLite fixtures in a temporary directory and monkeypatches
the environment. No test reads a real product. These tests cover
the endpoint's arithmetic and gating only — they do not bless
the shape as contract (formalization does that).
"""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

START = date(2026, 3, 15)


@pytest.fixture()
def hybrid_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Rollup (effective as-of) + DDA sidecar (200 days, 2 gap days)."""
    rollup = tmp_path / "rollup.sqlite"
    con = sqlite3.connect(rollup)
    con.execute(
        "CREATE TABLE dyad_status_rollup (iso1 TEXT, iso2 TEXT,"
        " as_of TEXT, label TEXT, build_id TEXT, vintage INTEGER,"
        " computed_at TEXT)"
    )
    con.execute(
        "INSERT INTO dyad_status_rollup VALUES"
        " ('AAA', 'BBB', '2026-09-30', 'steady', 't', 1, 'now'),"
        " ('CCC', 'DDD', '2026-05-03', 'steady', 't', 1, 'now')"
    )
    con.commit()
    con.close()

    dda = tmp_path / "dda.sqlite"
    con = sqlite3.connect(dda)
    con.execute(
        "CREATE TABLE dda (id INTEGER, iso1 TEXT, iso2 TEXT,"
        " obs_date DATE, cameo_code TEXT, obs_count INTEGER)"
    )
    rows = []
    rid = 1
    for i in range(200):
        day = START + timedelta(days=i)
        if day.isoformat() in ("2026-06-01", "2026-08-15"):
            continue  # gap days: no rows at all
        # Slowly destabilizing mix: stable code fades, crisis code grows.
        rows.append((rid, "AAA", "BBB", day.isoformat(), "010",
                     100 - i // 4))
        rid += 1
        rows.append((rid, "AAA", "BBB", day.isoformat(), "020", 20 + i // 2))
        rid += 1
    # Short-history dyad: 50 days only.
    for i in range(50):
        day = START + timedelta(days=i)
        rows.append((rid, "CCC", "DDD", day.isoformat(), "010", 50))
        rid += 1
    con.executemany("INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()

    tokens = tmp_path / "cameo_tokens.json"
    tokens.write_text(
        json.dumps([
            {"code": "010", "p1": 0.5, "p2": 0.3, "p3": 0.2, "p4": 0.0,
             "p5": 0.0},
            {"code": "020", "p1": 0.0, "p2": 0.1, "p3": 0.2, "p4": 0.3,
             "p5": 0.4},
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup))
    monkeypatch.setenv("DDA_SQLITE", str(dda))
    monkeypatch.setenv("DDA_SIMPLEXES_JSON", str(tokens))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def test_hybrid_ok(hybrid_env: TestClient) -> None:
    r = hybrid_env.get("/lc/hybrid",
                       params={"iso1": "AAA", "iso2": "BBB",
                               "as_of": "2026-09-30"})
    assert r.status_code == 200
    body = r.json()
    assert body["provenance"]["message"] == \
        "LC DEV ONLY — not part of the v1 contract."
    data = body["data"]
    assert data["status"] == "ok"
    assert data["model"] == "F2" and data["horizon"] == 60
    assert data["as_of"] == "2026-09-30"
    assert data["origins"] == [0, 7, 15, 30, 45]
    assert len(data["path"]) == 5
    assert len(data["lower"]) == 5 and len(data["upper"]) == 5
    assert sum(data["path"]) == pytest.approx(1.0)
    for lo, path, hi in zip(data["lower"], data["path"], data["upper"]):
        assert lo <= path <= hi
    assert data["regime_now"] in ("calm", "agitated")
    assert 0.0 <= data["p_change"] <= 1.0
    assert len(data["history"]) == 180
    assert data["series"]["n_filled"] == 2
    # Origin targets: T+60 down to T+15.
    assert data["origin_targets"]["0"] == "2026-11-29"
    assert data["origin_targets"]["45"] == "2026-10-15"
    assert set(data["origin_points"]) == {"0", "7", "15", "30", "45"}


def test_hybrid_short_history(hybrid_env: TestClient) -> None:
    r = hybrid_env.get("/lc/hybrid",
                       params={"iso1": "CCC", "iso2": "DDD",
                               "as_of": "2026-05-03"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "insufficient_history"
    assert data["n_days"] == 50 and data["required_days"] == 80


def test_hybrid_unknown_dyad(hybrid_env: TestClient) -> None:
    r = hybrid_env.get("/lc/hybrid",
                       params={"iso1": "ZZZ", "iso2": "YYY",
                               "as_of": "2026-09-30"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "no_data"
