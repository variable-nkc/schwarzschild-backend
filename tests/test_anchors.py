"""Tests for GET /dev/anchors (R&D reference anchors,
2026-11-25).

Fixture discipline as in test_poc_pipeline.py: small SQLite
fixtures in a temporary directory, monkeypatched
environment. The numeric expectations are recomputed in
the test from the fixture spec by an independent
implementation of the diagnostic's definitions (crosswalk
zeros -> 5e-3 + rescale; compositions as share-weighted
clr means), so the endpoint is checked against the
definitions, not against its own code.
"""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

START = date(2025, 6, 1)
DAYS = 60

# Per-day (code, count) spec for each fixture dyad.
SPEC = {
    "AAA_BBB": [("010", 50), ("020", 20), ("190", 5)],
    "AAA_CCC": [("010", 40), ("020", 10)],
    "CCC_BBB": [("020", 25), ("190", 15)],
    # heavy reporter, but labelled insufficient throughout:
    # must be excluded from every population reference while
    # keeping its own trait.
    "DDD_EEE": [("190", 200)],
}
SUFFICIENT = ["AAA_BBB", "AAA_CCC", "CCC_BBB"]

TOKENS = [
    {"code": "010", "p1": 0.5, "p2": 0.3, "p3": 0.2,
     "p4": 0.0, "p5": 0.0},
    {"code": "020", "p1": 0.0, "p2": 0.1, "p3": 0.2,
     "p4": 0.3, "p5": 0.4},
    {"code": "190", "p1": 0.02, "p2": 0.05, "p3": 0.13,
     "p4": 0.30, "p5": 0.50},
    {"code": "999", "p1": None, "p2": None, "p3": None,
     "p4": None, "p5": None},
]


def _expected() -> dict:
    """Independent recomputation of the population centres
    from SPEC, following the diagnostic's definitions."""
    S = {}
    for t in TOKENS:
        if t["p1"] is None:
            continue
        v = np.array([t[f"p{i}"] for i in range(1, 6)], float)
        v = np.where(v == 0, 5e-3, v)
        S[t["code"]] = v / v.sum()
    S_clr = {c: np.log(v) - np.log(v).mean() for c, v in S.items()}

    def dyad_clr(spec):
        tot = sum(n for _, n in spec)
        return sum((n / tot) * S_clr[c] for c, n in spec)

    def closure(c):
        e = np.exp(c - c.max())
        return e / e.sum()

    pooled: dict[str, float] = {}
    for dy in SUFFICIENT:
        for c, n in SPEC[dy]:
            pooled[c] = pooled.get(c, 0.0) + n * DAYS
    tot = sum(pooled.values())
    b_clr = sum((n / tot) * S_clr[c] for c, n in pooled.items())
    centre_clr = np.mean([dyad_clr(SPEC[dy]) for dy in SUFFICIENT],
                         axis=0)
    return {"b": closure(b_clr), "centre": closure(centre_clr),
            "own": {dy: closure(dyad_clr(spec))
                    for dy, spec in SPEC.items()}}


@pytest.fixture()
def anchors_env(tmp_path: Path,
                monkeypatch: pytest.MonkeyPatch) -> TestClient:
    dda = tmp_path / "dda.sqlite"
    con = sqlite3.connect(dda)
    con.execute(
        "CREATE TABLE dda (id INTEGER, iso1 TEXT, iso2 TEXT,"
        " obs_date DATE, cameo_code TEXT, obs_count INTEGER)")
    rows = []
    rid = 1
    for dy, spec in SPEC.items():
        iso1, iso2 = dy.split("_")
        for i in range(DAYS):
            day = (START + timedelta(days=i)).isoformat()
            for code, n in spec:
                rows.append((rid, iso1, iso2, day, code, n))
                rid += 1
            rows.append((rid, iso1, iso2, day, "999", 3))
            rid += 1
    con.executemany("INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()

    rollup = tmp_path / "rollup.sqlite"
    con = sqlite3.connect(rollup)
    con.execute(
        "CREATE TABLE dyad_status_rollup (iso1 TEXT, iso2 TEXT,"
        " as_of TEXT, label TEXT, build_id TEXT, vintage TEXT,"
        " computed_at TEXT)")
    rows = []
    for dy in SPEC:
        iso1, iso2 = dy.split("_")
        label = "insufficient" if dy == "DDD_EEE" else "steady"
        for k in range(12):
            as_of = (START + timedelta(days=7 * k)).isoformat()
            rows.append((iso1, iso2, as_of, label, "b1", "20250930",
                         "2025-01-01T00:00:00"))
    con.executemany("INSERT INTO dyad_status_rollup VALUES"
                    " (?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()

    tokens = tmp_path / "cameo_tokens.json"
    tokens.write_text(json.dumps(TOKENS), encoding="utf-8")
    monkeypatch.setenv("DDA_SQLITE", str(dda))
    monkeypatch.setenv("DDA_SIMPLEXES_JSON", str(tokens))
    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def test_anchors_ok(anchors_env: TestClient) -> None:
    r = anchors_env.get("/dev/anchors",
                        params={"iso1": "AAA", "iso2": "BBB"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["dyad"] == "AAA_BBB"
    assert data["stratum"]["n_dyads"] == 3
    assert data["volume_used"] == 75  # 50 + 20 + 5 mapped/day
    refs = data["references"]
    assert set(refs) == {"population_mix", "typical_dyad",
                         "random_dyad_day", "own_trait"}
    for name, ref in refs.items():
        shares = ref["shares"]
        assert shares is not None, name
        assert sum(shares) == pytest.approx(1.0, abs=1e-9), name
        q = ref["distance_quantiles"]
        assert q is not None, name
        vals = q["values"]
        assert len(vals) == len(q["probs"]) == 8
        assert all(b >= a for a, b in zip(vals, vals[1:])), name
        assert all(v >= 0 for v in vals), name
    # the two 2-level references share one centre
    assert refs["random_dyad_day"]["shares"] == \
        refs["typical_dyad"]["shares"]
    own = refs["own_trait"]
    assert own["n_days"] == DAYS
    assert own["n_reports"] == 75 * DAYS


def test_anchors_match_definitions(anchors_env: TestClient) -> None:
    exp = _expected()
    r = anchors_env.get("/dev/anchors",
                        params={"iso1": "AAA", "iso2": "BBB"})
    refs = r.json()["data"]["references"]
    assert refs["population_mix"]["shares"] == pytest.approx(
        list(exp["b"]), abs=1e-9)
    assert refs["typical_dyad"]["shares"] == pytest.approx(
        list(exp["centre"]), abs=1e-9)
    assert refs["own_trait"]["shares"] == pytest.approx(
        list(exp["own"]["AAA_BBB"]), abs=1e-9)


def test_anchors_insufficient_dyad_keeps_own_trait(
        anchors_env: TestClient) -> None:
    exp = _expected()
    r = anchors_env.get("/dev/anchors",
                        params={"iso1": "DDD", "iso2": "EEE"})
    assert r.status_code == 200
    refs = r.json()["data"]["references"]
    assert refs["own_trait"]["shares"] == pytest.approx(
        list(exp["own"]["DDD_EEE"]), abs=1e-9)


def test_anchors_unknown_dyad(anchors_env: TestClient) -> None:
    r = anchors_env.get("/dev/anchors",
                        params={"iso1": "ZZZ", "iso2": "YYY"})
    assert r.status_code == 200
    data = r.json()["data"]
    own = data["references"]["own_trait"]
    assert own["shares"] is None
    assert own["n_days"] == 0
    assert data["volume_used"] is None
    # population references that need no volume still serve
    assert data["references"]["typical_dyad"]["shares"] is not None
    assert data["references"]["population_mix"][
        "distance_quantiles"] is None


def test_anchors_repeatable(anchors_env: TestClient) -> None:
    a = anchors_env.get("/dev/anchors",
                        params={"iso1": "AAA", "iso2": "BBB"})
    b = anchors_env.get("/dev/anchors",
                        params={"iso1": "AAA", "iso2": "BBB"})
    assert a.json() == b.json()


# --- as_of restriction (Dan, 2026-11-25) -------------------
# A second fixture whose focal dyad changes both mix and
# volume halfway through, so a date restriction has visible
# consequences for the trait, the stratum and the volume.

START2 = date(2025, 6, 1)
DAYS2 = 40


def _asof_env(tmp_path: Path,
              monkeypatch: pytest.MonkeyPatch) -> TestClient:
    dda = tmp_path / "dda2.sqlite"
    con = sqlite3.connect(dda)
    con.execute(
        "CREATE TABLE dda (id INTEGER, iso1 TEXT, iso2 TEXT,"
        " obs_date DATE, cameo_code TEXT, obs_count INTEGER)")
    rows = []
    rid = 1
    for i in range(DAYS2):
        day = (START2 + timedelta(days=i)).isoformat()
        spec = [("010", 10)] if i < 20 else [("190", 100)]
        for code, n in spec:
            rows.append((rid, "EEE", "FFF", day, code, n))
            rid += 1
        rows.append((rid, "GGG", "HHH", day, "020", 10))
        rid += 1
    con.executemany("INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)",
                    rows)
    con.commit()
    con.close()

    rollup = tmp_path / "rollup2.sqlite"
    con = sqlite3.connect(rollup)
    con.execute(
        "CREATE TABLE dyad_status_rollup (iso1 TEXT, iso2 TEXT,"
        " as_of TEXT, label TEXT, build_id TEXT, vintage TEXT,"
        " computed_at TEXT)")
    rows = []
    for iso1, iso2 in (("EEE", "FFF"), ("GGG", "HHH")):
        for k in range(12):
            as_of = (START2 + timedelta(days=7 * k)).isoformat()
            rows.append((iso1, iso2, as_of, "steady", "b1",
                         "20250930", "2025-01-01T00:00:00"))
    con.executemany("INSERT INTO dyad_status_rollup VALUES"
                    " (?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()

    tokens = tmp_path / "cameo_tokens.json"
    tokens.write_text(json.dumps(TOKENS), encoding="utf-8")
    monkeypatch.setenv("DDA_SQLITE", str(dda))
    monkeypatch.setenv("DDA_SIMPLEXES_JSON", str(tokens))
    monkeypatch.setenv("ROLLUP_SQLITE", str(rollup))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


@pytest.fixture()
def asof_env(tmp_path: Path,
             monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return _asof_env(tmp_path, monkeypatch)


def _pure(code: str) -> list:
    """The composition of a single code, diagnostic rules."""
    t = next(t for t in TOKENS if t["code"] == code)
    v = np.array([t[f"p{i}"] for i in range(1, 6)], float)
    v = np.where(v == 0, 5e-3, v)
    return list(v / v.sum())


def test_anchors_asof_restricts(asof_env: TestClient) -> None:
    cut = (START2 + timedelta(days=19)).isoformat()
    r = asof_env.get("/dev/anchors", params={
        "iso1": "EEE", "iso2": "FFF", "as_of": cut})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["as_of"] == cut
    assert data["span"] == {"first": START2.isoformat(),
                            "last": cut}
    own = data["references"]["own_trait"]
    # only the first half exists at the cut: pure code 010
    assert own["shares"] == pytest.approx(_pure("010"),
                                          abs=1e-9)
    assert own["n_days"] == 20
    assert own["n_reports"] == 200
    assert data["volume_used"] == 10
    # unrestricted, the same dyad is a different quantity:
    # both halves, and the trailing window's median volume
    # straddles the volume change (20 days at 10, 20 at 100)
    r = asof_env.get("/dev/anchors",
                     params={"iso1": "EEE", "iso2": "FFF"})
    data = r.json()["data"]
    assert data["as_of"] is None
    assert data["volume_used"] == 55
    own = data["references"]["own_trait"]
    assert own["n_reports"] == 2200
    assert own["shares"] != pytest.approx(_pure("010"),
                                          abs=1e-9)


def test_anchors_asof_stratum_window(
        asof_env: TestClient) -> None:
    # 6 rollup as-ofs on or before day 35 < the 10 minimum:
    # the stratum is empty and the population references
    # serve nulls, while the dyad's own trait still works.
    cut = (START2 + timedelta(days=35)).isoformat()
    r = asof_env.get("/dev/anchors", params={
        "iso1": "EEE", "iso2": "FFF", "as_of": cut})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["stratum"]["n_dyads"] == 0
    refs = data["references"]
    for name in ("population_mix", "typical_dyad",
                 "random_dyad_day"):
        assert refs[name]["shares"] is None, name
        assert refs[name]["distance_quantiles"] is None, name
    assert refs["own_trait"]["shares"] is not None
    # 11 as-ofs on or before day 70: the stratum exists.
    cut = (START2 + timedelta(days=70)).isoformat()
    r = asof_env.get("/dev/anchors", params={
        "iso1": "EEE", "iso2": "FFF", "as_of": cut})
    data = r.json()["data"]
    assert data["stratum"]["n_dyads"] == 2
    assert data["references"]["population_mix"][
        "shares"] is not None


def test_anchors_bad_asof(asof_env: TestClient) -> None:
    r = asof_env.get("/dev/anchors", params={
        "iso1": "EEE", "iso2": "FFF", "as_of": "June 20 2025"})
    assert r.status_code == 400
    assert "bad_as_of" in r.text


def test_anchors_window(asof_env: TestClient) -> None:
    # The full window over the second half: only code 190
    # at volume 100 exists for the focal dyad there.
    start = (START2 + timedelta(days=20)).isoformat()
    end = (START2 + timedelta(days=39)).isoformat()
    r = asof_env.get("/dev/anchors", params={
        "iso1": "EEE", "iso2": "FFF",
        "start": start, "as_of": end})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["start"] == start
    assert data["as_of"] == end
    assert data["span"] == {"first": start, "last": end}
    own = data["references"]["own_trait"]
    assert own["shares"] == pytest.approx(_pure("190"),
                                          abs=1e-9)
    assert own["n_days"] == 20
    assert own["n_reports"] == 2000
    assert data["volume_used"] == 100
    # start alone also windows the stratum count: as-ofs
    # on/after day 20 number 9 (< 10), so the stratum is
    # empty even though the dyad's own trait computes...
    r = asof_env.get("/dev/anchors", params={
        "iso1": "EEE", "iso2": "FFF", "start": start})
    data = r.json()["data"]
    assert data["stratum"]["n_dyads"] == 0
    assert data["references"]["population_mix"][
        "shares"] is None
    assert data["references"]["own_trait"][
        "shares"] == pytest.approx(_pure("190"), abs=1e-9)
    # ...while a start at day 14 leaves exactly 10 as-ofs
    # in range and the stratum exists.
    start14 = (START2 + timedelta(days=14)).isoformat()
    r = asof_env.get("/dev/anchors", params={
        "iso1": "EEE", "iso2": "FFF", "start": start14})
    assert r.json()["data"]["stratum"]["n_dyads"] == 2


def test_anchors_bad_start(asof_env: TestClient) -> None:
    r = asof_env.get("/dev/anchors", params={
        "iso1": "EEE", "iso2": "FFF", "start": "last June"})
    assert r.status_code == 400
    assert "bad_start" in r.text
