"""Tests for the DEV event-detail endpoint (Event detail page).

Same discipline as test_dev_dda.py: a small planted mirror in a
temporary directory, env monkeypatched per test. The planted
numbers are hand-computed in the fixture docstring.
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def event_env(tmp_path, monkeypatch):
    """Tiny mirror: dyad AAA->BBB, as-of 2026-09-30.

    Events (28d window = Sep 3..30; 7d = Sep 24..30):
      1. Sep 30, code 190, geo City One, CAPITAL CITY -> TARGET
         CITY, parent BBB:GOV, src 2/art 4/men 6, tone -6,
         news.example.com
      2. Sep 29, code 030, geo City One, CAPITAL CITY -> BBB
         LAND, no parent, 1/1/2, tone +1, other.org
      3. Sep 25, code 999 (unmapped), geo COUNTRY, AAA LAND ->
         TARGET CITY, parent BBB:MIL, 1/2/3, tone -8, no url
      4. Sep 10, code 190, geo City Two, GENERAL SOMEONE ->
         TARGET CITY, parent BBB:GOV, 3/3/3, tone -3,
         news.example.com
      5. Aug 1 (outside), code 190 — excluded everywhere.
      6. Sep 28, code 190, geo Far City (country CCC, outside
         the dyad) — excluded by the geo-country restriction.
      7. Sep 12, code 030, geo City Two, GENERAL SOMEONE ->
         TARGET CITY, parent BBB:GOV, 1/1/1, tone 0, other.org
      8. Sep 15, code 030, geo City Two, GENERAL SOMEONE ->
         TARGET CITY, parent BBB:GOV, 2/2/2, tone +2, third.io

    City Two therefore holds three mapped events (one 190,
    two 030): its by-events median simplex is the 030 simplex
    itself (median phase 1.7277) where the mean would give
    2.8021 — the median/mean discriminator.
    """
    db = tmp_path / "mirror.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE gdelt_geos (id INTEGER, full_name TEXT,"
        " country_code TEXT, adm1_code TEXT, latitude REAL,"
        " longitude REAL, type_code TEXT)")
    con.executemany(
        "INSERT INTO gdelt_geos VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "City One", "AAA", "AA1", 10.0, 20.0, "WORLD_CITY"),
            (2, "B-Land", "BBB", "BB", 30.0, 40.0, "COUNTRY"),
            (3, "City Two", "AAA", "AA2", 11.0, 21.0, "WORLD_CITY"),
            (4, "Far City", "CCC", "CC1", 0.0, 0.0, "WORLD_CITY"),
        ],
    )
    con.execute(
        "CREATE TABLE gdelt_actors (id INTEGER, full_name TEXT,"
        " country_code TEXT, type_code TEXT, subtype1_code TEXT)")
    con.executemany(
        "INSERT INTO gdelt_actors VALUES (?, ?, ?, ?, ?)",
        [
            (1, "AAALAND", "AAA", "AAA", None),
            (2, "CAPITAL CITY", "AAA", "AAAGOV", "GOV"),
            (3, "GENERAL SOMEONE", "AAA", "AAAMIL", "MIL"),
            (4, "BBBLAND", "BBB", "BBB", None),
            (5, "TARGET CITY", "BBB", "BBBGOV", "GOV"),
            (6, "AAALANDIAN", "AAA", "AAA", None),
        ],
    )
    con.execute(
        "CREATE TABLE gdelt_codes_country"
        " (code TEXT, full_name TEXT)")
    con.executemany(
        "INSERT INTO gdelt_codes_country VALUES (?, ?)",
        [("AAA", "Aaaland"), ("BBB", "Bbbland")],
    )
    con.execute(
        "CREATE TABLE gdelt_codes_actor_subtype"
        " (code TEXT, label TEXT)")
    con.executemany(
        "INSERT INTO gdelt_codes_actor_subtype VALUES (?, ?)",
        [("GOV", "Government"), ("MIL", "Military")],
    )
    con.execute(
        "CREATE TABLE gdelt_events_core (id INTEGER,"
        " event_date DATE, cameo_code TEXT, geo_id REAL,"
        " actor1_id INTEGER, actor2_id INTEGER,"
        " actor1_country_code TEXT, actor2_country_code TEXT,"
        " actor2_parent_code TEXT, min_num_sources INTEGER,"
        " min_num_articles INTEGER, min_num_mentions INTEGER,"
        " init_avg_tone REAL, init_source_url TEXT)")
    con.executemany(
        "INSERT INTO gdelt_events_core VALUES"
        " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2026-09-30", "190", 1.0, 2, 5, "AAA", "BBB",
             "BBB:GOV-.-.", 2, 4, 6, -6.0, "https://news.example.com/x"),
            (2, "2026-09-29", "030", 1.0, 2, 4, "AAA", "BBB",
             None, 1, 1, 2, 1.0, "https://other.org/y"),
            (3, "2026-09-25", "999", 2.0, 1, 5, "AAA", "BBB",
             "BBB:MIL-.-.", 1, 2, 3, -8.0, None),
            (4, "2026-09-10", "190", 3.0, 3, 5, "AAA", "BBB",
             "BBB:GOV-.-.", 3, 3, 3, -3.0, "https://news.example.com/z"),
            (5, "2026-08-01", "190", 1.0, 2, 5, "AAA", "BBB",
             "BBB:GOV-.-.", 9, 9, 9, 9.0, "https://old.example.com/"),
            (6, "2026-09-28", "190", 4.0, 2, 5, "AAA", "BBB",
             "BBB:GOV-.-.", 5, 5, 5, -1.0,
             "https://news.example.com/w"),
            (7, "2026-09-12", "030", 3.0, 3, 5, "AAA", "BBB",
             "BBB:GOV-.-.", 1, 1, 1, 0.0, "https://other.org/a"),
            (8, "2026-09-15", "030", 3.0, 3, 5, "AAA", "BBB",
             "BBB:GOV-.-.", 2, 2, 2, 2.0, "https://third.io/b"),
        ],
    )
    con.commit()
    con.close()
    tokens = tmp_path / "cameo_tokens.json"
    tokens.write_text(
        json.dumps([
            {"code": "190", "p1": 0, "p2": 0, "p3": 0, "p4": 0,
             "p5": 1},
            {"code": "030", "p1": 0.5, "p2": 0.3, "p3": 0.2,
             "p4": 0, "p5": 0},
        ]),
        encoding="utf-8",
    )
    groups = tmp_path / "cameo_groups.json"
    groups.write_text(
        json.dumps({
            "escalatory": ["190"],
            "instability": ["190"],
            "stability": ["030"],
            "deescalatory": [],
            "critical_incidents": ["190"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("MIRROR_SQLITE", str(db))
    monkeypatch.setenv("DDA_SIMPLEXES_JSON", str(tokens))
    monkeypatch.setenv("DDA_SETS_JSON", str(groups))
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    return TestClient(create_app())


def _get(client, **params):
    resp = client.get("/dev/event-detail", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_event_detail_aggregates(event_env):
    data = _get(event_env, iso1="AAA", iso2="BBB", as_of="2026-09-30")
    assert data["as_of"] == "2026-09-30"
    w = data["windows"]["28"]
    assert w["n_events"] == 6
    assert w["n_mapped"] == 5
    assert w["mapped_share"] == 0.8333
    assert w["n_country_geo"] == 1

    points = {p["name"]: p for p in w["map_points"]}
    assert set(points) == {"City One", "City Two"}
    one = points["City One"]
    assert one["n_events"] == 2 and one["volume"] == 8
    # Tones -6 (4 articles) and +1 (1 article): plain median
    # -2.5; articles-weighted median is the weight-4 event's
    # tone itself.
    assert one["tone_by_events"] == -2.5
    assert one["tone_by_articles"] == -6
    # One of City One's two events is a 190 (hot-union):
    # 1/2 by events, 4 of 5 articles by articles.
    assert one["hot_share_by_events"] == 0.5
    assert one["hot_share_by_articles"] == 0.8
    assert one["by_events"]["median_phase"] == pytest.approx(
        3.3394, abs=1e-3)
    # Articles-weighted median (ev1 has 4 articles, ev2 has 1):
    # the weight-4 event's simplex is the median on every
    # component, so the composition is ev1's near-pure p5.
    assert one["by_articles"]["median_phase"] == pytest.approx(
        4.9510, abs=1e-3)
    assert one["by_articles"]["simplex"][4] == pytest.approx(
        0.9804, abs=1e-3)
    two = points["City Two"]
    assert two["n_events"] == 3 and two["volume"] == 6
    # Tones -3 (3 articles), 0 (1), +2 (2): plain median 0;
    # articles-weighted median is -3 (its weight reaches the
    # halfway crossing first).
    assert two["tone_by_events"] == 0.0
    assert two["tone_by_articles"] == -3
    assert two["hot_share_by_events"] == 0.3333
    assert two["hot_share_by_articles"] == 0.5
    # Median of {190, 030, 030} is the 030 simplex itself.
    assert two["by_events"]["median_phase"] == pytest.approx(
        1.7277, abs=1e-3)
    assert two["by_events"]["simplex"][0] == pytest.approx(
        0.49505, abs=1e-3)
    # Articles-weighted median mixes components across events
    # and needs the renormalization (raw sum 0.0246).
    assert two["by_articles"]["median_phase"] == pytest.approx(
        3.0039, abs=1e-3)
    assert "Far City" not in points

    assert data["adm1_compare"] == [
        {"adm1": "AA2", "n28": 3, "n7": 0, "label": "City Two (AA2)"},
        {"adm1": "AA1", "n28": 2, "n7": 2, "label": "City One (AA1)"},
        {"adm1": "BB", "n28": 1, "n7": 1, "label": "B-Land (BB)"},
    ]
    assert w["actors_source"] == [
        {"name": "GENERAL SOMEONE", "n": 3},
        {"name": "CAPITAL CITY", "n": 2},
    ]
    assert w["actors_target"] == [{"name": "TARGET CITY", "n": 5}]
    assert w["target_sectors"] == {
        "coded": 5,
        "rows": [{"sector": "GOV", "n": 4},
                 {"sector": "MIL", "n": 1}],
    }
    assert w["tone"]["buckets"] == [1, 1, 1, 0, 3, 0]
    assert w["tone"]["median_by_events"] == -1.5
    assert w["tone"]["median_by_articles"] == -3
    assert w["reporting"] == {
        "sources": 1.6667, "articles": 2.1667, "mentions": 2.8333}
    outs = {o["name"]: o for o in w["outlets"]}
    assert outs["news.example.com"]["n"] == 2
    assert outs["news.example.com"]["tone_by_events"] == -4.5
    assert outs["news.example.com"]["tone_by_articles"] == -6
    # Both news events are 190s: the outlet's composition is
    # the near-pure p5 simplex under either weighting.
    assert outs["news.example.com"]["by_events"][
        "median_phase"] == pytest.approx(4.9510, abs=1e-3)
    assert outs["other.org"]["tone_by_events"] == 0.5
    assert outs["other.org"]["tone_by_articles"] == 0
    # Both other.org events are 030s.
    assert outs["other.org"]["by_events"][
        "median_phase"] == pytest.approx(1.7277, abs=1e-3)
    assert outs["third.io"]["by_articles"][
        "median_phase"] == pytest.approx(1.7277, abs=1e-3)
    assert w["actor_pairs"] == {
        "sources": ["GENERAL SOMEONE", "CAPITAL CITY"],
        "targets": ["TARGET CITY"],
        "rows": [
            {"source": "GENERAL SOMEONE", "target": "TARGET CITY",
             "n": 3},
            {"source": "CAPITAL CITY", "target": "TARGET CITY",
             "n": 1},
        ],
    }
    assert len(w["actor_xy"]) == 2
    assert w["actor_xy"][0]["name"] == "GENERAL SOMEONE"
    assert w["actor_xy"][0]["n_events"] == 3
    assert w["actor_xy"][0]["by_events"]["median_phase"] == (
        pytest.approx(1.7277, abs=1e-3))
    # GENERAL SOMEONE's tones: -3 (3 art), 0 (1), +2 (2).
    assert w["actor_xy"][0]["tone_by_events"] == 0.0
    assert w["actor_xy"][0]["tone_by_articles"] == -3
    xy1 = w["actor_xy"][1]
    assert xy1["name"] == "CAPITAL CITY"
    assert xy1["tone_by_events"] == -2.5
    assert xy1["tone_by_articles"] == -6

    subs = {r["code"]: r for r in w["subtypes"]["rows"]}
    assert w["subtypes"]["coded_share"] == 0.8333
    assert subs["GOV"]["n_events"] == 2
    assert subs["GOV"]["n_actors"] == 1
    assert subs["GOV"]["label"] == "Government"
    assert subs["GOV"]["by_events"]["median_phase"] == (
        pytest.approx(3.3394, abs=1e-3))
    assert subs["GOV"]["by_articles"]["median_phase"] == (
        pytest.approx(4.9510, abs=1e-3))
    assert subs["MIL"]["n_events"] == 3
    assert subs["MIL"]["by_events"]["median_phase"] == (
        pytest.approx(1.7277, abs=1e-3))

    w7 = data["windows"]["7"]
    assert w7["n_events"] == 3
    assert w7["n_mapped"] == 2
    assert w7["tone"]["median_by_events"] == -6
    assert w7["tone"]["median_by_articles"] == -6

    # Set shares: 190 in escalatory/instability/CI, 030 in
    # stability, 999 in none; denominators are ALL slice
    # events (6 events, 13 articles over 28d).
    assert w["set_shares"] == [
        {"set": "escalatory", "n_events": 2, "n_articles": 7,
         "share_by_events": 0.3333, "share_by_articles": 0.5385},
        {"set": "instability", "n_events": 2, "n_articles": 7,
         "share_by_events": 0.3333, "share_by_articles": 0.5385},
        {"set": "stability", "n_events": 3, "n_articles": 4,
         "share_by_events": 0.5, "share_by_articles": 0.3077},
        {"set": "deescalatory", "n_events": 0, "n_articles": 0,
         "share_by_events": 0.0, "share_by_articles": 0.0},
        {"set": "critical_incidents", "n_events": 2,
         "n_articles": 7, "share_by_events": 0.3333,
         "share_by_articles": 0.5385},
    ]
    assert w7["set_shares"][0] == {
        "set": "escalatory", "n_events": 1, "n_articles": 4,
        "share_by_events": 0.3333, "share_by_articles": 0.5714}
    assert w7["set_shares"][2] == {
        "set": "stability", "n_events": 1, "n_articles": 1,
        "share_by_events": 0.3333, "share_by_articles": 0.1429}


def test_event_detail_set_shares_degrade_without_groups(
        event_env, monkeypatch):
    # DDA_SETS_JSON pointing nowhere: only the set-shares
    # block degrades to null; the page payload is intact.
    monkeypatch.setenv("DDA_SETS_JSON", "/nonexistent.json")
    data = _get(event_env, iso1="AAA", iso2="BBB",
                as_of="2026-09-30")
    assert data["windows"]["28"]["set_shares"] is None
    assert data["windows"]["28"]["n_events"] == 6
    pt = data["windows"]["28"]["map_points"][0]
    assert pt["hot_share_by_events"] is None
    assert pt["hot_share_by_articles"] is None


def test_country_form_names(event_env):
    import os
    import sqlite3 as _sq

    from app.event_detail_store import _country_form_names

    conn = _sq.connect(os.environ["MIRROR_SQLITE"])
    try:
        assert _country_form_names(
            conn, "AAA",
            {"AAALAND", "AAALANDIAN", "CAPITAL CITY",
             "GENERAL SOMEONE"}) == {"AAALAND", "AAALANDIAN"}
        assert _country_form_names(
            conn, "BBB", {"BBBLAND", "TARGET CITY"}) == {"BBBLAND"}
    finally:
        conn.close()


def test_event_detail_default_as_of(event_env):
    data = _get(event_env, iso1="AAA", iso2="BBB")
    assert data["as_of"] == "2026-09-30"
    assert data["windows"]["28"]["n_events"] == 6


def test_event_detail_unknown_dyad(event_env):
    data = _get(event_env, iso1="CCC", iso2="DDD")
    assert data["as_of"] is None
    assert data["windows"]["28"]["n_events"] == 0
    assert data["windows"]["28"]["map_points"] == []
    assert data["adm1_compare"] == []


def test_event_detail_503_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("MIRROR_SQLITE", raising=False)
    monkeypatch.delenv("DDA_SIMPLEXES_JSON", raising=False)
    monkeypatch.setenv("API_VERSION_MODE", "strict")
    client = TestClient(create_app())
    resp = client.get("/dev/event-detail",
                      params={"iso1": "AAA", "iso2": "BBB"})
    assert resp.status_code == 503
