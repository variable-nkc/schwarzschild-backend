"""Tests for the DEV Look-Closer endpoints (/lc/, Wave 1 only).

Same discipline as test_dev_dda.py: every test builds its own
small SQLite fixtures in a temporary directory and monkeypatches
the environment per test. No test reads a real product. These
tests cover the dev endpoints' arithmetic and gating only — they
do not bless the shapes as contract (formalization does that).
The LC-02/LC-05 scoring rules asserted here are the PROVISIONAL
dev-only rules documented in app/lc_store.py and
docs/LC_DEV_NOTES.md, not formalized methodology.
"""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

LC_MSG = "LC DEV ONLY — not part of the v1 contract."
DATA_REASON = (
    "mirror record/source line of effort, "
    "not served by rollup or /lc/ endpoints"
)
SETS = (
    "escalatory",
    "stability",
    "instability",
    "deescalatory",
    "critical_incidents",
)

# Per-set (r7_dir, std_r7, share_r7, r28_dir) at 2026-09-30 for AAA_BBB.
# Agreement: esc agree, stab agree, inst disagree, deesc disagree,
# crit agree -> stability dots 3. Top |std|: esc 6.25, inst 2.62,
# deesc 0.84 -> the three LC-05 chips.
PER_SET_0930 = {
    "escalatory": ("up", 6.25, 0.14, "up"),
    "stability": ("steady", -0.10, 0.21, "steady"),
    "instability": ("up", 2.62, 0.24, "steady"),
    "deescalatory": ("steady", -0.84, 0.11, "down"),
    "critical_incidents": ("steady", -0.82, 0.05, "steady"),
}


def _rollup_columns() -> str:
    cols = [
        "iso1 TEXT", "iso2 TEXT", "as_of TEXT", "label TEXT",
        "flag_insufficient INTEGER", "flag_hot INTEGER",
        "flag_drifting INTEGER", "flag_atypical INTEGER",
        "temperature REAL", "v_7d REAL", "days_present_7 REAL",
        "insufficient_sets TEXT",
        "build_id TEXT", "vintage INTEGER", "computed_at TEXT",
    ]
    for s in SETS:
        cols += [
            f"dir_r7_{s} TEXT", f"std_r7_{s} REAL", f"share_r7_{s} REAL",
            f"dir_r28_{s} TEXT",
        ]
    return ", ".join(cols)


def _rollup_row(day: date, label: str, drifting: int) -> tuple:
    vals: list = [
        "AAA", "BBB", day.isoformat(), label, 0, 0, drifting, 0,
        0.3, 1128.0, 7.0, "", "test-build", 366, "2026-10-01",
    ]
    for s in SETS:
        r7, std, share, r28 = PER_SET_0930[s]
        vals += [r7, std, share, r28]
    return tuple(vals)


@pytest.fixture()
def lc_env(tmp_path, monkeypatch):
    """Tiny rollup (AAA_BBB, 2026-09-01..30) + tiny DDA sidecar.

    Rollup labels: steady 09-01..09-28, drifting 09-29..09-30, so
    LC-01 at 09-30 has delta "no change since yesterday" and at
    09-29 has a label + flag change vs 09-28.
    DDA dyads over [2026-06-11, 2026-09-30] (112 days, the full
    LC-07 lead-in + typical window): CONST at 10/day mapped,
    SPIKE at 10/day then 100/day over the last 7 days, SMALL
    with only 10 days (ratio/median null).
    """
    db = tmp_path / "rollup.sqlite"
    con = sqlite3.connect(db)
    con.execute(f"CREATE TABLE dyad_status_rollup ({_rollup_columns()})")
    con.execute(
        "CREATE INDEX idx_rollup_dyad ON dyad_status_rollup(iso1, iso2, as_of)"
    )
    day = date(2026, 9, 1)
    rows = []
    while day <= date(2026, 9, 30):
        if day >= date(2026, 9, 29):
            rows.append(_rollup_row(day, "drifting", 1))
        else:
            rows.append(_rollup_row(day, "steady", 0))
        day += timedelta(days=1)
    placeholders = ", ".join("?" * len(rows[0]))
    con.executemany(
        f"INSERT INTO dyad_status_rollup VALUES ({placeholders})", rows
    )
    con.commit()
    con.close()

    dda = tmp_path / "dda.sqlite"
    con = sqlite3.connect(dda)
    con.execute(
        "CREATE TABLE dda (id INTEGER, iso1 TEXT, iso2 TEXT,"
        " obs_date DATE, cameo_code TEXT, obs_count INTEGER)"
    )
    con.execute(
        "CREATE INDEX idx_dda_dyad_date ON dda (iso1, iso2, obs_date)"
    )
    drows = []
    rid = 1
    day = date(2026, 6, 11)
    while day <= date(2026, 9, 30):
        drows.append((rid, "AAA", "BBB", day.isoformat(), "010", 10))
        rid += 1
        spike = 100 if day >= date(2026, 9, 24) else 10
        drows.append((rid, "CCC", "DDD", day.isoformat(), "010", spike))
        rid += 1
        day += timedelta(days=1)
    # Unmapped code on CONST's as-of: excluded from mapped volume.
    drows.append((rid, "AAA", "BBB", "2026-09-30", "999", 5))
    rid += 1
    day = date(2026, 9, 21)
    while day <= date(2026, 9, 30):
        drows.append((rid, "EEE", "FFF", day.isoformat(), "010", 10))
        rid += 1
        day += timedelta(days=1)
    con.executemany("INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)", drows)
    con.execute("CREATE TABLE _sidecar_meta (key TEXT, value TEXT)")
    con.executemany(
        "INSERT INTO _sidecar_meta VALUES (?, ?)",
        [("dda_rows", str(len(drows))), ("obs_max", "2026-09-30")],
    )
    con.commit()
    con.close()
    tokens = tmp_path / "cameo_tokens.json"
    tokens.write_text(
        json.dumps([{"code": "010", "p1": 0, "p2": 0, "p3": 0.2,
                     "p4": 0.3, "p5": 0.5}]),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROLLUP_SQLITE", str(db))
    monkeypatch.setenv("DDA_SQLITE", str(dda))
    monkeypatch.setenv("DDA_SIMPLEXES_JSON", str(tokens))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def test_lc01_history_delta_and_window_end(lc_env):
    r = lc_env.get("/lc/lc-01?iso1=AAA&iso2=BBB&as_of=2026-09-30")
    assert r.status_code == 200
    body = r.json()
    assert body["provenance"]["message"] == LC_MSG
    data = body["data"]
    assert data["as_of"] == "2026-09-30"
    assert data["today_label"] == "drifting"
    # 28 days ending at as-of (09-03..09-30), oldest first.
    assert len(data["days"]) == 28
    assert data["days"][0]["as_of"] == "2026-09-03"
    assert data["days"][-1]["as_of"] == "2026-09-30"
    assert data["days"][-1]["label"] == "drifting"
    assert data["days"][-1]["flag_drifting"] is True
    assert data["days"][-1]["flag_hot"] is False
    assert "recent_typical" not in data
    # 09-30 vs 09-29: same label, same flags.
    assert data["delta"]["previous_as_of"] == "2026-09-29"
    assert data["delta"]["label_change"] is None
    assert data["delta"]["flag_changes"] == []
    assert data["delta"]["text"] == "no change since yesterday"
    # Window ends at as-of, never at the snapshot end.
    r2 = lc_env.get("/lc/lc-01?iso1=AAA&iso2=BBB&as_of=2026-09-15")
    d2 = r2.json()["data"]
    assert d2["as_of"] == "2026-09-15"
    assert d2["days"][-1]["as_of"] == "2026-09-15"
    assert len(d2["days"]) == 15
    # Label change day: 09-29 vs steady 09-28.
    r3 = lc_env.get("/lc/lc-01?iso1=AAA&iso2=BBB&as_of=2026-09-29")
    d3 = r3.json()["data"]
    assert d3["delta"]["label_change"] == {
        "from": "steady", "to": "drifting"}
    assert d3["delta"]["flag_changes"] == [
        {"flag": "flag_drifting", "from": False, "to": True}]
    assert "steady -> drifting" in d3["delta"]["text"]


def test_lc02_rows_provisional_dots_no_composite(lc_env):
    r = lc_env.get("/lc/lc-02?iso1=AAA&iso2=BBB&as_of=2026-09-30")
    assert r.status_code == 200
    assert r.json()["provenance"]["message"] == LC_MSG
    data = r.json()["data"]
    assert data["as_of"] == "2026-09-30"
    assert data["provisional"] is True
    rows = {row["row"]: row for row in data["rows"]}
    assert set(rows) == {"evidence", "stability", "data"}
    # Evidence: days 7 (>=4: +2, >=6: +1), v 1128 (>=30: +1),
    # no insufficient sets (+1) = 5.
    ev = rows["evidence"]
    assert ev["dots"] == 5
    assert ev["inputs"]["v_7d"] == 1128.0
    assert ev["inputs"]["days_present_7"] == 7.0
    assert ev["inputs"]["sets_with_sufficient_support"] == list(SETS)
    assert ev["inputs"]["sets_with_insufficient_support"] == []
    # Stability (revised 2026-10-30): fixture R7 dirs are constant
    # across days, so every set agrees 7/7 -> all green, full window.
    st = rows["stability"]
    assert st["dot_states"] == ["green"] * 5
    assert st["inputs"]["window_days"] == 7
    assert st["inputs"]["short_history"] is False
    assert st["inputs"]["per_set"]["instability"]["agree_days"] == 7
    assert st["inputs"]["per_set"]["instability"]["dot"] == "green"
    # Data row: dots null, reason verbatim. No composite anywhere.
    assert rows["data"]["dots"] is None
    assert rows["data"]["reason"] == DATA_REASON
    assert "composite" not in json.dumps(data).lower()


def test_lc05_chips_rank_mixed_distance(lc_env):
    r = lc_env.get("/lc/lc-05?iso1=AAA&iso2=BBB&as_of=2026-09-30")
    assert r.status_code == 200
    assert r.json()["provenance"]["message"] == LC_MSG
    chips = r.json()["data"]["chips"]
    assert len(chips) == 3
    # Ranked by |std|: esc 6.25, inst 2.62, deesc 0.84.
    assert [c["set"] for c in chips] == [
        "escalatory", "instability", "deescalatory"]
    esc, inst, deesc = chips
    assert esc["direction"] == "up"
    assert esc["distance"] == "well above usual"
    assert esc["std_resid"] == pytest.approx(6.25)
    assert esc["share"] == pytest.approx(0.14)
    assert esc["r7_dir"] == "up" and esc["r28_dir"] == "up"
    # R28 steady disagrees with R7 up -> mixed over the month.
    assert inst["direction"] == "mixed over the month"
    assert inst["distance"] == "above usual"
    # R28 down disagrees with R7 steady -> mixed; |std| < 1.
    assert deesc["direction"] == "mixed over the month"
    assert deesc["distance"] == "near usual"
    assert deesc["r7_dir"] == "steady" and deesc["r28_dir"] == "down"


def test_lc07_constant_series(lc_env):
    r = lc_env.get("/lc/lc-07?iso1=AAA&iso2=BBB&as_of=2026-09-30")
    assert r.status_code == 200
    assert r.json()["provenance"]["message"] == LC_MSG
    data = r.json()["data"]
    assert data["as_of"] == "2026-09-30"
    # Pooled 7d mapped = 70; all 84 baseline windows are 70.
    assert data["pooled_7d_volume"] == pytest.approx(70.0)
    assert data["baseline_expectation"] == pytest.approx(70.0)
    assert data["baseline_window_count"] == 84
    assert data["volume_ratio"] == pytest.approx(1.0)
    assert data["baseline_tail"] == pytest.approx(1.0)
    # Internal consistency: pooled / baseline == ratio.
    assert data["pooled_7d_volume"] / data["baseline_expectation"] \
        == pytest.approx(data["volume_ratio"])
    # Previous 14 days all ratio 1.0 -> median 1.0, 14 valid.
    assert data["typical_valid_days"] == 14
    assert data["recent_typical_median"] == pytest.approx(1.0)
    assert len(data["recent_daily_ratios"]) == 14
    assert all(row["volume_ratio"] == pytest.approx(1.0)
               for row in data["recent_daily_ratios"])
    assert "1.00 times the usual volume" in data["verdict"]
    # Flat series: rare tail but ratio exactly 1.0 -> no caution.
    assert data["caution"] is False
    assert data["caution_text"] is None


def test_lc07_spike_series(lc_env):
    r = lc_env.get("/lc/lc-07?iso1=CCC&iso2=DDD&as_of=2026-09-30")
    assert r.status_code == 200
    data = r.json()["data"]
    # Last 7 days at 100: pooled 700, baseline windows all 70.
    assert data["pooled_7d_volume"] == pytest.approx(700.0)
    assert data["baseline_expectation"] == pytest.approx(70.0)
    assert data["volume_ratio"] == pytest.approx(10.0)
    assert data["baseline_tail"] == pytest.approx(0.0)
    assert data["verdict"] == (
        "10.00 times the usual volume; exceptionally loud")
    assert data["caution"] is True
    assert data["caution_text"] == (
        "volume alone could explain part of today's move — "
        "check the mix before the level.")
    assert data["typical_valid_days"] == 14


def test_lc07_short_history_nulls(lc_env):
    r = lc_env.get("/lc/lc-07?iso1=EEE&iso2=FFF&as_of=2026-09-30")
    assert r.status_code == 200
    data = r.json()["data"]
    # 10 days cannot supply the 97-day lead-in: nulls, not zeros.
    assert data["volume_ratio"] is None
    assert data["baseline_tail"] is None
    assert data["recent_typical_median"] is None
    assert data["typical_valid_days"] == 0
    assert data["verdict"] is None
    assert data["caution"] is False


def test_lc_unknown_dyad_returns_empty(lc_env):
    for path, key in (("/lc/lc-01", "days"), ("/lc/lc-02", "rows"),
                      ("/lc/lc-05", "chips")):
        r = lc_env.get(f"{path}?iso1=XXX&iso2=YYY")
        assert r.status_code == 200, path
        data = r.json()["data"]
        assert r.json()["provenance"]["message"] == LC_MSG
        if key == "rows":
            # Shape preserved with nulls; data row keeps its reason.
            assert data["as_of"] is None
            assert data["rows"][2]["reason"] == DATA_REASON
            assert data["rows"][2]["dots"] is None
        else:
            assert data[key] == []
            assert data["as_of"] is None
    r = lc_env.get("/lc/lc-07?iso1=XXX&iso2=YYY")
    assert r.status_code == 200
    assert r.json()["data"]["volume_ratio"] is None
    assert r.json()["data"]["as_of"] is None


def test_lc_missing_config_503(tmp_path, monkeypatch):
    monkeypatch.delenv("ROLLUP_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SIMPLEXES_JSON", raising=False)
    client = TestClient(create_app())
    for path in ("/lc/lc-01", "/lc/lc-02", "/lc/lc-05", "/lc/lc-07"):
        r = client.get(f"{path}?iso1=AAA&iso2=BBB")
        assert r.status_code == 503, path
        assert r.json()["error"]["code"] == "db_not_configured"
        assert r.json()["provenance"]["message"] == LC_MSG


def test_lc_version_gate_matches_dev(lc_env):
    # Dan 2026-10-30 flag 1: /lc/ applies the same gate as /dev/.
    # api_version=99 409s in strict mode; 1 and no version pass.
    for path in ("/lc/lc-01", "/lc/lc-02", "/lc/lc-05", "/lc/lc-07"):
        r = lc_env.get(
            f"{path}?iso1=AAA&iso2=BBB&as_of=2026-09-30&api_version=99")
        assert r.status_code == 409, path
        r = lc_env.get(
            f"{path}?iso1=AAA&iso2=BBB&as_of=2026-09-30&api_version=1")
        assert r.status_code == 200, path
        r = lc_env.get(f"{path}?iso1=AAA&iso2=BBB&as_of=2026-09-30")
        assert r.status_code == 200, path
    # Sanity: the gate still fires on a v1 endpoint in this env.
    r = lc_env.get("/dyad-status?iso1=AAA&iso2=BBB&api_version=99")
    assert r.status_code == 409


def test_lc07_effective_as_of_matches_rollup(lc_env):
    # Dan 2026-10-30 flag 3: LC-07 shares LC-01/02/05's effective
    # as-of (latest rollup row <= requested). AAA_BBB's rollup
    # ends 2026-09-30, so a 2026-10-05 request resolves to
    # 2026-09-30 (constant series, ratio 1.0) instead of the
    # verbatim 2026-10-05 window, which would dilute the pooled
    # volume with zero-volume October days.
    r = lc_env.get("/lc/lc-07?iso1=AAA&iso2=BBB&as_of=2026-10-05")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["as_of"] == "2026-09-30"
    assert data["volume_ratio"] == pytest.approx(1.0)
    assert data["pooled_7d_volume"] == pytest.approx(70.0)


def _stability_rollup(tmp_path, dyad_days):
    """Build a rollup DB from {(iso1, iso2): [(as_of, {set: dir})]}.

    Only the columns LC-02 reads are populated; other columns are
    given neutral values. Returns the DB path.
    """
    db = tmp_path / "stab_rollup.sqlite"
    con = sqlite3.connect(db)
    con.execute(f"CREATE TABLE dyad_status_rollup ({_rollup_columns()})")
    for (iso1, iso2), days in dyad_days.items():
        for as_of, dirs in days:
            vals = [iso1, iso2, as_of, "steady", 0, 0, 0, 0,
                    0.3, 100.0, 7.0, "", "test-build", 366, "2026-10-01"]
            for s in SETS:
                vals += [dirs.get(s, "steady"), 0.0, 0.1, "steady"]
            con.execute(
                f"INSERT INTO dyad_status_rollup VALUES "
                f"({', '.join('?' * len(vals))})", vals)
    con.commit()
    con.close()
    return db


def test_lc02_stability_graded_dots(tmp_path, monkeypatch):
    # Last 7 as-ofs 2026-09-24..30, today = 2026-09-30.
    days = []
    esc = ["steady"] * 5 + ["up", "up"]
    stab = ["steady"] * 7
    inst = ["steady"] * 6 + ["up"]
    deesc = ["steady"] * 7
    crit = ["down"] + ["steady"] * 6
    for i, day in enumerate(range(24, 31)):
        days.append((f"2026-09-{day}", {
            "escalatory": esc[i], "stability": stab[i],
            "instability": inst[i], "deescalatory": deesc[i],
            "critical_incidents": crit[i]}))
    db = _stability_rollup(tmp_path, {("GGG", "HHH"): days})
    monkeypatch.setenv("ROLLUP_SQLITE", str(db))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    client = TestClient(create_app())
    r = client.get("/lc/lc-02?iso1=GGG&iso2=HHH&as_of=2026-09-30")
    assert r.status_code == 200
    st = {row["row"]: row for row in r.json()["data"]["rows"]}["stability"]
    # esc 2/7 empty, stab 7/7 green, inst 1/7 empty,
    # deesc 7/7 green, crit 6/7 yellow — in SETS order.
    assert st["dot_states"] == ["empty", "green", "empty", "green", "yellow"]
    assert st["inputs"]["window_days"] == 7
    assert st["inputs"]["short_history"] is False
    assert st["inputs"]["per_set"]["escalatory"]["agree_days"] == 2
    assert st["inputs"]["per_set"]["critical_incidents"]["agree_days"] == 6


def test_lc02_stability_short_history_flag(tmp_path, monkeypatch):
    # Only 3 as-ofs, all dirs steady: agree 3/3 -> empty by the
    # count thresholds, and short_history must be flagged.
    days = [(f"2026-09-{d}", {s: "steady" for s in SETS})
            for d in (28, 29, 30)]
    db = _stability_rollup(tmp_path, {("III", "JJJ"): days})
    monkeypatch.setenv("ROLLUP_SQLITE", str(db))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    client = TestClient(create_app())
    r = client.get("/lc/lc-02?iso1=III&iso2=JJJ&as_of=2026-09-30")
    assert r.status_code == 200
    st = {row["row"]: row for row in r.json()["data"]["rows"]}["stability"]
    assert st["dot_states"] == ["empty"] * 5
    assert st["inputs"]["window_days"] == 3
    assert st["inputs"]["short_history"] is True
    assert "Short history" in st["reason"]


def test_lc02_stability_insufficient_never_agrees(tmp_path, monkeypatch):
    # Dan 2026-10-30: "insufficient" is absence of evidence,
    # not a direction, so it never counts as agreement — even
    # insufficient-vs-insufficient. Today = 2026-09-30.
    # Escalatory: today insufficient, 5 insufficient days in
    # the window (incl. today) + 2 up days -> old code scored
    # 5/7 yellow; correct score is 0/7 empty. Stability:
    # today up, 5 insufficient days + 2 up days -> 2/7 empty
    # (insufficient history days are misses). Other sets
    # steady all week -> green, unaffected.
    days = []
    esc = ["up", "up"] + ["insufficient"] * 5
    stab = ["insufficient"] * 5 + ["up", "up"]
    for i, day in enumerate(range(24, 31)):
        days.append((f"2026-09-{day}", {
            "escalatory": esc[i], "stability": stab[i],
            "instability": "steady", "deescalatory": "steady",
            "critical_incidents": "steady"}))
    db = _stability_rollup(tmp_path, {("KKK", "LLL"): days})
    monkeypatch.setenv("ROLLUP_SQLITE", str(db))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    client = TestClient(create_app())
    r = client.get("/lc/lc-02?iso1=KKK&iso2=LLL&as_of=2026-09-30")
    assert r.status_code == 200
    st = {row["row"]: row for row in r.json()["data"]["rows"]}["stability"]
    assert st["dot_states"] == ["empty", "empty", "green", "green", "green"]
    assert st["inputs"]["per_set"]["escalatory"]["today_dir"] == "insufficient"
    assert st["inputs"]["per_set"]["escalatory"]["agree_days"] == 0
    assert st["inputs"]["per_set"]["stability"]["agree_days"] == 2


# ------------------------------------------------------------------
# Wave 2: LC-06 / LC-08 / LC-09
# ------------------------------------------------------------------

def _wave2_client(monkeypatch, rollup_db=None, dda_db=None, tokens=None):
    """TestClient with Wave 2 env (any of the three may be None)."""
    if rollup_db is not None:
        monkeypatch.setenv("ROLLUP_SQLITE", str(rollup_db))
    if dda_db is not None:
        monkeypatch.setenv("DDA_SQLITE", str(dda_db))
    if tokens is not None:
        monkeypatch.setenv("DDA_SIMPLEXES_JSON", str(tokens))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def test_lc06_pooled_mix_null_joint_skipped_and_low_support(
        tmp_path, monkeypatch):
    """LC-06: pooled geometric mean, null-joint day skipped,
    coverage, and the low-support floor.

    Simplexes have no zeros, so no delta replacement enters
    the hand computation. Now window (60d ending 2026-09-30):
    2026-09-28 null-joint code 030 x5, 2026-09-29 code 010
    x10, 2026-09-30 code 020 x10. Then window (60d ending
    2026-07-02): code 010 on both days, so the pooled mix
    is exactly code 010's simplex. Second dyad CCC_DDD has
    now-window coverage 1/11 < 0.50 -> low_support.
    """
    import math

    rollup = _stability_rollup(tmp_path, {
        ("AAA", "BBB"): [("2026-09-30", {s: "steady" for s in SETS})],
        ("CCC", "DDD"): [("2026-09-30", {s: "steady" for s in SETS})],
    })
    dda = tmp_path / "dda.sqlite"
    con = sqlite3.connect(dda)
    con.execute(
        "CREATE TABLE dda (id INTEGER, iso1 TEXT, iso2 TEXT,"
        " obs_date DATE, cameo_code TEXT, obs_count INTEGER)"
    )
    rows = [
        (1, "AAA", "BBB", "2026-09-28", "030", 5),
        (2, "AAA", "BBB", "2026-09-29", "010", 10),
        (3, "AAA", "BBB", "2026-09-30", "020", 10),
        (4, "AAA", "BBB", "2026-07-01", "010", 30),
        (5, "AAA", "BBB", "2026-07-02", "010", 10),
        (6, "CCC", "DDD", "2026-09-30", "010", 1),
        (7, "CCC", "DDD", "2026-09-30", "999", 10),
    ]
    con.executemany("INSERT INTO dda VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    tokens = tmp_path / "cameo_tokens.json"
    tokens.write_text(json.dumps([
        {"code": "010", "p1": 0.4, "p2": 0.3, "p3": 0.2,
         "p4": 0.05, "p5": 0.05},
        {"code": "020", "p1": 0.1, "p2": 0.1, "p3": 0.2,
         "p4": 0.3, "p5": 0.3},
        {"code": "030", "p1": None, "p2": None, "p3": None,
         "p4": None, "p5": None},
    ]), encoding="utf-8")
    client = _wave2_client(monkeypatch, rollup, dda, tokens)
    r = client.get("/lc/lc-06?iso1=AAA&iso2=BBB&as_of=2026-09-30")
    assert r.status_code == 200
    assert r.json()["provenance"]["message"] == LC_MSG
    data = r.json()["data"]
    assert data["as_of"] == "2026-09-30"
    assert data["provisional"] is True
    assert data["window_days"] == 60
    assert data["comparison_days_ago"] == 90
    now = data["now"]
    assert now["window_start"] == "2026-08-02"
    assert now["window_end"] == "2026-09-30"
    assert now["days_with_joint"] == 2
    assert now["joint_volume"] == 20
    assert now["volume_total"] == 25
    assert now["coverage"] == pytest.approx(0.8)
    # Equal-weight geometric mean of 010 and 020, closed.
    raw = [math.sqrt(a * b) for a, b in zip(
        (0.4, 0.3, 0.2, 0.05, 0.05), (0.1, 0.1, 0.2, 0.3, 0.3))]
    total = sum(raw)
    for p, expected in zip(("p1", "p2", "p3", "p4", "p5"), raw):
        assert now["mix"][p] == pytest.approx(expected / total)
    then = data["then"]
    assert then["window_start"] == "2026-05-04"
    assert then["window_end"] == "2026-07-02"
    assert then["days_with_joint"] == 2
    assert then["joint_volume"] == 40
    assert then["volume_total"] == 40
    assert then["coverage"] == pytest.approx(1.0)
    for p, expected in zip(
            ("p1", "p2", "p3", "p4", "p5"),
            (0.4, 0.3, 0.2, 0.05, 0.05)):
        assert then["mix"][p] == pytest.approx(expected)
    assert data["low_support"] is False
    assert data["low_support_reason"] is None
    # Low-support dyad: now coverage 1/11, then window empty
    # (coverage null, not low-support on its own).
    r = client.get("/lc/lc-06?iso1=CCC&iso2=DDD&as_of=2026-09-30")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["now"]["coverage"] == pytest.approx(1 / 11)
    assert data["then"]["coverage"] is None
    assert data["low_support"] is True
    assert "now window coverage" in data["low_support_reason"]


def _lc08_rollup(tmp_path, dyad_days):
    """Rollup DB from {dyad: [(as_of, {set: std_or_None})]}."""
    db = tmp_path / "lc08.sqlite"
    con = sqlite3.connect(db)
    con.execute(f"CREATE TABLE dyad_status_rollup ({_rollup_columns()})")
    for (iso1, iso2), days in dyad_days.items():
        for as_of, stds in days:
            vals = [iso1, iso2, as_of, "steady", 0, 0, 0, 0,
                    0.3, 100.0, 7.0, "", "test-build", 366, "2026-10-01"]
            for s in SETS:
                vals += ["steady", stds.get(s), 0.1, "steady"]
            con.execute(
                f"INSERT INTO dyad_status_rollup VALUES "
                f"({', '.join('?' * len(vals))})", vals)
    con.commit()
    con.close()
    return db


def test_lc08_bands_inside_outside_net_skips_missing(tmp_path, monkeypatch):
    """LC-08: p10-p90 nearest-rank bands, inside/outside with
    signed distance, and net R skipping as-ofs with a missing
    std. 12 as-ofs 2026-09-19..30 (d=1..12); deescalatory is
    missing on d=6, critical_incidents on d=5, so net has 10
    computable values, not 12."""
    days = []
    for d in range(1, 13):
        days.append((f"2026-09-{18 + d}", {
            "escalatory": float(d),
            "stability": 0.0,
            "instability": float(-d),
            "deescalatory": None if d == 6 else 0.0,
            "critical_incidents": None if d == 5 else float(d),
        }))
    db = _lc08_rollup(tmp_path, {("AAA", "BBB"): days})
    client = _wave2_client(monkeypatch, rollup_db=db)
    r = client.get("/lc/lc-08?iso1=AAA&iso2=BBB&as_of=2026-09-30")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["as_of"] == "2026-09-30"
    assert data["window_days"] == 60
    assert data["history_rows"] == 12
    assert data["band_method"] == (
        "p10-p90 nearest-rank over trailing 60 rollup "
        "as-ofs incl. effective"
    )
    per = data["per_set"]
    assert list(per.keys()) == list(SETS)
    # esc 1..12: p10 idx ceil(1.2)-1=1 -> 2; p90 idx
    # ceil(10.8)-1=10 -> 11; last 12 -> outside, +1.
    assert per["escalatory"] == {
        "last_week": 12.0, "lo": 2.0, "hi": 11.0, "n": 12,
        "inside": False, "distance_outside": 1.0}
    assert per["stability"] == {
        "last_week": 0.0, "lo": 0.0, "hi": 0.0, "n": 12,
        "inside": True, "distance_outside": 0}
    # inst -12..-1: p10 -> -11, p90 -> -2; last -12 -> -1.
    assert per["instability"] == {
        "last_week": -12.0, "lo": -11.0, "hi": -2.0, "n": 12,
        "inside": False, "distance_outside": -1.0}
    assert per["deescalatory"]["n"] == 11
    assert per["deescalatory"]["inside"] is True
    # crit d except 5: p10 -> 2, p90 -> 11; last 12 -> +1.
    assert per["critical_incidents"]["lo"] == 2.0
    assert per["critical_incidents"]["hi"] == 11.0
    assert per["critical_incidents"]["distance_outside"] == 1.0
    # Net R = d on computable days (d != 5, 6): 10 values
    # {1,2,3,4,7,8,9,10,11,12}; p10 -> 1, p90 -> 11.
    net = data["net"]
    assert net == {
        "last_week": 12.0, "lo": 1.0, "hi": 11.0, "n": 10,
        "inside": False, "distance_outside": 1.0}


def test_lc08_band_null_below_floor(tmp_path, monkeypatch):
    """LC-08: fewer than 10 present values -> lo/hi null, and
    a present value with no band reports distance null (not
    0, which would falsely read as inside)."""
    days = []
    for d in range(1, 13):
        days.append((f"2026-09-{18 + d}", {
            "escalatory": 2.0 if d >= 10 else None,
            "stability": 0.0,
            "instability": 0.0,
            "deescalatory": 0.0,
            "critical_incidents": 0.0,
        }))
    db = _lc08_rollup(tmp_path, {("AAA", "BBB"): days})
    client = _wave2_client(monkeypatch, rollup_db=db)
    r = client.get("/lc/lc-08?iso1=AAA&iso2=BBB&as_of=2026-09-30")
    assert r.status_code == 200
    esc = r.json()["data"]["per_set"]["escalatory"]
    assert esc == {
        "last_week": 2.0, "lo": None, "hi": None, "n": 3,
        "inside": False, "distance_outside": None}
    # Net is computable only on the 3 esc-present days.
    assert r.json()["data"]["net"]["n"] == 3
    assert r.json()["data"]["net"]["lo"] is None


def _lc09_rollup(tmp_path):
    """Rollup for LC-09: focal AAA_BBB + peers AAA_CCC/DDD/
    EEE/FFF at 2026-09-30, plus a BBB_CCC decoy (other iso1)."""
    db = tmp_path / "lc09.sqlite"
    con = sqlite3.connect(db)
    con.execute(f"CREATE TABLE dyad_status_rollup ({_rollup_columns()})")
    con.execute(
        "CREATE INDEX idx_rollup_dyad ON dyad_status_rollup(iso1, iso2, as_of)"
    )
    days = [f"2026-09-{d}" for d in range(16, 31)]
    spec = {
        # (today temp, today days, today v, prior temps)
        ("AAA", "BBB"): (0.5, 7.0, 100.0, [0.4] * 14),
        ("AAA", "CCC"): (0.8, 7.0, 100.0, [0.6] * 14),
        ("AAA", "DDD"): (0.3, 2.0, 100.0, [0.2] * 14),
        ("AAA", "EEE"): (None, 7.0, 10.0, [0.5] * 5),
        ("AAA", "FFF"): (0.9, 7.0, 100.0, []),
        ("BBB", "CCC"): (0.99, 7.0, 100.0, [0.99] * 14),
    }
    for (iso1, iso2), (temp, dp, v, prior) in spec.items():
        rows = []
        # Prior rows are as-ofs BEFORE effective only; EEE has
        # only the last 5 of those.
        before = [d for d in days if d != "2026-09-30"]
        prior_days = before[-len(prior):] if prior else []
        for as_of, t in zip(prior_days, prior):
            rows.append((iso1, iso2, as_of, t, 7.0, 100.0))
        rows.append((iso1, iso2, "2026-09-30", temp, dp, v))
        for iso1_r, iso2_r, as_of, t, dp_r, v_r in rows:
            vals = [iso1_r, iso2_r, as_of, "steady", 0, 0, 0, 0,
                    t, v_r, dp_r, "", "test-build", 366, "2026-10-01"]
            for s in SETS:
                vals += ["steady", 0.0, 0.1, "steady"]
            con.execute(
                f"INSERT INTO dyad_status_rollup VALUES "
                f"({', '.join('?' * len(vals))})", vals)
    con.commit()
    con.close()
    return db


def test_lc09_peer_ranks_typical_hollow(tmp_path, monkeypatch):
    """LC-09 Part 1: same-first-actor peers only, today and
    typical ranks, hollow support, typical null below 7
    valid days, null temperatures sorted last."""
    db = _lc09_rollup(tmp_path)
    client = _wave2_client(monkeypatch, rollup_db=db)
    r = client.get("/lc/lc-09?iso1=AAA&iso2=BBB&as_of=2026-09-30")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["peer_rule"] == "all dyads with the same first actor"
    focal = data["focal"]
    assert focal["temperature"] == 0.5
    assert focal["typical_temperature"] == 0.4
    # Greater today: CCC 0.8, FFF 0.9 -> rank 3. Greater
    # typical: CCC 0.6 only -> typical rank 2.
    assert focal["rank"] == 3
    assert focal["typical_rank"] == 2
    assert focal["peers_with_temperature"] == 3
    assert focal["peers_with_typical"] == 2
    peers = data["peers"]
    assert [p["dyad"] for p in peers] == [
        "AAA_FFF", "AAA_CCC", "AAA_DDD", "AAA_EEE"]
    assert [p["supported"] for p in peers] == [True, True, False, False]
    assert [p["typical_temperature"] for p in peers] == [
        None, 0.6, 0.2, None]
    assert peers[0]["temperature"] == 0.9
    assert peers[-1]["temperature"] is None
