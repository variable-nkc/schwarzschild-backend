"""Tests for schwarzschild-backend v1.

Every test builds its own small SQLite fixture (a few
``dyad_status_rollup`` rows) in a temporary directory. No test reads
a real analytics product, and no fixture file is committed: the
fixture lives only for the duration of the test run.

The app reads ROLLUP_SQLITE and API_VERSION_MODE at request time,
so monkeypatching the environment per test is enough — no app
rebuild is needed between tests.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

SETS = (
    "escalatory",
    "stability",
    "instability",
    "deescalatory",
    "critical_incidents",
)

# Columns the app actually reads, plus the provenance columns.
# A real rollup has more columns; SELECT * passes them through
# untouched, so a subset schema is a faithful fixture here.
COLUMNS = [
    "iso1",
    "iso2",
    "as_of",
    "temperature",
    "label",
    "flag_insufficient",
    "flag_drifting",
    "flag_hot",
    "flag_atypical",
    "v_7d",
    "v_28d",
    "days_present_7",
    "max_exc45",
    "max_exc45_set",
    "anomaly_composite",
    "anomaly_composite_raw",
    "anomaly_max3d",
    "gap_flag",
    "insufficient_sets",
    "low_volume",
    "config_profile",
    "vintage",
    "computed_at",
    "build_id",
    "fingerprint",
]
for _set in SETS:
    COLUMNS += [
        f"share_r7_{_set}",
        f"dir_r7_{_set}",
        f"std_r7_{_set}",
        f"dir_r28_{_set}",
        f"std_r28_{_set}",
    ]

# Fixture column types, mirroring the real rollup: without these, a
# TEXT-only fixture would return numbers as strings and the tests
# would be testing the fixture, not the app.
REAL_COLUMNS = {
    "temperature",
    "v_7d",
    "v_28d",
    "days_present_7",
    "max_exc45",
    "max_exc45_set",
    "anomaly_composite",
    "anomaly_composite_raw",
    "anomaly_max3d",
} | {
    f"{prefix}_{name}"
    for name in SETS
    for prefix in ("share_r7", "std_r7", "std_r28")
}
INT_COLUMNS = {
    "flag_insufficient",
    "flag_drifting",
    "flag_hot",
    "flag_atypical",
    "gap_flag",
    "low_volume",
    "vintage",
}


def _col_type(name: str) -> str:
    if name in REAL_COLUMNS:
        return "REAL"
    if name in INT_COLUMNS:
        return "INTEGER"
    return "TEXT"


def _row(iso1, iso2, as_of, label, temperature, flags, anomaly=None,
         max_exc45=None, max_exc45_set=None):
    row = {c: None for c in COLUMNS}
    composite, raw, max3d = anomaly or (1.5, 1.2, 2.0)
    row.update(
        {
            "iso1": iso1,
            "iso2": iso2,
            "as_of": as_of,
            "temperature": temperature,
            "label": label,
            "max_exc45": max_exc45,
            "max_exc45_set": max_exc45_set,
            "anomaly_composite": composite,
            "anomaly_composite_raw": raw,
            "anomaly_max3d": max3d,
            "flag_insufficient": 1 if "insufficient" in flags else 0,
            "flag_drifting": 1 if "drifting" in flags else 0,
            "flag_hot": 1 if "hot" in flags else 0,
            "flag_atypical": 1 if "atypical" in flags else 0,
            "v_7d": 500.0,
            "v_28d": 2000.0,
            "days_present_7": 7.0,
            "gap_flag": 0,
            "insufficient_sets": "",
            "low_volume": 0,
            "config_profile": "default",
            "vintage": 366,
            "computed_at": "2026-10-26T20:50:40+00:00",
            "build_id": "f5b7de9",
            "fingerprint": "test-fingerprint",
        }
    )
    for name in SETS:
        row[f"share_r7_{name}"] = 0.1
        row[f"dir_r7_{name}"] = "steady"
        row[f"std_r7_{name}"] = 0.5
        row[f"dir_r28_{name}"] = "steady"
        row[f"std_r28_{name}"] = 0.5
    # One distinctive per-set value, so series tests can tell the
    # per-set block apart from a constant.
    row["dir_r7_escalatory"] = "up"
    row["share_r7_escalatory"] = 0.3
    row["std_r7_escalatory"] = 2.5
    return row


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "rollup_poc_base.sqlite"
    conn = sqlite3.connect(path)
    cols_sql = ", ".join(f'"{c}" {_col_type(c)}' for c in COLUMNS)
    # Types are loose in the fixture on purpose: sqlite is
    # dynamically typed, and the app only reads values back.
    conn.execute(f"CREATE TABLE dyad_status_rollup ({cols_sql})")
    rows = [
        # Watchlist at 2026-09-30: one dyad per label, in an order
        # deliberately different from label-priority order, so the
        # ordering test actually tests the ordering.
        _row("USA", "GBR", "2026-09-30", "steady", 0.1, ()),
        _row("USA", "FRA", "2026-09-30", "atypical", 0.2, ("atypical",)),
        _row("USA", "CAN", "2026-09-30", "drifting", 0.3, ("drifting",)),
        _row("USA", "IRN", "2026-09-30", "hot", 0.6, ("hot",),
             max_exc45=0.80, max_exc45_set="critical_incidents"),
        _row("USA", "MEX", "2026-09-30", "insufficient", 0.0, ("insufficient",)),
        # History for USA_IRN, oldest first in the fixture insert
        # order is deliberately not sorted, to test series ordering.
        # Anomaly values differ per row so series tests can tell
        # the rows apart.
        _row("USA", "IRN", "2026-09-29", "drifting", 0.4, ("drifting",),
             anomaly=(2.0, 1.8, 3.5), max_exc45=0.45,
             max_exc45_set="instability"),
        _row("USA", "IRN", "2026-09-28", "steady", 0.35, (),
             anomaly=(0.5, 0.4, 1.0), max_exc45=0.10,
             max_exc45_set="escalatory"),
    ]
    placeholders = ", ".join("?" * len(COLUMNS))
    for row in rows:
        conn.execute(
            f"INSERT INTO dyad_status_rollup ({', '.join(COLUMNS)}) "
            f"VALUES ({placeholders})",
            [row[c] for c in COLUMNS],
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ROLLUP_SQLITE", str(db_path))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def test_health_ok_and_provenance(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["db_configured"] is True
    assert body["api_version"] == "1"
    assert body["provenance"]["build_id"] == "f5b7de9"
    assert body["provenance"]["vintage"] == 366
    assert body["provenance"]["computed_at"] == "2026-10-26T20:50:40+00:00"


def test_health_without_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROLLUP_SQLITE", raising=False)
    app_client = TestClient(create_app())
    resp = app_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["db_configured"] is False
    assert body["provenance"]["build_id"] is None
    assert body["provenance"]["vintage"] is None
    assert "ROLLUP_SQLITE" in (body["provenance"]["message"] or "")


def test_dyad_status_full_row_and_provenance(client: TestClient) -> None:
    resp = client.get(
        "/dyad-status", params={"iso1": "USA", "iso2": "IRN", "as_of": "2026-09-30"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_version"] == "1"
    assert body["provenance"]["build_id"] == "f5b7de9"
    row = body["data"]
    assert row["iso1"] == "USA" and row["iso2"] == "IRN"
    assert row["as_of"] == "2026-09-30"
    assert row["label"] == "hot"
    assert row["temperature"] == 0.6
    # Full row, not a projection: per-set and support fields ride along.
    assert row["share_r7_escalatory"] == 0.3
    assert row["dir_r7_escalatory"] == "up"
    assert row["v_7d"] == 500.0
    assert row["days_present_7"] == 7.0
    # Stored INTEGER flags surface as JSON booleans.
    assert row["flag_hot"] is True
    assert row["flag_drifting"] is False


def test_dyad_status_latest_when_no_as_of(client: TestClient) -> None:
    resp = client.get("/dyad-status", params={"iso1": "USA", "iso2": "IRN"})
    assert resp.status_code == 200
    assert resp.json()["data"]["as_of"] == "2026-09-30"


def test_dyad_status_not_found(client: TestClient) -> None:
    resp = client.get(
        "/dyad-status", params={"iso1": "USA", "iso2": "XXX", "as_of": "2026-09-30"}
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["data"] is None
    assert body["error"]["code"] == "dyad_not_found"
    # Envelope still present on errors.
    assert body["api_version"] == "1"
    assert body["provenance"]["build_id"] == "f5b7de9"


def test_watchlist_hot_first_priority_order(client: TestClient) -> None:
    resp = client.get("/watchlist", params={"as_of": "2026-09-30"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["as_of"] == "2026-09-30"
    assert body["data"]["count"] == 5
    labels = [row["label"] for row in body["data"]["rows"]]
    # v1 first pass: literal label-priority order (see README).
    assert labels == ["insufficient", "hot", "drifting", "atypical", "steady"]
    # Support fields ride along on watchlist rows too.
    assert all("v_7d" in row and "days_present_7" in row for row in body["data"]["rows"])


def test_watchlist_limit_and_label_filter(client: TestClient) -> None:
    resp = client.get(
        "/watchlist", params={"as_of": "2026-09-30", "limit": 2}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 2
    assert [r["label"] for r in resp.json()["data"]["rows"]] == [
        "insufficient",
        "hot",
    ]
    resp = client.get(
        "/watchlist", params={"as_of": "2026-09-30", "labels": "hot"}
    )
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert len(rows) == 1 and rows[0]["label"] == "hot"
    assert rows[0]["iso2"] == "IRN"


def test_watchlist_latest_as_of_when_omitted(client: TestClient) -> None:
    resp = client.get("/watchlist")
    assert resp.status_code == 200
    assert resp.json()["data"]["as_of"] == "2026-09-30"


def test_watchlist_unknown_label_is_400(client: TestClient) -> None:
    resp = client.get(
        "/watchlist", params={"as_of": "2026-09-30", "labels": "bogus"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_label"


def test_pcs_series_from_rollup_history(client: TestClient) -> None:
    resp = client.get("/pcs-series", params={"iso1": "USA", "iso2": "IRN"})
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert data["dyad_id"] == "USA_IRN"
    assert data["dates"] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    assert [r["label"] for r in data["rows"]] == ["steady", "drifting", "hot"]
    assert [r["temperature"] for r in data["rows"]] == [0.35, 0.4, 0.6]
    last = data["rows"][-1]
    assert last["per_set"]["escalatory"]["direction_r7"] == "up"
    assert last["per_set"]["escalatory"]["share_r7"] == 0.3
    assert last["per_set"]["escalatory"]["std_resid_r7"] == 2.5
    assert last["per_set"]["stability"]["direction_r28"] == "steady"


def test_pcs_series_not_found(client: TestClient) -> None:
    resp = client.get("/pcs-series", params={"iso1": "USA", "iso2": "XXX"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "dyad_not_found"


def test_pcs_series_carries_stored_anomaly_fields(
    client: TestClient,
) -> None:
    resp = client.get("/pcs-series", params={"iso1": "USA", "iso2": "IRN"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert [r["anomaly_composite"] for r in rows] == [0.5, 2.0, 1.5]
    assert [r["anomaly_composite_raw"] for r in rows] == [0.4, 1.8, 1.2]
    assert [r["anomaly_max3d"] for r in rows] == [1.0, 3.5, 2.0]


def test_pcs_series_carries_stored_max_exc45(
    client: TestClient,
) -> None:
    resp = client.get("/pcs-series", params={"iso1": "USA", "iso2": "IRN"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert [r["max_exc45"] for r in rows] == [0.10, 0.45, 0.80]


def test_pcs_series_carries_stored_max_exc45_set(
    client: TestClient,
) -> None:
    resp = client.get("/pcs-series", params={"iso1": "USA", "iso2": "IRN"})
    assert resp.status_code == 200
    rows = resp.json()["data"]["rows"]
    assert [r["max_exc45_set"] for r in rows] == [
        "escalatory", "instability", "critical_incidents"]


def test_census_at_one_as_of(client: TestClient) -> None:
    resp = client.get("/census", params={"as_of": "2026-09-30"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["as_of"] == "2026-09-30"
    assert data["total"] == 5
    assert data["counts"] == {
        "insufficient": 1,
        "hot": 1,
        "drifting": 1,
        "atypical": 1,
        "steady": 1,
    }


def test_census_latest_as_of_when_omitted(client: TestClient) -> None:
    resp = client.get("/census")
    assert resp.status_code == 200
    assert resp.json()["data"]["as_of"] == "2026-09-30"


def test_census_unknown_as_of_returns_zeros_not_error(
    client: TestClient,
) -> None:
    resp = client.get("/census", params={"as_of": "2020-01-01"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert all(v == 0 for v in data["counts"].values())


def test_census_series_over_time(client: TestClient) -> None:
    resp = client.get("/census-series")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["dates"] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    rows = data["rows"]
    assert rows[0]["as_of"] == "2026-09-28"
    assert rows[0]["steady"] == 1 and rows[0]["total"] == 1
    assert rows[1]["drifting"] == 1 and rows[1]["total"] == 1
    assert rows[2]["total"] == 5
    assert rows[2]["hot"] == 1 and rows[2]["insufficient"] == 1


def test_version_strict_mismatch_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    resp = client.get(
        "/dyad-status",
        params={
            "iso1": "USA",
            "iso2": "IRN",
            "as_of": "2026-09-30",
            "api_version": "2",
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["data"] is None
    assert body["error"]["code"] == "version_mismatch"
    assert "expected api_version 2" in body["error"]["message"]
    assert "served api_version 1" in body["error"]["message"]
    assert body["error"]["expected"] == "2"
    assert body["error"]["served"] == "1"


def test_version_warn_continues_with_warning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_VERSION_MODE", "warn")
    resp = client.get(
        "/dyad-status",
        params={
            "iso1": "USA",
            "iso2": "IRN",
            "as_of": "2026-09-30",
            "api_version": "2",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["label"] == "hot"
    warnings = body["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["code"] == "version_mismatch"
    assert warnings[0]["expected"] == "2"
    assert warnings[0]["served"] == "1"


def test_version_match_has_no_version_warning(client: TestClient) -> None:
    resp = client.get(
        "/dyad-status",
        params={
            "iso1": "USA",
            "iso2": "IRN",
            "as_of": "2026-09-30",
            "api_version": "1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["warnings"] == []


def test_data_endpoints_without_db_return_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ROLLUP_SQLITE", raising=False)
    app_client = TestClient(create_app())
    resp = app_client.get(
        "/dyad-status", params={"iso1": "USA", "iso2": "IRN", "as_of": "2026-09-30"}
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "db_not_configured"
    assert body["provenance"]["build_id"] is None
