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


@pytest.mark.parametrize("model", ["F0p", "F0m", "F1"])
def test_hybrid_model_selectable(hybrid_env: TestClient, model: str) -> None:
    r = hybrid_env.get("/lc/hybrid",
                       params={"iso1": "AAA", "iso2": "BBB",
                               "as_of": "2026-09-30", "model": model})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "ok"
    assert data["model"] == model and data["horizon"] == 60
    assert len(data["path"]) == 5
    assert len(data["lower"]) == 5 and len(data["upper"]) == 5
    assert sum(data["path"]) == pytest.approx(1.0)
    for lo, path, hi in zip(data["lower"], data["path"], data["upper"]):
        assert lo <= path <= hi
    # Regime extras are F2-only: non-F2 leaves carry nulls, never
    # a stale regime call.
    assert data["regime_now"] is None and data["p_change"] is None


def test_hybrid_model_unknown_is_400(hybrid_env: TestClient) -> None:
    r = hybrid_env.get("/lc/hybrid",
                       params={"iso1": "AAA", "iso2": "BBB",
                               "as_of": "2026-09-30", "model": "F3"})
    assert r.status_code == 400


def test_hybrid_trend_defaults_and_params(hybrid_env: TestClient) -> None:
    r = hybrid_env.get("/lc/hybrid",
                       params={"iso1": "AAA", "iso2": "BBB",
                               "as_of": "2026-09-30"})
    data = r.json()["data"]
    assert data["status"] == "ok"
    assert data["trend"] == {"half_life_days": 7.0, "w_cap": 0.0,
                             "w_alpha": 1.0}
    r = hybrid_env.get("/lc/hybrid",
                       params={"iso1": "AAA", "iso2": "BBB",
                               "as_of": "2026-09-30", "half_life": 14,
                               "w_cap": 500, "w_alpha": 0.5})
    data = r.json()["data"]
    assert data["status"] == "ok"
    assert data["trend"] == {"half_life_days": 14.0, "w_cap": 500.0,
                             "w_alpha": 0.5}
    assert sum(data["path"]) == pytest.approx(1.0)


def test_hybrid_trend_invalid_is_400(hybrid_env: TestClient) -> None:
    base = {"iso1": "AAA", "iso2": "BBB", "as_of": "2026-09-30"}
    for extra in ({"half_life": 0}, {"half_life": -3},
                  {"w_cap": -1}, {"w_alpha": -0.5}):
        r = hybrid_env.get("/lc/hybrid", params={**base, **extra})
        assert r.status_code == 400, extra


def test_hybrid_trend_series(hybrid_env: TestClient) -> None:
    """trend_series: posterior per history date, aligned by date.

    Fixture AAA_BBB has 2 gap days (2026-06-01, 2026-08-15)
    inside the last-180-day history window. Gap days carry a
    posterior (carried forward from the previous day), so the
    only nulls would be pre-first-data dates — and this
    dyad's data starts before the history window, so none.
    """
    r = hybrid_env.get("/lc/hybrid",
                       params={"iso1": "AAA", "iso2": "BBB",
                               "as_of": "2026-09-30"})
    data = r.json()["data"]
    assert data["status"] == "ok"
    ts = data["trend_series"]
    hist = data["history"]
    assert len(ts) == len(hist) == 180
    assert [row["date"] for row in ts] == [row["date"] for row in hist]
    by_date = {row["date"]: row for row in ts}
    # Every trend row is a simplex (gap rows included).
    for row in ts:
        assert sum(row[p] for p in ("p1", "p2", "p3", "p4", "p5")) == \
            pytest.approx(1.0)
    # Gap days carry the previous day's posterior forward.
    for gap, prev in (("2026-06-01", "2026-05-31"),
                      ("2026-08-15", "2026-08-14")):
        assert by_date[gap]["p1"] is not None
        assert by_date[gap]["p1"] == pytest.approx(by_date[prev]["p1"])
    # The trend is not the raw joints echoed back.
    assert any(abs(t["p1"] - h["p1"]) > 1e-9
               for t, h in zip(ts, hist) if h["p1"] is not None)


def test_hybrid_trend_series_moves_with_params(
        hybrid_env: TestClient) -> None:
    """The (3) fix at the backend level: a different half-life
    yields a different trend series (the displayed line moves
    with the Trend inputs, not just the forecast point)."""
    base = {"iso1": "AAA", "iso2": "BBB", "as_of": "2026-09-30"}
    ts7 = hybrid_env.get("/lc/hybrid", params=base).json()["data"][
        "trend_series"]
    ts30 = hybrid_env.get(
        "/lc/hybrid", params={**base, "half_life": 30}).json()["data"][
        "trend_series"]
    assert [row["date"] for row in ts7] == [row["date"] for row in ts30]
    assert any(abs(a["p1"] - b["p1"]) > 1e-9
               for a, b in zip(ts7, ts30) if a["p1"] is not None)
