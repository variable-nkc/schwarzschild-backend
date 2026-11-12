"""Tests for the pill's observation support (``exc45_support``).

The pill's mu is a volume-weighted mean of a carry-forward share
series, so a high exceedance can rest on very few genuine activity
days. Support — genuine days in the trailing 90d window and the
age of the most recent genuine day, counted from the DDA sidecar
for the pill's driving set — is attached alongside ``exc45_max``
so the frontend can annotate and tier the pill. It never alters a
served value.

Optionality mirrors the pill's own contract, one level down:
predictive unserved -> no ``exc45_max`` and no ``exc45_support``;
predictive served but the DDA pair (DDA_SQLITE + DDA_SETS_JSON)
unconfigured -> ``exc45_max`` present, ``exc45_support`` absent;
both served -> ``exc45_support`` is a dict for a dyad with a
pill, None for a served-but-sparse dyad (``exc45_max`` None).

Fixtures follow the test_predictive_sets.py pattern.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import _support_at

AS_OFS = ["2026-09-28", "2026-09-29", "2026-09-30"]

ROLLUP_COLUMNS = [
    "iso1", "iso2", "as_of", "temperature", "label",
    "max_exc45", "max_exc45_set", "v_7d", "days_present_7",
    "flag_insufficient", "flag_hot", "flag_drifting",
    "flag_atypical", "anomaly_composite", "anomaly_composite_raw",
    "anomaly_max3d", "build_id", "vintage", "computed_at",
]

# Fixture set membership: one code per set, deliberately disjoint.
SETS_JSON = {
    "escalatory": ["190"],
    "instability": ["191"],
    "stability": ["010"],
    "deescalatory": ["020"],
    "critical_incidents": ["192"],
}

# AAA_BBB escalatory (code 190) genuine days: one before the
# trailing window of every fixture as-of, two inside it. The
# 2026-09-25 day also carries a stability code and an unmapped
# code; neither may count toward escalatory support.
DDA_ROWS = [
    ("AAA", "BBB", "2026-06-15", "190", 4),
    ("AAA", "BBB", "2026-09-22", "190", 5),
    ("AAA", "BBB", "2026-09-25", "190", 3),
    ("AAA", "BBB", "2026-09-25", "010", 6),
    ("AAA", "BBB", "2026-09-25", "999", 2),
    ("AAA", "BBB", "2026-09-26", "191", 1),
]


def _rollup_row(iso1: str, iso2: str, as_of: str) -> list:
    return [iso1, iso2, as_of, 0.42, "steady", 0.31, "stability",
            120, 7, 0, 0, 0, 0, 0.5, 0.6, 0.7, "fixture", 380,
            "2026-10-01T00:00:00Z"]


@pytest.fixture()
def rollup_path(tmp_path: Path) -> Path:
    path = tmp_path / "rollup.sqlite"
    conn = sqlite3.connect(path)
    cols = ", ".join(f'"{c}"' for c in ROLLUP_COLUMNS)
    conn.execute(f"CREATE TABLE dyad_status_rollup ({cols})")
    for dyad in (("AAA", "BBB"), ("CCC", "DDD")):
        for as_of in AS_OFS:
            conn.execute(
                f"INSERT INTO dyad_status_rollup VALUES "
                f"({', '.join('?' * len(ROLLUP_COLUMNS))})",
                _rollup_row(dyad[0], dyad[1], as_of),
            )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def predictive_path_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "rollup_predictive.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE dyad_predictive_summary "
        "(iso1 TEXT, iso2 TEXT, as_of TEXT, exc45_max REAL, "
        "exc45_max_set TEXT)"
    )
    conn.execute(
        'CREATE TABLE dyad_set_predictive '
        '(iso1 TEXT, iso2 TEXT, as_of TEXT, "set" TEXT, mu REAL, '
        "phi_current REAL, phi_adopted REAL, k REAL, bar REAL, "
        "exc45 REAL, vintage INTEGER, validated INTEGER)"
    )
    # AAA_BBB has a pill (driving set escalatory) at every as-of;
    # CCC_DDD deliberately has no summary row (served but sparse).
    for as_of in AS_OFS:
        conn.execute(
            "INSERT INTO dyad_predictive_summary VALUES "
            "('AAA', 'BBB', ?, 0.9984, 'escalatory')",
            (as_of,),
        )
        conn.execute(
            "INSERT INTO dyad_set_predictive VALUES "
            "('AAA', 'BBB', ?, 'escalatory', 0.5595, 12.0, 12.956, "
            "5.1245, 0.1868, 0.9984, 380, 1)",
            (as_of,),
        )
        conn.execute(
            "INSERT INTO dyad_set_predictive VALUES "
            "('AAA', 'BBB', ?, 'instability', 0.2642, 10.0, "
            "10.249, 5.1245, 0.4363, 0.1108, 380, 1)",
            (as_of,),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def dda_path_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "dda.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE dda (id INTEGER, iso1 TEXT, iso2 TEXT, "
        "obs_date TEXT, cameo_code TEXT, obs_count INTEGER)"
    )
    for i, row in enumerate(DDA_ROWS):
        conn.execute("INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)",
                     (i, *row))
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def sets_path_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "cameo_groups.json"
    path.write_text(json.dumps(SETS_JSON), encoding="utf-8")
    return path


@pytest.fixture()
def client(rollup_path: Path, monkeypatch: pytest.MonkeyPatch,
           ) -> TestClient:
    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup_path))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def _serve_all(client: TestClient, predictive_path_fixture: Path,
               dda_path_fixture: Path, sets_path_fixture: Path,
               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREDICTIVE_SQLITE",
                       str(predictive_path_fixture))
    monkeypatch.setenv("DDA_SQLITE", str(dda_path_fixture))
    monkeypatch.setenv("DDA_SETS_JSON", str(sets_path_fixture))


def test_support_at_pure() -> None:
    dates = ["2026-06-15", "2026-09-22", "2026-09-25"]
    # Window for 2026-09-30 is [2026-07-03, 2026-09-30]: the June
    # day is outside it but is not the last seen either.
    assert _support_at(dates, "2026-09-30") == {
        "genuine_days_90d": 2, "last_seen_days_ago": 5}
    assert _support_at(dates, "2026-09-28") == {
        "genuine_days_90d": 2, "last_seen_days_ago": 3}
    # Before any genuine day: nothing in window, never seen.
    assert _support_at(dates, "2026-06-01") == {
        "genuine_days_90d": 0, "last_seen_days_ago": None}
    assert _support_at([], "2026-09-30") == {
        "genuine_days_90d": 0, "last_seen_days_ago": None}


def test_dyad_status_support(
    client: TestClient, predictive_path_fixture: Path,
    dda_path_fixture: Path, sets_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_all(client, predictive_path_fixture, dda_path_fixture,
               sets_path_fixture, monkeypatch)
    resp = client.get("/dyad-status",
                      params={"iso1": "AAA", "iso2": "BBB",
                              "as_of": "2026-09-30"})
    assert resp.status_code == 200
    row = resp.json()["data"]
    assert row["exc45_max"] == 0.9984
    assert row["exc45_support"] == {
        "set": "escalatory", "genuine_days_90d": 2,
        "last_seen_days_ago": 5,
    }


def test_dyad_status_support_sparse_dyad_is_none(
    client: TestClient, predictive_path_fixture: Path,
    dda_path_fixture: Path, sets_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_all(client, predictive_path_fixture, dda_path_fixture,
               sets_path_fixture, monkeypatch)
    resp = client.get("/dyad-status",
                      params={"iso1": "CCC", "iso2": "DDD",
                              "as_of": "2026-09-30"})
    assert resp.status_code == 200
    row = resp.json()["data"]
    assert row["exc45_max"] is None
    assert "exc45_support" in row and row["exc45_support"] is None


def test_support_absent_without_dda(
    client: TestClient, predictive_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PREDICTIVE_SQLITE",
                       str(predictive_path_fixture))
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SETS_JSON", raising=False)
    resp = client.get("/dyad-status",
                      params={"iso1": "AAA", "iso2": "BBB",
                              "as_of": "2026-09-30"})
    assert resp.status_code == 200
    row = resp.json()["data"]
    assert row["exc45_max"] == 0.9984
    assert "exc45_support" not in row


def test_support_absent_when_predictive_unserved(
    client: TestClient, dda_path_fixture: Path,
    sets_path_fixture: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PREDICTIVE_SQLITE", raising=False)
    monkeypatch.setenv("DDA_SQLITE", str(dda_path_fixture))
    monkeypatch.setenv("DDA_SETS_JSON", str(sets_path_fixture))
    resp = client.get("/dyad-status",
                      params={"iso1": "AAA", "iso2": "BBB",
                              "as_of": "2026-09-30"})
    assert resp.status_code == 200
    row = resp.json()["data"]
    assert "exc45_max" not in row
    assert "exc45_support" not in row


def test_watchlist_rows_carry_support(
    client: TestClient, predictive_path_fixture: Path,
    dda_path_fixture: Path, sets_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_all(client, predictive_path_fixture, dda_path_fixture,
               sets_path_fixture, monkeypatch)
    resp = client.get("/watchlist",
                      params={"as_of": "2026-09-30", "limit": 10})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    by_dyad = {f"{r['iso1']}_{r['iso2']}": r for r in rows}
    assert by_dyad["AAA_BBB"]["exc45_support"] == {
        "set": "escalatory", "genuine_days_90d": 2,
        "last_seen_days_ago": 5,
    }
    assert by_dyad["CCC_DDD"]["exc45_support"] is None


def test_pcs_series_support(
    client: TestClient, predictive_path_fixture: Path,
    dda_path_fixture: Path, sets_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_all(client, predictive_path_fixture, dda_path_fixture,
               sets_path_fixture, monkeypatch)
    resp = client.get("/pcs-series",
                      params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    # Series rows (Dig pill strip) carry the driving set's support
    # at each as-of.
    last = data["rows"][-1]
    assert last["as_of"] == "2026-09-30"
    assert last["exc45_support"] == {
        "set": "escalatory", "genuine_days_90d": 2,
        "last_seen_days_ago": 5,
    }
    # Per-set rows (Vet) carry their own set's support: escalatory
    # as above; instability was seen once, on 2026-09-26.
    sets = data["predictive_sets"]
    assert sets["escalatory"][-1]["genuine_days_90d"] == 2
    assert sets["escalatory"][-1]["last_seen_days_ago"] == 5
    assert sets["instability"][-1]["genuine_days_90d"] == 1
    assert sets["instability"][-1]["last_seen_days_ago"] == 4


def test_pcs_series_support_absent_without_dda(
    client: TestClient, predictive_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PREDICTIVE_SQLITE",
                       str(predictive_path_fixture))
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SETS_JSON", raising=False)
    resp = client.get("/pcs-series",
                      params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "genuine_days_90d" not in data["predictive_sets"][
        "escalatory"][-1]
    assert data["rows"][-1]["exc45_support"] is None
