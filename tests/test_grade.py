"""Tests for the D19 row grade (INTEGRATION_SPEC section 12.3).

D19 builds of the predictive file carry a per-row ``grade``
('validated' / 'indicative') on dyad_set_predictive — the
row's domain grade: 'indicative' where the row's phi took
beta_precision's fallback branch (the sparse domain, outside
the calibrated domain), else 'validated'.

Contract (mirroring the support keys): served from a D19
file -> each predictive_sets row carries ``grade`` with the
stored value; served from a pre-D19 file (no grade column)
-> the key is absent entirely, so the frontend renders
ungraded rows exactly as before.

Fixtures follow the test_predictive_sets.py pattern.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

AS_OFS = ["2026-09-29", "2026-09-30"]

ROLLUP_COLUMNS = [
    "iso1", "iso2", "as_of", "temperature", "label",
    "max_exc45", "max_exc45_set", "v_7d", "days_present_7",
    "flag_insufficient", "flag_hot", "flag_drifting",
    "flag_atypical", "anomaly_composite", "anomaly_composite_raw",
    "anomaly_max3d", "build_id", "vintage", "computed_at",
]


def _rollup_row(iso1: str, iso2: str, as_of: str) -> list:
    return [iso1, iso2, as_of, 0.42, "steady", 0.31, "stability",
            120, 7, 0, 0, 0, 0, 0.5, 0.6, 0.7, "fixture", 380,
            "2026-10-01T00:00:00Z"]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
           ) -> TestClient:
    path = tmp_path / "rollup.sqlite"
    conn = sqlite3.connect(path)
    cols = ", ".join(f'"{c}"' for c in ROLLUP_COLUMNS)
    conn.execute(f"CREATE TABLE dyad_status_rollup ({cols})")
    for as_of in AS_OFS:
        conn.execute(
            f"INSERT INTO dyad_status_rollup VALUES "
            f"({', '.join('?' * len(ROLLUP_COLUMNS))})",
            _rollup_row("AAA", "BBB", as_of))
    conn.commit()
    conn.close()
    monkeypatch.setenv("ROLLUP_SQLITE", str(path))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SETS_JSON", raising=False)
    return TestClient(create_app())


def _predictive(tmp_path: Path, with_grade: bool) -> Path:
    path = tmp_path / "predictive.sqlite"
    conn = sqlite3.connect(path)
    grade_col = ", grade TEXT" if with_grade else ""
    conn.execute(
        'CREATE TABLE dyad_set_predictive ('
        'iso1 TEXT, iso2 TEXT, as_of TEXT, "set" TEXT, mu REAL, '
        'phi_current REAL, phi_adopted REAL, k REAL, bar REAL, '
        f'exc45 REAL, vintage INTEGER, validated INTEGER{grade_col})')
    rows = [
        ("AAA", "BBB", "2026-09-29", "instability", 0.50, 30.0,
         456.0, 15.23, 0.4179, 0.61, 380, 1, "validated"),
        ("AAA", "BBB", "2026-09-30", "instability", 0.50, 50.0,
         761.6, 15.23, 0.4179, 0.99, 380, 1, "indicative"),
        ("AAA", "BBB", "2026-09-30", "escalatory", 0.10, 12.0,
         182.8, 15.23, 0.1381, 0.05, 380, 1, "validated"),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO dyad_set_predictive VALUES "
            f"({', '.join('?' * (13 if with_grade else 12))})",
            r if with_grade else r[:-1])
    conn.execute(
        "CREATE TABLE dyad_predictive_summary ("
        "iso1 TEXT, iso2 TEXT, as_of TEXT, exc45_max REAL, "
        "exc45_max_set TEXT, exc45_calm_max REAL, vintage INTEGER)")
    conn.commit()
    conn.close()
    return path


def test_grade_served_when_column_present(
        client: TestClient, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREDICTIVE_SQLITE",
                       str(_predictive(tmp_path, with_grade=True)))
    resp = client.get("/pcs-series",
                      params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    sets = resp.json()["data"]["predictive_sets"]
    inst = {r["as_of"]: r for r in sets["instability"]}
    assert inst["2026-09-29"]["grade"] == "validated"
    assert inst["2026-09-30"]["grade"] == "indicative"
    assert sets["escalatory"][0]["grade"] == "validated"


def test_grade_absent_on_pre_d19_file(
        client: TestClient, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREDICTIVE_SQLITE",
                       str(_predictive(tmp_path, with_grade=False)))
    resp = client.get("/pcs-series",
                      params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    sets = resp.json()["data"]["predictive_sets"]
    for rows in sets.values():
        for r in rows:
            assert "grade" not in r
