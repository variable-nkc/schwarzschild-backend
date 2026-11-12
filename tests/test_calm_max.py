"""Tests for the D18 calming-side attach (``exc45_calm_max``).

The D18 summary carries, alongside the trio pill, the max
exceedance over the calming pair (stability, deescalatory) so
the frontend can mark dyads where both sides are elevated. The
value annotates the pill; it never alters it.

Contract, mirroring ``exc45_max`` one level down: predictive
unserved -> no calm key; served from a pre-D18 file (summary
table without the column) -> no calm key; served from a D18
file -> key present, the stored value, or None when the dyad
has no summary row at the as-of or the stored value is NULL
(neither calming set emitted — absence is silence).

Fixtures follow the test_predictive_sets.py pattern.
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
                _rollup_row(*dyad, as_of),
            )
    conn.commit()
    conn.close()
    return path


def _predictive(path: Path, with_calm: bool) -> Path:
    conn = sqlite3.connect(path)
    if with_calm:
        conn.execute(
            "CREATE TABLE dyad_predictive_summary "
            "(iso1 TEXT, iso2 TEXT, as_of TEXT, exc45_max REAL, "
            "exc45_max_set TEXT, exc45_calm_max REAL, "
            "vintage INTEGER)")
        # AAA_BBB: calm NULL at 09-28 (neither calming set
        # emitted), 0.85 afterwards. CCC_DDD: no rows at all.
        for as_of, calm in (("2026-09-28", None),
                            ("2026-09-29", 0.85),
                            ("2026-09-30", 0.85)):
            conn.execute(
                "INSERT INTO dyad_predictive_summary VALUES "
                "('AAA', 'BBB', ?, 0.91, 'instability', ?, 380)",
                (as_of, calm))
    else:
        conn.execute(
            "CREATE TABLE dyad_predictive_summary "
            "(iso1 TEXT, iso2 TEXT, as_of TEXT, exc45_max REAL, "
            "exc45_max_set TEXT, vintage INTEGER)")
        for as_of in AS_OFS:
            conn.execute(
                "INSERT INTO dyad_predictive_summary VALUES "
                "('AAA', 'BBB', ?, 0.91, 'instability', 380)",
                (as_of,))
    conn.execute(
        'CREATE TABLE dyad_set_predictive '
        '(iso1 TEXT, iso2 TEXT, as_of TEXT, "set" TEXT, mu REAL, '
        "phi_current REAL, phi_adopted REAL, k REAL, bar REAL, "
        "exc45 REAL, vintage INTEGER, validated INTEGER)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def client(rollup_path: Path, monkeypatch: pytest.MonkeyPatch,
           ) -> TestClient:
    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup_path))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SETS_JSON", raising=False)
    return TestClient(create_app())


def test_calm_max_served_from_d18_file(
    client: TestClient, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PREDICTIVE_SQLITE",
        str(_predictive(tmp_path / "pred.sqlite", True)))
    resp = client.get("/dyad-status", params={
        "iso1": "AAA", "iso2": "BBB", "as_of": "2026-09-30"})
    assert resp.status_code == 200
    row = resp.json()["data"]
    assert row["exc45_max"] == 0.91
    assert row["exc45_calm_max"] == 0.85
    # NULL in the file (no calming row emitted) reads as None,
    # with the key present.
    resp = client.get("/dyad-status", params={
        "iso1": "AAA", "iso2": "BBB", "as_of": "2026-09-28"})
    row = resp.json()["data"]
    assert "exc45_calm_max" in row and row["exc45_calm_max"] is None
    # Served-but-sparse dyad: no summary row -> key present, None.
    resp = client.get("/dyad-status", params={
        "iso1": "CCC", "iso2": "DDD", "as_of": "2026-09-30"})
    row = resp.json()["data"]
    assert row["exc45_max"] is None
    assert "exc45_calm_max" in row and row["exc45_calm_max"] is None


def test_calm_max_absent_from_pred18_file(
    client: TestClient, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PREDICTIVE_SQLITE",
        str(_predictive(tmp_path / "pred.sqlite", False)))
    resp = client.get("/dyad-status", params={
        "iso1": "AAA", "iso2": "BBB", "as_of": "2026-09-30"})
    assert resp.status_code == 200
    row = resp.json()["data"]
    assert row["exc45_max"] == 0.91
    assert "exc45_calm_max" not in row


def test_calm_max_in_series_projection(
    client: TestClient, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PREDICTIVE_SQLITE",
        str(_predictive(tmp_path / "pred.sqlite", True)))
    resp = client.get("/pcs-series",
                      params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    by_as_of = {r["as_of"]: r for r in rows}
    assert by_as_of["2026-09-30"]["exc45_calm_max"] == 0.85
    assert by_as_of["2026-09-28"]["exc45_calm_max"] is None
