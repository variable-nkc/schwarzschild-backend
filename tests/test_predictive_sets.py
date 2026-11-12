"""Tests for the /pcs-series ``predictive_sets`` payload.

The per-set rows of the set-share predictive build
(``dyad_set_predictive``: mu, phi_adopted, bar, exc45 per as-of
for the validated sets) are served alongside the rollup-history
rows when PREDICTIVE_SQLITE is set, under the same optionality
contract as the ``exc45_max`` summary attach: unset/missing ->
the key is absent entirely; served but no rows for the dyad ->
the key is present with an empty dict.

Fixtures follow the test_api.py pattern: small SQLite files in a
temporary directory, env monkeypatched per test.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

AS_OFS = ["2026-09-28", "2026-09-29", "2026-09-30"]

ROLLUP_COLUMNS = [
    "iso1", "iso2", "as_of", "temperature", "label",
    "max_exc45", "max_exc45_set", "v_7d", "days_present_7",
    "flag_insufficient", "flag_hot", "flag_drifting",
    "flag_atypical", "anomaly_composite", "anomaly_composite_raw",
    "anomaly_max3d", "build_id", "vintage", "computed_at",
]

# (as_of, mu, phi_adopted, bar, exc45) per set, deliberately not
# in as-of order at insert time: the serve must sort.
SET_ROWS = {
    "escalatory": [
        ("2026-09-30", 0.5595, 12.956, 0.1868, 0.9984),
        ("2026-09-28", 0.5001, 11.0, 0.1868, 0.9900),
        ("2026-09-29", 0.5300, 12.0, 0.1868, 0.9950),
    ],
    "instability": [
        ("2026-09-29", 0.2500, 9.5, 0.4363, 0.0900),
        ("2026-09-30", 0.2642, 10.249, 0.4363, 0.1108),
        ("2026-09-28", 0.2400, 9.0, 0.4363, 0.0800),
    ],
}


@pytest.fixture()
def rollup_path(tmp_path: Path) -> Path:
    path = tmp_path / "rollup.sqlite"
    conn = sqlite3.connect(path)
    cols = ", ".join(f'"{c}"' for c in ROLLUP_COLUMNS)
    conn.execute(f"CREATE TABLE dyad_status_rollup ({cols})")
    for as_of in AS_OFS:
        conn.execute(
            f"INSERT INTO dyad_status_rollup VALUES "
            f"({', '.join('?' * len(ROLLUP_COLUMNS))})",
            ["AAA", "BBB", as_of, 0.42, "steady", 0.31, "stability",
             120, 7, 0, 0, 0, 0, 0.5, 0.6, 0.7, "fixture", 380,
             "2026-10-01T00:00:00Z"],
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
        "exc45 REAL, vintage INTEGER)"
    )
    for as_of in AS_OFS:
        conn.execute(
            "INSERT INTO dyad_predictive_summary VALUES "
            "('AAA', 'BBB', ?, 0.9984, 'escalatory')",
            (as_of,),
        )
    for set_name, rows in SET_ROWS.items():
        for as_of, mu, phi, bar, exc45 in rows:
            conn.execute(
                "INSERT INTO dyad_set_predictive VALUES "
                "('AAA', 'BBB', ?, ?, ?, ?, ?, 5.1245, ?, ?, 380)",
                (as_of, set_name, mu, phi, phi, bar, exc45),
            )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def client(rollup_path: Path, monkeypatch: pytest.MonkeyPatch,
           ) -> TestClient:
    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup_path))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def test_predictive_sets_served_grouped_and_sorted(
    client: TestClient, predictive_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PREDICTIVE_SQLITE",
                       str(predictive_path_fixture))
    resp = client.get("/pcs-series",
                      params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    sets = data["predictive_sets"]
    assert set(sets) == {"escalatory", "instability"}
    esc = sets["escalatory"]
    assert [r["as_of"] for r in esc] == AS_OFS
    assert esc[-1] == {
        "as_of": "2026-09-30", "mu": 0.5595, "phi_adopted": 12.956,
        "bar": 0.1868, "exc45": 0.9984,
    }
    # The summary attach rides the same response, untouched.
    assert data["rows"][-1]["exc45_max"] == 0.9984
    assert data["rows"][-1]["exc45_max_set"] == "escalatory"


def test_predictive_sets_absent_when_unserved(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PREDICTIVE_SQLITE", raising=False)
    resp = client.get("/pcs-series",
                      params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "predictive_sets" not in data
    # In the series projection the exc45_max key is always
    # present (build_series projects it unconditionally); unserved
    # means its value is None. The key-absence contract applies to
    # the full-row responses (dyad-status, watchlist).
    assert data["rows"][-1]["exc45_max"] is None


def test_predictive_sets_empty_for_unknown_dyad(
    client: TestClient, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty_predictive.sqlite"
    conn = sqlite3.connect(empty)
    conn.execute(
        "CREATE TABLE dyad_predictive_summary "
        "(iso1 TEXT, iso2 TEXT, as_of TEXT, exc45_max REAL, "
        "exc45_max_set TEXT)"
    )
    conn.execute(
        'CREATE TABLE dyad_set_predictive '
        '(iso1 TEXT, iso2 TEXT, as_of TEXT, "set" TEXT, mu REAL, '
        "phi_current REAL, phi_adopted REAL, k REAL, bar REAL, "
        "exc45 REAL, vintage INTEGER)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PREDICTIVE_SQLITE", str(empty))
    resp = client.get("/pcs-series",
                      params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["predictive_sets"] == {}
    assert data["rows"][-1]["exc45_max"] is None
