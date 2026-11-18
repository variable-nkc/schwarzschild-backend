"""Tests for POST /dev/poc-pipeline (R&D page, 2026-11-17).

Same discipline as test_hybrid.py: small SQLite fixtures in a
temporary directory, monkeypatched environment, no real
products. The numeric goldens below are regression pins for
the numpy port in app/poc_pipeline.py; the port itself was
verified against the original seqbayes engine (dan/poc) on
synthetic records at build time — worst diff 1.9e-13 across
series, forecasts (incl. multi-origin mixtures), anomaly
families, temperature modulation and TF-IDF tables.
"""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

START = date(2025, 6, 1)
END = date(2026, 7, 5)

BODY = {
    "dyad_id": "AAA_BBB",
    "timeframe": {"start": "2025-06-01", "end": "2026-07-05"},
    "decay": {"half_life_days": 14.0, "w_cap": None, "w_alpha": 1.0},
    "tfidf": {"enabled": False, "window_days": 28,
              "tf_mode": "share"},
    "forecast": {"horizons": [45, 60],
                 "models": ["F0p", "F1", "F2"],
                 "params": {}, "origins": [0, 7, 15]},
    "anomaly": {"families": ["A1", "A2", "A3"],
                "baseline_window_days": 28, "params": {}},
    "temperature": {"enabled": True, "window_days": 28,
                    "t_lo": 0.32, "t_hi": 0.42, "w_min": 0.10,
                    "velocity_enabled": False,
                    "velocity_lag_days": 28,
                    "velocity_scale": 0.05, "velocity_max": 0.40},
}


@pytest.fixture()
def poc_env(tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> TestClient:
    dda = tmp_path / "dda.sqlite"
    con = sqlite3.connect(dda)
    con.execute(
        "CREATE TABLE dda (id INTEGER, iso1 TEXT, iso2 TEXT,"
        " obs_date DATE, cameo_code TEXT, obs_count INTEGER)")
    rows = []
    rid = 1
    for i in range(400):
        day = (START + timedelta(days=i)).isoformat()
        rows.append((rid, "AAA", "BBB", day, "010", 100 - i // 8))
        rid += 1
        if i % 7 != 0:
            rows.append((rid, "AAA", "BBB", day, "020", 20 + i // 3))
            rid += 1
        rows.append((rid, "AAA", "BBB", day, "190", 5 + i // 10))
        rid += 1
        rows.append((rid, "AAA", "BBB", day, "999", 3))  # unplaced
        rid += 1
        rows.append((rid, "AAA", "CCC", day, "010", 30))  # referent
        rid += 1
        rows.append((rid, "CCC", "BBB", day, "020", 25))  # referent
        rid += 1
    con.executemany("INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()

    tokens = tmp_path / "cameo_tokens.json"
    tokens.write_text(
        json.dumps([
            {"code": "010", "p1": 0.5, "p2": 0.3, "p3": 0.2,
             "p4": 0.0, "p5": 0.0},
            {"code": "020", "p1": 0.0, "p2": 0.1, "p3": 0.2,
             "p4": 0.3, "p5": 0.4},
            {"code": "190", "p1": 0.02, "p2": 0.05, "p3": 0.13,
             "p4": 0.30, "p5": 0.50},
            {"code": "999", "p1": None, "p2": None, "p3": None,
             "p4": None, "p5": None},
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("DDA_SQLITE", str(dda))
    monkeypatch.setenv("DDA_SIMPLEXES_JSON", str(tokens))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def test_poc_ok(poc_env: TestClient) -> None:
    r = poc_env.post("/dev/poc-pipeline", json=BODY)
    assert r.status_code == 200
    data = r.json()["data"]
    s = data["series"]
    assert len(s["dates"]) == 400
    assert s["dates"][0] == "2025-06-01"
    assert s["dates"][-1] == "2026-07-05"
    assert s["posterior_simplex"][-1] == pytest.approx(
        [0.0353230361, 0.1994199942, 0.3210510602, 0.193279175,
         0.2509267345], abs=1e-8)
    assert s["n_eff"][-1] == pytest.approx(4499.4507117, abs=1e-6)
    # 400 records of the unplaced code 999 were dropped.
    assert data["meta"]["warnings"] == [
        "dropped 400 records with no crosswalk mapping"]
    fc = data["forecast"]["unweighted"]
    assert fc["F1"]["60"]["path"] == pytest.approx(
        [0.0249759616, 0.168732774, 0.2902277051, 0.2221397155,
         0.2939238437], abs=1e-8)
    assert fc["F2"]["45"]["regime_now"] == "calm"
    assert set(fc["F1"]["60"]["origin_points"]) == {"0", "7", "15"}
    an = data["anomaly"]["unweighted"]
    assert set(an["scores"]) >= {"A1", "A2", "A3", "composite"}
    finite = [v for v in an["scores"]["composite"] if v is not None]
    assert max(finite) == pytest.approx(2.17445464, abs=1e-6)
    assert an["temperature"]["params"]["t_lo"] == 0.32
    vr = data["volume_regime"]
    assert len(vr["date"]) == 303
    assert vr["vol_ratio"][-1] == pytest.approx(1.05892953, abs=1e-6)
    # TF-IDF off: blocks report not_enabled, no set-share block.
    assert data["tfidf"] == {"enabled": False}
    assert data["series_tfidf"] == {"status": "not_enabled"}
    assert "setshare" not in data


def test_poc_tfidf(poc_env: TestClient) -> None:
    body = json.loads(json.dumps(BODY))
    body["tfidf"] = {"enabled": True, "window_days": 28,
                     "tf_mode": "share"}
    r = poc_env.post("/dev/poc-pipeline", json=body)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["tfidf"]["enabled"] is True
    assert len(data["tfidf"]["window_metrics"]) == 14
    assert data["tfidf"]["window_metrics"][-1][
        "mean_uniqueness"] == pytest.approx(0.3854855139, abs=1e-8)
    assert data["series_tfidf"]["posterior_simplex"][-1] == \
        pytest.approx(
            [0.0236753157, 0.1758759851, 0.2969524602, 0.2183405988,
             0.2851556402], abs=1e-8)
    assert data["forecast"]["tfidf"]["F1"]["60"]["path"]
    assert data["anomaly"]["tfidf"]["scores"]["composite"]


def test_poc_setshare_key_ignored(poc_env: TestClient) -> None:
    body = json.loads(json.dumps(BODY))
    body["setshare"] = {"enabled": True}
    r = poc_env.post("/dev/poc-pipeline", json=body)
    assert r.status_code == 200
    assert "setshare" not in r.json()["data"]


def test_poc_bad_body(poc_env: TestClient) -> None:
    r = poc_env.post("/dev/poc-pipeline", json={"timeframe": {}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_body"


def test_poc_bad_config(poc_env: TestClient) -> None:
    body = json.loads(json.dumps(BODY))
    body["tfidf"] = {"enabled": True, "window_days": 99,
                     "tf_mode": "share"}
    r = poc_env.post("/dev/poc-pipeline", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_config"
