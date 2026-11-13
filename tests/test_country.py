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


def test_country_aggregates_n_flagged(
        country_env: TestClient,
        monkeypatch: pytest.MonkeyPatch) -> None:
    # n_flagged rides alongside share_flagged: the count behind
    # the share (hot/drifting/atypical, involvement view).
    # PREDICTIVE_SQLITE is cleared so the pill assertion below
    # is hermetic (a developer .env may well configure it).
    monkeypatch.delenv("PREDICTIVE_SQLITE", raising=False)
    r = country_env.get("/dev/country-aggregates",
                        params={"as_of": "2026-09-30"})
    by = _by_country(r.json()["data"])
    assert by["USA"]["n_flagged"] == 1   # USA_CAN hot; USA_MEX steady
    assert by["CAN"]["n_flagged"] == 2   # hot + drifting
    assert by["MEX"]["n_flagged"] == 1   # CAN_MEX drifting
    assert by["FRA"]["n_flagged"] == 1   # GBR_FRA hot
    assert by["DEU"]["n_flagged"] == 0   # insufficient only
    assert by["ITA"]["n_flagged"] == 0   # steady only
    # pill_max is emitted on every row even with no predictive
    # dict behind it (value None, key present).
    assert all(c["pill_max"] is None for c in by.values())


def test_country_aggregates_scoped_dyads(country_env: TestClient) -> None:
    # Scoping restricts BOTH slices: vol_change is computed
    # from scoped volumes only.
    r = country_env.get(
        "/dev/country-aggregates",
        params={"as_of": "2026-09-30", "dyads": "USA_CAN,CAN_MEX"})
    assert r.status_code == 200
    by = _by_country(r.json()["data"])
    assert set(by) == {"USA", "CAN", "MEX"}
    usa = by["USA"]
    assert usa["vol_now"] == pytest.approx(100)
    assert usa["vol_prior"] == pytest.approx(50)
    assert usa["vol_change_log2"] == pytest.approx(math.log2(2.0))
    assert usa["n_dyads"] == 1 and usa["n_flagged"] == 1
    can = by["CAN"]
    assert can["vol_now"] == pytest.approx(130)
    assert can["vol_prior"] == pytest.approx(110)
    assert can["vol_change_log2"] == pytest.approx(math.log2(130 / 110))
    assert can["n_dyads"] == 2
    mex = by["MEX"]
    assert mex["vol_now"] == pytest.approx(30)
    assert mex["vol_prior"] == pytest.approx(60)
    assert mex["n_dyads"] == 1


def test_country_aggregates_views(country_env: TestClient) -> None:
    r = country_env.get(
        "/dev/country-aggregates",
        params={"as_of": "2026-09-30", "view": "targets"})
    assert r.status_code == 200
    by = _by_country(r.json()["data"])
    # Only iso2 slots are fed: pure sources never appear.
    assert set(by) == {"CAN", "MEX", "DEU", "FRA", "ESP"}
    assert by["CAN"]["vol_now"] == pytest.approx(100)
    assert by["CAN"]["n_dyads"] == 1
    assert by["MEX"]["vol_now"] == pytest.approx(80)
    assert by["MEX"]["n_dyads"] == 2
    assert by["DEU"]["vol_now"] == pytest.approx(10)

    r = country_env.get(
        "/dev/country-aggregates",
        params={"as_of": "2026-09-30", "view": "sources"})
    assert r.status_code == 200
    by = _by_country(r.json()["data"])
    assert set(by) == {"USA", "CAN", "FRA", "GBR", "ITA"}
    assert by["USA"]["vol_now"] == pytest.approx(150)
    assert by["USA"]["n_dyads"] == 2
    assert by["FRA"]["vol_now"] == pytest.approx(10)
    assert by["FRA"]["n_dyads"] == 1

    r = country_env.get(
        "/dev/country-aggregates",
        params={"as_of": "2026-09-30", "view": "sideways"})
    assert r.status_code == 400


def _predictive_file(tmp_path: Path) -> Path:
    pred = tmp_path / "predictive.sqlite"
    con = sqlite3.connect(pred)
    con.execute(
        "CREATE TABLE dyad_predictive_summary (iso1 TEXT,"
        " iso2 TEXT, as_of TEXT, exc45_max REAL)")
    for row in (("USA", "CAN", 0.9), ("USA", "MEX", 0.4),
                ("CAN", "MEX", 0.7)):
        con.execute(
            "INSERT INTO dyad_predictive_summary VALUES"
            " (?, ?, '2026-09-30', ?)", row)
    # A row at another as-of must not leak into this slice.
    con.execute(
        "INSERT INTO dyad_predictive_summary VALUES"
        " ('FRA', 'DEU', '2026-09-23', 0.99)")
    con.commit()
    con.close()
    return pred


def test_country_aggregates_pill_max(
        country_env: TestClient, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREDICTIVE_SQLITE",
                       str(_predictive_file(tmp_path)))
    r = country_env.get("/dev/country-aggregates",
                        params={"as_of": "2026-09-30"})
    assert r.status_code == 200
    by = _by_country(r.json()["data"])
    assert by["USA"]["pill_max"] == pytest.approx(0.9)
    assert by["CAN"]["pill_max"] == pytest.approx(0.9)
    assert by["MEX"]["pill_max"] == pytest.approx(0.7)
    # FRA's dyads have no summary row at this as-of.
    assert by["FRA"]["pill_max"] is None
    assert by["DEU"]["pill_max"] is None
    # Scoped + targets view: the max runs over the slot's dyads.
    r = country_env.get(
        "/dev/country-aggregates",
        params={"as_of": "2026-09-30", "view": "targets",
                "dyads": "USA_CAN,USA_MEX,CAN_MEX"})
    by = _by_country(r.json()["data"])
    assert by["CAN"]["pill_max"] == pytest.approx(0.9)
    assert by["MEX"]["pill_max"] == pytest.approx(0.7)
    assert "USA" not in by


def test_country_aggregates_pill_degrades(
        country_env: TestClient, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Unset: pill_max present but null, endpoint still serves.
    monkeypatch.delenv("PREDICTIVE_SQLITE", raising=False)
    r = country_env.get("/dev/country-aggregates",
                        params={"as_of": "2026-09-30"})
    assert r.status_code == 200
    assert all(c["pill_max"] is None
               for c in r.json()["data"]["countries"])
    # Configured but the file does not exist: same degradation.
    monkeypatch.setenv("PREDICTIVE_SQLITE",
                       str(tmp_path / "nope.sqlite"))
    r = country_env.get("/dev/country-aggregates",
                        params={"as_of": "2026-09-30"})
    assert r.status_code == 200
    assert all(c["pill_max"] is None
               for c in r.json()["data"]["countries"])
    # Configured, file exists, summary table absent: same.
    bare = tmp_path / "bare.sqlite"
    con = sqlite3.connect(bare)
    con.execute("CREATE TABLE something_else (x INTEGER)")
    con.commit()
    con.close()
    monkeypatch.setenv("PREDICTIVE_SQLITE", str(bare))
    r = country_env.get("/dev/country-aggregates",
                        params={"as_of": "2026-09-30"})
    assert r.status_code == 200
    assert all(c["pill_max"] is None
               for c in r.json()["data"]["countries"])


def test_fetch_country_aggregates_pill_by_dyad(tmp_path: Path) -> None:
    # Store level: the max runs over exactly the dyads feeding
    # each slot; no dict (or a dyad missing from it) -> None.
    from app import country_store

    rollup = tmp_path / "rollup.sqlite"
    con = sqlite3.connect(rollup)
    con.execute(
        "CREATE TABLE dyad_status_rollup (iso1 TEXT, iso2 TEXT,"
        " as_of TEXT, v_7d REAL, temperature REAL, label TEXT)")
    for row in CURRENT:
        con.execute(
            "INSERT INTO dyad_status_rollup VALUES (?, ?,"
            " '2026-09-30', ?, ?, ?)", row)
    con.commit()
    con.close()
    mirror = tmp_path / "mirror.sqlite"
    con = sqlite3.connect(mirror)
    con.execute(
        "CREATE TABLE gdelt_codes_country (iso_code TEXT,"
        " full_name TEXT, polygon_type TEXT, bounding_box TEXT)")
    con.execute(
        "INSERT INTO gdelt_codes_country VALUES ('USA',"
        " 'United States', 'MultiPolygon', ?)", (BOX,))
    con.commit()
    con.close()

    rconn = country_store.connect(rollup)
    mconn = country_store.connect(mirror)
    try:
        data = country_store.fetch_country_aggregates(
            rconn, mconn, "2026-09-30",
            pill_by_dyad={("USA", "CAN"): 0.9, ("CAN", "MEX"): 0.7})
        by = _by_country(data)
        assert by["USA"]["pill_max"] == pytest.approx(0.9)
        assert by["CAN"]["pill_max"] == pytest.approx(0.9)
        assert by["MEX"]["pill_max"] == pytest.approx(0.7)
        assert by["FRA"]["pill_max"] is None
        data = country_store.fetch_country_aggregates(
            rconn, mconn, "2026-09-30")
        assert all(c["pill_max"] is None for c in data["countries"])
    finally:
        rconn.close()
        mconn.close()
        country_store._GEOMETRY_CACHE.clear()


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


def test_rewind_ring_orientation() -> None:
    """2026-11-03 choropleth blob: a reversed ring renders
    in d3-geo as the sphere minus the polygon. Exteriors
    must come out positive-area, holes negative-area,
    closure preserved, antimeridian rings untouched when
    already positive."""
    from app import country_store

    # Vietnam's island triangle, verbatim from the served
    # payload: negative spherical area, rendered as a
    # world-covering complement until rewound.
    tri = [[106.211, 9.621], [106.284, 9.544],
           [106.093, 9.761], [106.211, 9.621]]
    assert country_store._spherical_signed_area(tri) < 0
    fixed = country_store._rewind_ring(tri, exterior=True)
    assert country_store._spherical_signed_area(fixed) > 0
    assert fixed[0] == fixed[-1] == tri[0]
    assert sorted(map(tuple, fixed)) == sorted(map(tuple, tri))
    # A hole wound the exterior way flips the other way:
    # this square traverses clockwise (spherical area
    # positive), so as a hole it reverses to negative.
    hole = [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0],
            [0.0, 0.0]]
    assert country_store._spherical_signed_area(hole) > 0
    fixed_hole = country_store._rewind_ring(hole, exterior=False)
    assert country_store._spherical_signed_area(fixed_hole) < 0
    # An already-positive exterior is left alone, including
    # an antimeridian-crossing ring (longitude differences
    # wrap, so the jump does not flip the sign). This ring
    # traverses clockwise once unwrapped.
    anti = [[179.0, 10.0], [179.0, 12.0], [-179.0, 12.0],
            [-179.0, 10.0], [179.0, 10.0]]
    assert country_store._spherical_signed_area(anti) > 0
    assert country_store._rewind_ring(anti, exterior=True) == anti


def test_degenerate_triangle_dropped() -> None:
    """2026-11-03: a collinear Norwegian island triangle
    (one vertex on the segment between the other two)
    rendered in d3-geo as the whole sphere, painting the
    Context oceans orange even after the rewind. Such
    rings are dropped; genuine small triangles survive."""
    import sqlite3

    from app import country_store

    # Norway poly 19, verbatim from the served payload.
    nor_tri = [[12.734, 65.976], [12.904, 66.015],
               [12.369, 65.892], [12.734, 65.976]]
    assert country_store._is_degenerate_triangle(nor_tri)
    # Vietnam's island triangle: small but real.
    vnm_tri = [[106.211, 9.621], [106.093, 9.761],
               [106.284, 9.544], [106.211, 9.621]]
    assert not country_store._is_degenerate_triangle(vnm_tri)
    # A repeated vertex is degenerate too.
    dup = [[0.0, 0.0], [1.0, 0.0], [0.0, 0.0], [0.0, 0.0]]
    assert country_store._is_degenerate_triangle(dup)
    # Larger rings are never dropped by this test, even
    # with a sharp vertex (fjord detail, not degeneracy).
    coast = [[0.0, 0.0], [1.0, 0.0], [1.0, 0.01],
             [2.0, 0.0], [0.0, 0.0]]
    assert not country_store._is_degenerate_triangle(coast)

    # End to end through _load_geometry: a feature whose
    # only ring is degenerate disappears; a feature with
    # a genuine triangle keeps it.
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE gdelt_codes_country"
                " (iso_code TEXT, full_name TEXT,"
                "  polygon_type TEXT, bounding_box TEXT)")
    nor_box = ("{[[[12.734,65.976],[12.904,66.015],"
               "[12.369,65.892],[12.734,65.976]]]}")
    vnm_box = ("{[[[106.211,9.621],[106.093,9.761],"
               "[106.284,9.544],[106.211,9.621]]]}")
    con.execute("INSERT INTO gdelt_codes_country VALUES"
                " ('NOR', 'Norway', 'MultiPolygon', ?)", (nor_box,))
    con.execute("INSERT INTO gdelt_codes_country VALUES"
                " ('VNM', 'Vietnam', 'MultiPolygon', ?)", (vnm_box,))
    country_store._GEOMETRY_CACHE.clear()
    feats = country_store._load_geometry(con)
    ids = [f["id"] for f in feats]
    assert "NOR" not in ids
    assert "VNM" in ids
    country_store._GEOMETRY_CACHE.clear()
