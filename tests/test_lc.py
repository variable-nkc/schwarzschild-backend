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
