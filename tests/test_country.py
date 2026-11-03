"""Tests for GET /dev/country-aggregates (dev/dda-sidecar only).

Same discipline as test_hybrid.py: small SQLite fixtures in a
temporary directory, monkeypatched environment, no real
products. Covers the five choropleth metrics, the involvement
view, the prior-slice rule, and the geometry join — not the
shape as contract (formalization does that).
"""

import json
import math
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

BOX = "{[[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]}"

# (iso1, iso2, v_7d, temperature, label)
CURRENT = [
    ("USA", "CAN", 100, 0.5, "hot"),
    ("USA", "MEX", 50, 0.2, "steady"),
    ("CAN", "MEX", 30, 0.4, "drifting"),
    ("FRA", "DEU", 10, 0.9, "insufficient"),
    ("GBR", "FRA", 40, 0.3, "hot"),
    ("ITA", "ESP", 20, 0.6, "steady"),
]
PRIOR = [
    ("USA", "CAN", 50, 0.5, "hot"),
    ("USA", "MEX", 50, 0.2, "steady"),
    ("CAN", "MEX", 60, 0.4, "drifting"),
    ("FRA", "DEU", 20, 0.9, "insufficient"),
    ("GBR", "FRA", 40, 0.3, "hot"),
    # ITA_ESP absent from the prior slice: prior volume zero.
]


@pytest.fixture()
def country_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    rollup = tmp_path / "rollup.sqlite"
    con = sqlite3.connect(rollup)
    con.execute(
        "CREATE TABLE dyad_status_rollup (iso1 TEXT, iso2 TEXT,"
        " as_of TEXT, v_7d REAL, temperature REAL, label TEXT)"
    )
    for row in CURRENT:
        con.execute(
            "INSERT INTO dyad_status_rollup VALUES (?, ?, '2026-09-30',"
            " ?, ?, ?)", row)
    for row in PRIOR:
        con.execute(
            "INSERT INTO dyad_status_rollup VALUES (?, ?, '2026-09-23',"
            " ?, ?, ?)", row)
    con.commit()
    con.close()

    mirror = tmp_path / "mirror.sqlite"
    con = sqlite3.connect(mirror)
    con.execute(
        "CREATE TABLE gdelt_codes_country (iso_code TEXT,"
        " full_name TEXT, polygon_type TEXT, bounding_box TEXT)"
    )
    for iso, name in (("USA", "United States"), ("CAN", "Canada"),
                      ("MEX", "Mexico"), ("FRA", "France"),
                      ("DEU", "Germany"), ("GBR", "United Kingdom"),
                      ("ITA", "Italy"), ("ESP", "Spain")):
        con.execute(
            "INSERT INTO gdelt_codes_country VALUES (?, ?,"
            " 'MultiPolygon', ?)", (iso, name, BOX))
    # SSD-like: valid polygon, null name -> falls back to the code.
    con.execute(
        "INSERT INTO gdelt_codes_country VALUES ('SSD', NULL,"
        " 'MultiPolygon', ?)", (BOX,))
    # Null box and null code rows are skipped, never rendered.
    con.execute(
        "INSERT INTO gdelt_codes_country VALUES ('XXX', 'No Box',"
        " 'MultiPolygon', NULL)")
    con.execute(
        "INSERT INTO gdelt_codes_country VALUES (NULL, 'No Code',"
        " 'MultiPolygon', ?)", (BOX,))
    con.commit()
    con.close()

    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup))
    monkeypatch.setenv("MIRROR_SQLITE", str(mirror))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def _by_country(data: dict) -> dict:
    return {c["country"]: c for c in data["countries"]}


def test_country_aggregates_metrics(country_env: TestClient) -> None:
    r = country_env.get("/dev/country-aggregates",
                        params={"as_of": "2026-09-30"})
    assert r.status_code == 200
    body = r.json()
    assert body["provenance"]["message"] == \
        "DEV ONLY — not part of the v1 contract."
    data = body["data"]
    assert data["status"] == "ok"
    assert data["as_of"] == "2026-09-30"
    assert data["prior_as_of"] == "2026-09-23"
    by = _by_country(data)

    # USA: involvement over USA_CAN + USA_MEX.
    usa = by["USA"]
    assert usa["vol_now"] == pytest.approx(150)
    assert usa["vol_prior"] == pytest.approx(100)
    assert usa["vol_ratio"] == pytest.approx(1.5)
    assert usa["vol_change_log2"] == pytest.approx(math.log2(1.5))
    assert usa["log10_vol"] == pytest.approx(math.log10(150))
    # p75 of [0.2, 0.5], linear: 0.425.
    assert usa["temp_p75"] == pytest.approx(0.425)
    assert usa["n_temp"] == 2
    # Mode tie (hot 1, steady 1) breaks by priority: hot.
    assert usa["mode_label"] == "hot"
    assert usa["n_sufficient"] == 2 and usa["n_dyads"] == 2
    assert usa["share_flagged"] == pytest.approx(0.5)

    # CAN: vol 130/110, temps [0.5, 0.4] -> p75 0.475, all flagged.
    can = by["CAN"]
    assert can["vol_change_log2"] == pytest.approx(math.log2(130 / 110))
    assert can["temp_p75"] == pytest.approx(0.475)
    assert can["mode_label"] == "hot"
    assert can["share_flagged"] == pytest.approx(1.0)

    # FRA: FRA_DEU (v 10 < 30, insufficient) contributes volume
    # and a dyad, but no temperature and no mode vote.
    fra = by["FRA"]
    assert fra["vol_now"] == pytest.approx(50)
    assert fra["temp_p75"] == pytest.approx(0.3)
    assert fra["n_temp"] == 1
    assert fra["mode_label"] == "hot"
    assert fra["n_sufficient"] == 1 and fra["n_dyads"] == 2
    assert fra["share_flagged"] == pytest.approx(0.5)

    # DEU: only the insufficient dyad -> no mode, no temp.
    deu = by["DEU"]
    assert deu["mode_label"] is None
    assert deu["n_sufficient"] == 0
    assert deu["temp_p75"] is None and deu["n_temp"] == 0
    assert deu["share_flagged"] == pytest.approx(0.0)

    # ITA: absent from the prior slice -> change null, not infinite.
    ita = by["ITA"]
    assert ita["vol_prior"] == pytest.approx(0)
    assert ita["vol_change_log2"] is None
    assert ita["vol_ratio"] is None
    assert ita["log10_vol"] == pytest.approx(math.log10(20))

    # Geometry: real codes render, null-box / null-code rows do not,
    # null names fall back to the code.
    ids = {f["id"]: f["properties"]["name"]
           for f in data["geometry"]["features"]}
    assert ids["USA"] == "United States"
    assert ids["SSD"] == "SSD"
    assert "XXX" not in ids and None not in ids


def test_country_aggregates_prior_missing(country_env: TestClient) -> None:
    # as_of 2026-09-25 resolves to the 09-23 slice; its prior
    # (09-16) has no slice, so every change is null.
    r = country_env.get("/dev/country-aggregates",
                        params={"as_of": "2026-09-25"})
    data = r.json()["data"]
    assert data["as_of"] == "2026-09-23"
    assert data["prior_as_of"] is None
    assert all(c["vol_change_log2"] is None
               for c in data["countries"])


def test_country_aggregates_missing_mirror_503(
        country_env: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIRROR_SQLITE")
    r = country_env.get("/dev/country-aggregates",
                        params={"as_of": "2026-09-30"})
    assert r.status_code == 503


def test_simplify_ring_keeps_endpoints_and_reduces() -> None:
    from app import country_store

    # Zigzag with tiny amplitude: interior points within
    # tolerance collapse; a closed ring stays closed. (The
    # first==last segment is degenerate, so DP also keeps the
    # farthest point — a closed loop cannot collapse to a
    # single segment and back.)
    ring = [[float(i), 0.01 if i % 2 else 0.0] for i in range(21)]
    ring.append(ring[0])
    simple = country_store.simplify_ring(ring, tol=0.1)
    assert simple == [[0.0, 0.0], [20.0, 0.0], [0.0, 0.0]]
    # A sharp spike above tolerance survives.
    ring2 = [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [10.0, 0.0],
             [0.0, 0.0]]
    simple2 = country_store.simplify_ring(ring2, tol=0.1)
    assert [5.0, 5.0] in simple2
    # Rounding to 3 decimals.
    ring3 = [[0.0, 0.0], [1.123456, 2.654321], [2.0, 0.0], [0.0, 0.0]]
    simple3 = country_store.simplify_ring(ring3, tol=0.0)
    assert simple3[1] == [1.123, 2.654]
    print("PASS: simplify_ring endpoints, reduction, rounding")
