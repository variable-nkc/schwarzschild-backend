"""Tests for the experimental ``/exp/`` endpoints (exp/raw-data).

Every test builds its own small SQLite fixtures in a temporary
directory: a rollup DB (subset schema — ``SELECT *`` passes columns
through, as in ``tests/test_api.py``) and a DDA sidecar-shaped DB
(table ``dda``). No test reads a real analytics product.

The app reads ROLLUP_SQLITE and DDA_SQLITE at request time, so
monkeypatching the environment per test is enough.

Contract under test (see README, "Experimental /exp/ endpoints"):
same envelope shape as v1 but ``api_version: "exp"``, exempt from
the version gate, provenance message exactly "EXPERIMENTAL — not
part of the v1 contract; never promote." on every response, raw
rows only, unknown dyads return empty rows, and no
``/exp/pcs-slice`` exists.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import EXP_MESSAGE, create_app

ROLLUP_COLS = [
    "iso1", "iso2", "as_of", "temperature", "label",
    "flag_insufficient", "flag_hot", "v_7d",
    "build_id", "vintage", "computed_at",
]


def _rollup_row(iso1, iso2, as_of, label, v_7d, temperature=0.3):
    return {
        "iso1": iso1, "iso2": iso2, "as_of": as_of, "label": label,
        "temperature": temperature, "flag_insufficient": 0,
        "flag_hot": 1 if label == "hot" else 0, "v_7d": v_7d,
        "build_id": "exp-test-build", "vintage": 366,
        "computed_at": "2026-10-27T10:32:25+00:00",
    }


def _build_rollup(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE dyad_status_rollup ("
        "iso1 TEXT, iso2 TEXT, as_of TEXT, temperature REAL, "
        "label TEXT, flag_insufficient INTEGER, flag_hot INTEGER, "
        "v_7d REAL, build_id TEXT, vintage INTEGER, "
        "computed_at TEXT)"
    )
    rows = [
        _rollup_row("USA", "CAN", "2026-09-30", "drifting", 500.0),
        _rollup_row("USA", "IRN", "2026-09-30", "hot", 900.0, 0.6),
        _rollup_row("USA", "MEX", "2026-09-30", "steady", 100.0, 0.1),
        _rollup_row("USA", "CAN", "2026-09-29", "steady", 700.0, 0.2),
        _rollup_row("USA", "IRN", "2026-09-29", "drifting", 800.0, 0.4),
    ]
    for row in rows:
        conn.execute(
            f"INSERT INTO dyad_status_rollup ({', '.join(ROLLUP_COLS)}) "
            f"VALUES ({', '.join('?' * len(ROLLUP_COLS))})",
            [row[c] for c in ROLLUP_COLS],
        )
    conn.commit()
    conn.close()


def _build_dda(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE dda (id INTEGER, iso1 TEXT, iso2 TEXT, "
        "obs_date TEXT, cameo_code TEXT, obs_count INTEGER)"
    )
    # Target dyad USA_CAN plus its inverse CAN_USA (both excluded
    # from the corpus), referents sharing USA or CAN, and one dyad
    # sharing neither country (excluded). Inserted out of order so
    # ordering tests actually test ordering.
    rows = [
        ("USA", "CAN", "2026-03-02", "043", 5),
        ("USA", "CAN", "2026-03-01", "010", 2),
        ("USA", "CAN", "2026-03-01", "042", 1),
        ("CAN", "USA", "2026-03-01", "010", 3),
        ("USA", "MEX", "2026-03-01", "010", 4),
        ("MEX", "CAN", "2026-03-01", "020", 6),
        ("FRA", "USA", "2026-03-02", "030", 7),
        ("CAN", "FRA", "2026-03-01", "010", 8),
        ("MEX", "FRA", "2026-03-01", "010", 9),
    ]
    for i, (a, b, d, code, n) in enumerate(rows):
        conn.execute(
            "INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)",
            (i + 1, a, b, d, code, n),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def rollup_path(tmp_path: Path) -> Path:
    path = tmp_path / "rollup_poc_base.sqlite"
    _build_rollup(path)
    return path


@pytest.fixture()
def dda_path(tmp_path: Path) -> Path:
    path = tmp_path / "dda_poc_base.sqlite"
    _build_dda(path)
    return path


@pytest.fixture()
def client(
    rollup_path: Path, dda_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup_path))
    monkeypatch.setenv("DDA_SQLITE", str(dda_path))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def _assert_exp_envelope(body: dict) -> None:
    assert body["api_version"] == "exp"
    assert body["provenance"]["message"] == EXP_MESSAGE
    assert body["warnings"] == []
    assert body["error"] is None


def test_exp_envelope_and_rollup_provenance(client: TestClient) -> None:
    resp = client.get("/exp/rollup-dyad", params={"iso1": "USA", "iso2": "CAN"})
    assert resp.status_code == 200
    body = resp.json()
    _assert_exp_envelope(body)
    assert body["provenance"]["build_id"] == "exp-test-build"
    assert body["provenance"]["vintage"] == 366


def test_exp_exempt_from_version_gate(client: TestClient) -> None:
    # Even with a mismatched api_version param in strict mode, an
    # /exp/ route must not 409: it takes no api_version parameter.
    resp = client.get(
        "/exp/dda-records",
        params={"iso1": "USA", "iso2": "CAN", "api_version": "999"},
    )
    assert resp.status_code == 200
    _assert_exp_envelope(resp.json())


def test_exp_dda_records_string_codes_and_ordering(
    client: TestClient,
) -> None:
    resp = client.get("/exp/dda-records", params={"iso1": "USA", "iso2": "CAN"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert [(r["obs_date"], r["cameo_code"], r["obs_count"]) for r in rows] == [
        ("2026-03-01", "010", 2),
        ("2026-03-01", "042", 1),
        ("2026-03-02", "043", 5),
    ]
    assert all(isinstance(r["cameo_code"], str) for r in rows)
    # The leading zero survives: "010" is not the int 10.
    assert rows[0]["cameo_code"] == "010"


def test_exp_dda_records_unknown_dyad_empty_not_error(
    client: TestClient,
) -> None:
    resp = client.get("/exp/dda-records", params={"iso1": "ZZZ", "iso2": "YYY"})
    assert resp.status_code == 200
    assert resp.json()["data"]["rows"] == []


def test_exp_corpus_referent_rule_and_exclusions(
    client: TestClient,
) -> None:
    resp = client.get("/exp/corpus", params={"iso1": "USA", "iso2": "CAN"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    dyads = sorted({(r["iso1"], r["iso2"]) for r in rows})
    # Referents: share USA or CAN. Excluded: the target USA_CAN,
    # its inverse CAN_USA, and MEX_FRA (shares neither country).
    assert dyads == [
        ("CAN", "FRA"),
        ("FRA", "USA"),
        ("MEX", "CAN"),
        ("USA", "MEX"),
    ]
    assert all(r["iso1"] != "USA" or r["iso2"] != "CAN" for r in rows)
    assert all(r["iso1"] != "CAN" or r["iso2"] != "USA" for r in rows)
    # Rows carry dyad identity, and the USA_MEX row rides along.
    assert any(
        r["iso1"] == "USA" and r["iso2"] == "MEX"
        and r["cameo_code"] == "010" and r["obs_count"] == 4
        for r in rows
    )


def test_exp_rollup_at_full_rows_ordered_by_dyad(
    client: TestClient,
) -> None:
    resp = client.get("/exp/rollup-at", params={"as_of": "2026-09-30"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert [(r["iso1"], r["iso2"]) for r in rows] == [
        ("USA", "CAN"),
        ("USA", "IRN"),
        ("USA", "MEX"),
    ]
    # Full rows, not a projection: every fixture column present.
    assert all(set(ROLLUP_COLS) <= set(r) for r in rows)
    # Same boolean conversion as v1 rows.
    assert rows[1]["flag_hot"] is True
    assert rows[0]["flag_hot"] is False


def test_exp_rollup_at_empty_as_of(client: TestClient) -> None:
    resp = client.get("/exp/rollup-at", params={"as_of": "2020-01-01"})
    assert resp.status_code == 200
    assert resp.json()["data"]["rows"] == []


def test_exp_rollup_dyad_oldest_first(client: TestClient) -> None:
    resp = client.get("/exp/rollup-dyad", params={"iso1": "USA", "iso2": "CAN"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert [r["as_of"] for r in rows] == ["2026-09-29", "2026-09-30"]
    assert [r["label"] for r in rows] == ["steady", "drifting"]


def test_exp_census_series_shape(client: TestClient) -> None:
    resp = client.get("/exp/census-series")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["dates"] == ["2026-09-29", "2026-09-30"]
    assert data["rows"][1] == {
        "as_of": "2026-09-30", "total": 3,
        "insufficient": 0, "hot": 1, "drifting": 1,
        "atypical": 0, "steady": 1,
    }


def test_exp_notable_ranking_and_limit(client: TestClient) -> None:
    resp = client.get("/exp/notable", params={"limit": 2})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    # Peaks: USA_IRN 900, USA_CAN 700, USA_MEX 100.
    assert rows == [
        {"iso1": "USA", "iso2": "IRN", "peak_v_7d": 900.0},
        {"iso1": "USA", "iso2": "CAN", "peak_v_7d": 700.0},
    ]
    resp = client.get("/exp/notable")
    assert len(resp.json()["data"]["rows"]) == 3


def test_exp_meta_fields(client: TestClient) -> None:
    resp = client.get("/exp/meta")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {
        "dda_min_date": "2026-03-01",
        "dda_max_date": "2026-03-02",
        "dda_rows": 9,
        "dda_dyads": 7,
        "rollup_min_asof": "2026-09-29",
        "rollup_max_asof": "2026-09-30",
        "rollup_dyads": 3,
        "rollup_build_id": "exp-test-build",
        "rollup_vintage": 366,
    }


def test_exp_dda_endpoints_503_without_dda(
    rollup_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup_path))
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    app_client = TestClient(create_app())
    resp = app_client.get(
        "/exp/dda-records", params={"iso1": "USA", "iso2": "CAN"}
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["api_version"] == "exp"
    assert body["provenance"]["message"] == EXP_MESSAGE
    assert body["error"]["code"] == "db_not_configured"
    assert "DDA_SQLITE" in body["error"]["message"]


def test_exp_rollup_endpoints_503_without_rollup(
    dda_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DDA_SQLITE", str(dda_path))
    monkeypatch.delenv("ROLLUP_SQLITE", raising=False)
    app_client = TestClient(create_app())
    resp = app_client.get("/exp/rollup-at", params={"as_of": "2026-09-30"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "db_not_configured"
    # /exp/meta needs both: with only DDA configured it is a 503.
    resp = app_client.get("/exp/meta")
    assert resp.status_code == 503


def test_exp_no_pcs_slice(client: TestClient) -> None:
    resp = client.get("/exp/pcs-slice", params={"iso1": "USA", "iso2": "CAN"})
    assert resp.status_code == 404


def test_v1_endpoints_untouched_by_exp(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["api_version"] == "1"
    resp = client.get(
        "/dyad-status",
        params={"iso1": "USA", "iso2": "IRN", "as_of": "2026-09-30"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["label"] == "hot"
