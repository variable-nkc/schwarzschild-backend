"""Tests for the cooling flags in the /pcs-series payload.

The cooling flags are preprocessed in the rollup build
(``flag_cooling_005`` / ``flag_cooling_010``: the 28-day
temperature fall at two thresholds, gated on a sufficient
label — seqbayes FOUNDATIONS_SPEC §2.4a). The series rows
carry the stored values (1 / 0 / None) only when the rollup
table has both columns; on a pre-cooling rollup the keys are
absent from every row — the same degradation pattern as the
census ``cooling`` block (tests/test_census_cooling.py).
Fixtures are hand-built in that file's style.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

BASE_COLUMNS = [
    "iso1", "iso2", "as_of", "label", "temperature",
    "flag_insufficient", "flag_drifting", "flag_hot", "flag_atypical",
    "config_profile", "vintage", "computed_at", "build_id",
    "fingerprint",
]
COOLING_COLUMNS = ["flag_cooling_005", "flag_cooling_010"]

# (as_of, label, temperature, cooling_005, cooling_010) for the
# dyad AAA_BBB. The last row is a sufficient label whose 28-day
# fall was not computable, so both flags are NULL there.
FIXTURE_ROWS = [
    ("2026-09-28", "hot", 0.60, 1, 1),
    ("2026-09-29", "hot", 0.55, 1, 0),
    ("2026-09-30", "steady", 0.50, None, None),
]


def _build_db(path: Path, with_cooling: bool) -> Path:
    columns = BASE_COLUMNS + (COOLING_COLUMNS if with_cooling else [])
    types = {
        "temperature": "REAL",
        "flag_insufficient": "INTEGER", "flag_drifting": "INTEGER",
        "flag_hot": "INTEGER", "flag_atypical": "INTEGER",
        "flag_cooling_005": "INTEGER", "flag_cooling_010": "INTEGER",
        "vintage": "INTEGER",
    }
    cols_sql = ", ".join(
        f'"{c}" {types.get(c, "TEXT")}' for c in columns)
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE dyad_status_rollup ({cols_sql})")
    for as_of, label, temp, c005, c010 in FIXTURE_ROWS:
        row = {c: None for c in columns}
        row.update({
            "iso1": "AAA", "iso2": "BBB", "as_of": as_of,
            "label": label, "temperature": temp,
            "flag_insufficient": 0,
            "flag_drifting": 0,
            "flag_hot": 1 if label == "hot" else 0,
            "flag_atypical": 0,
            "config_profile": "default", "vintage": 366,
            "computed_at": "2026-10-26T20:50:40+00:00",
            "build_id": "f5b7de9", "fingerprint": "test-fingerprint",
        })
        if with_cooling:
            row["flag_cooling_005"] = c005
            row["flag_cooling_010"] = c010
        conn.execute(
            f"INSERT INTO dyad_status_rollup ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})",
            [row[c] for c in columns],
        )
    conn.commit()
    conn.close()
    return path


def _client(path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ROLLUP_SQLITE", str(path))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    monkeypatch.delenv("PREDICTIVE_SQLITE", raising=False)
    return TestClient(create_app())


def test_pcs_series_carries_cooling_flags_when_columns_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        _build_db(tmp_path / "rollup.sqlite", with_cooling=True),
        monkeypatch)
    resp = client.get(
        "/pcs-series", params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert [r["as_of"] for r in rows] == [
        "2026-09-28", "2026-09-29", "2026-09-30"]
    # Stored values pass through untouched, NULL included.
    assert [(r["flag_cooling_005"], r["flag_cooling_010"])
            for r in rows] == [(1, 1), (1, 0), (None, None)]
    # The base projection is unchanged alongside.
    assert rows[0]["temperature"] == 0.60
    assert rows[0]["flag_hot"] == 1


def test_pcs_series_omits_cooling_keys_on_pre_cooling_rollup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        _build_db(tmp_path / "rollup.sqlite", with_cooling=False),
        monkeypatch)
    resp = client.get(
        "/pcs-series", params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert len(rows) == 3
    for row in rows:
        assert "flag_cooling_005" not in row
        assert "flag_cooling_010" not in row
    # The rest of the projection is served as before.
    assert rows[0]["temperature"] == 0.60
    assert rows[0]["flag_hot"] == 1
    assert rows[2]["label"] == "steady"
