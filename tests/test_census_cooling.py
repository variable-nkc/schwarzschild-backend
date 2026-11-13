"""Tests for the census cooling block (LC-03).

The cooling flags are preprocessed in the rollup build
(``flag_cooling_005`` / ``flag_cooling_010``: the 28-day
temperature fall at two thresholds, gated on a sufficient
label). The census payload carries a ``cooling`` block only
when the rollup table has both columns; on a pre-cooling
rollup the key is absent entirely. Fixtures are hand-counted:
every expected number below is computed from the fixture rows
by hand, in the style of tests/test_api.py.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

AS_OF = "2026-09-30"

BASE_COLUMNS = [
    "iso1", "iso2", "as_of", "label",
    "flag_insufficient", "flag_drifting", "flag_hot", "flag_atypical",
    "config_profile", "vintage", "computed_at", "build_id",
    "fingerprint",
]
COOLING_COLUMNS = ["flag_cooling_005", "flag_cooling_010"]

# (iso2, label, flags, cooling_005, cooling_010); iso1 is "AAA"
# throughout. Cooling values are 1 / 0 / None (NULL: insufficient
# rows by construction, and row III — a sufficient label whose
# temperature fall was not computable).
FIXTURE_ROWS = [
    ("BBB", "hot", ("hot",), 1, 1),
    ("CCC", "hot", ("hot", "drifting"), 1, 0),
    ("DDD", "drifting", ("drifting",), 0, 0),
    ("EEE", "drifting", ("drifting", "atypical"), 1, 1),
    ("FFF", "atypical", ("atypical",), 1, 0),
    ("GGG", "steady", (), 0, 0),
    ("HHH", "steady", (), 1, 0),
    ("III", "hot", ("hot",), None, None),
    ("JJJ", "insufficient", ("insufficient",), None, None),
    ("KKK", "insufficient", ("insufficient",), None, None),
]


def _build_db(path: Path, with_cooling: bool) -> Path:
    columns = BASE_COLUMNS + (COOLING_COLUMNS if with_cooling else [])
    types = {
        "flag_insufficient": "INTEGER", "flag_drifting": "INTEGER",
        "flag_hot": "INTEGER", "flag_atypical": "INTEGER",
        "flag_cooling_005": "INTEGER", "flag_cooling_010": "INTEGER",
        "vintage": "INTEGER",
    }
    cols_sql = ", ".join(
        f'"{c}" {types.get(c, "TEXT")}' for c in columns)
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE dyad_status_rollup ({cols_sql})")
    for iso2, label, flags, c005, c010 in FIXTURE_ROWS:
        row = {c: None for c in columns}
        row.update({
            "iso1": "AAA", "iso2": iso2, "as_of": AS_OF,
            "label": label,
            "flag_insufficient": 1 if "insufficient" in flags else 0,
            "flag_drifting": 1 if "drifting" in flags else 0,
            "flag_hot": 1 if "hot" in flags else 0,
            "flag_atypical": 1 if "atypical" in flags else 0,
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
    return TestClient(create_app())


def test_census_cooling_block_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        _build_db(tmp_path / "rollup.sqlite", with_cooling=True),
        monkeypatch)
    resp = client.get("/census", params={"as_of": AS_OF})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 10
    assert data["counts"] == {
        "insufficient": 2, "hot": 3, "drifting": 2,
        "atypical": 1, "steady": 2,
    }
    cooling = data["cooling"]
    assert cooling["labels"] == {
        "insufficient": {"n": 2, "cooling_005": 0, "cooling_010": 0},
        "hot": {"n": 3, "cooling_005": 2, "cooling_010": 1},
        "drifting": {"n": 2, "cooling_005": 1, "cooling_010": 1},
        "atypical": {"n": 1, "cooling_005": 1, "cooling_010": 0},
        "steady": {"n": 2, "cooling_005": 1, "cooling_010": 0},
    }
    # Nested thresholds: the 0.10 set is a subset of the 0.05 set.
    for entry in cooling["labels"].values():
        assert entry["cooling_010"] <= entry["cooling_005"]
    # Also-flagged among the 5 sufficient cooling dyads: hot on
    # BBB+CCC, drifting on CCC+EEE, atypical on EEE+FFF.
    assert cooling["also_flagged_005"] == {
        "hot": 2, "drifting": 2, "atypical": 2}
    cross = cooling["cross"]
    assert len(cross) == 8
    assert sum(r["n"] for r in cross) == 8  # sufficient total
    assert sum(r["cooling_005"] for r in cross) == 5
    by_combo = {
        (r["hot"], r["drifting"], r["atypical"]): r for r in cross}
    assert by_combo[(False, False, False)] == {
        "hot": False, "drifting": False, "atypical": False,
        "n": 2, "cooling_005": 1}
    assert by_combo[(True, False, False)]["n"] == 2  # BBB + III
    assert by_combo[(True, False, False)]["cooling_005"] == 1
    assert by_combo[(True, True, False)]["cooling_005"] == 1
    assert by_combo[(False, True, True)]["cooling_005"] == 1
    assert by_combo[(False, True, False)]["cooling_005"] == 0
    assert by_combo[(True, True, True)]["n"] == 0


def test_census_cooling_block_empty_as_of_is_zeroed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        _build_db(tmp_path / "rollup.sqlite", with_cooling=True),
        monkeypatch)
    resp = client.get("/census", params={"as_of": "2020-01-01"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    cooling = data["cooling"]
    assert all(
        entry == {"n": 0, "cooling_005": 0, "cooling_010": 0}
        for entry in cooling["labels"].values())
    assert cooling["also_flagged_005"] == {
        "hot": 0, "drifting": 0, "atypical": 0}
    assert sum(r["n"] for r in cooling["cross"]) == 0


def test_census_without_cooling_columns_has_no_cooling_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(
        _build_db(tmp_path / "rollup.sqlite", with_cooling=False),
        monkeypatch)
    resp = client.get("/census", params={"as_of": AS_OF})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "cooling" not in data
    assert data["total"] == 10
    assert data["counts"]["hot"] == 3
