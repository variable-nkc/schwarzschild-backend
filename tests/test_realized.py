"""Tests for the realized 45d outcome (``realized_45d``).

Each per-set predictive row gains the outcome statistic the D17
sweep validated coverage against: the pooled trailing-90d share
(Σ set obs / Σ total obs) evaluated at as_of + 45d — the same S0m
the Beta predictive forecasts, at the horizon date. It is served
only when the DDA pair (DDA_SQLITE + DDA_SETS_JSON) is
configured, under the same contract as the support keys: pair
unconfigured -> the key is absent; pair configured -> the value
is the realized share, or None when the horizon has not elapsed
by the sidecar's last date or the realized window has no volume.

Fixtures follow the test_predictive_sets.py / test_set_support.py
patterns. All expected values are hand-computed from the fixture
counts.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import _realized_at

ROLLUP_COLUMNS = [
    "iso1", "iso2", "as_of", "temperature", "label",
    "max_exc45", "max_exc45_set", "v_7d", "days_present_7",
    "flag_insufficient", "flag_hot", "flag_drifting",
    "flag_atypical", "anomaly_composite", "anomaly_composite_raw",
    "anomaly_max3d", "build_id", "vintage", "computed_at",
]

# Fixture set membership. Code 192 is deliberately in both
# instability and critical_incidents (as in production, where the
# CI codes are a subset of instability's): it must count toward
# both numerators.
SETS_JSON = {
    "escalatory": ["190"],
    "instability": ["191", "192"],
    "stability": ["010"],
    "deescalatory": ["020"],
    "critical_incidents": ["192"],
}

# Sidecar contents. The sidecar's last date is 2026-09-30, so a
# predictive row at 2026-08-16 has its realized window exactly
# [2026-07-03, 2026-09-30] (trailing 90d ending at as_of + 45d),
# and a row at 2026-08-17 has not elapsed.
# AAA_BBB window totals: vol 40 + 10 + 20 = 70.
DDA_ROWS = [
    ("AAA", "BBB", "2026-07-10", "190", 10),
    ("AAA", "BBB", "2026-07-10", "999", 30),   # unmapped: vol only
    ("AAA", "BBB", "2026-08-01", "192", 6),
    ("AAA", "BBB", "2026-08-01", "191", 4),
    ("AAA", "BBB", "2026-09-30", "190", 10),
    ("AAA", "BBB", "2026-09-30", "010", 10),
    # CCC_DDD reports once, long before the realized window.
    ("CCC", "DDD", "2026-01-05", "190", 5),
]

AS_OFS = ["2026-08-16", "2026-08-17"]
SET_NAMES = ["escalatory", "instability", "stability",
             "critical_incidents"]


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
                [*dyad, as_of, 0.42, "steady", 0.31, "stability",
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
        "exc45 REAL, vintage INTEGER, validated INTEGER)"
    )
    for dyad in (("AAA", "BBB"), ("CCC", "DDD")):
        for as_of in AS_OFS:
            for name in SET_NAMES:
                conn.execute(
                    "INSERT INTO dyad_set_predictive VALUES "
                    "(?, ?, ?, ?, 0.25, 10.0, 10.0, 5.0, 0.2, "
                    "0.5, 380, 1)",
                    (*dyad, as_of, name),
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


def _sets(client: TestClient, iso1: str, iso2: str) -> dict:
    resp = client.get("/pcs-series", params={"iso1": iso1,
                                             "iso2": iso2})
    assert resp.status_code == 200
    return resp.json()["data"]["predictive_sets"]


def test_realized_values_served(
    client: TestClient, predictive_path_fixture: Path,
    dda_path_fixture: Path, sets_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_all(client, predictive_path_fixture, dda_path_fixture,
               sets_path_fixture, monkeypatch)
    sets = _sets(client, "AAA", "BBB")
    at = {name: {r["as_of"]: r for r in rows}
          for name, rows in sets.items()}
    # Window [2026-07-03, 2026-09-30], total volume 70.
    assert at["escalatory"]["2026-08-16"]["realized_45d"] == \
        pytest.approx(20 / 70)
    assert at["stability"]["2026-08-16"]["realized_45d"] == \
        pytest.approx(10 / 70)
    assert at["instability"]["2026-08-16"]["realized_45d"] == \
        pytest.approx(10 / 70)
    # The shared 192 code counts toward CI as well as instability.
    assert at["critical_incidents"]["2026-08-16"]["realized_45d"] \
        == pytest.approx(6 / 70)
    # 2026-08-17 + 45d = 2026-10-01 is past the sidecar's last
    # date: not yet observable, for every set.
    for name in SET_NAMES:
        assert at[name]["2026-08-17"]["realized_45d"] is None


def test_realized_none_when_window_has_no_volume(
    client: TestClient, predictive_path_fixture: Path,
    dda_path_fixture: Path, sets_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_all(client, predictive_path_fixture, dda_path_fixture,
               sets_path_fixture, monkeypatch)
    sets = _sets(client, "CCC", "DDD")
    row = {r["as_of"]: r for r in sets["escalatory"]}
    # The horizon has elapsed globally, but CCC_DDD has no records
    # at all in the realized window: undefined, not zero.
    assert row["2026-08-16"]["realized_45d"] is None


def test_realized_absent_without_dda_pair(
    client: TestClient, predictive_path_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PREDICTIVE_SQLITE",
                       str(predictive_path_fixture))
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SETS_JSON", raising=False)
    sets = _sets(client, "AAA", "BBB")
    for rows in sets.values():
        for r in rows:
            assert "realized_45d" not in r


def test_realized_at_pure() -> None:
    dates = ["2026-07-10", "2026-09-30"]
    vol = [0.0, 40.0, 60.0]
    num = [0.0, 10.0, 30.0]
    # as_of + 45d == max date exactly: elapsed, computed.
    assert _realized_at(dates, vol, num, "2026-08-16",
                        "2026-09-30") == pytest.approx(0.5)
    # One day later: past the data.
    assert _realized_at(dates, vol, num, "2026-08-17",
                        "2026-09-30") is None
    # Elapsed but zero volume in the window.
    assert _realized_at(dates, [0.0, 0.0, 0.0], num, "2026-08-16",
                        "2026-09-30") is None
    # No sidecar at all.
    assert _realized_at(dates, vol, num, "2026-08-16",
                        None) is None
