"""DEV event-detail store: event-level aggregates from the mirror.

Backs GET /dev/event-detail (v0.1 demo, Event detail page —
Dan, 2026-11-16). Reads the poc_base mirror's gdelt_events_core
(+ gdelt_geos / gdelt_actors / gdelt_codes_actor_subtype) for one
directed dyad over the 28 days ending at an as-of, and returns
aggregates only — no raw event rows leave the backend:

- map points (city/state-level geos): event counts, reporting
  volume (summed min_num_mentions), mean first-mention tone, and
  the Lund composition of the location's mapped events;
- ADM1 counts for the 28d window with the last-7d counts alongside;
- ranked named actors below the country level, both sides;
- target-side sector counts from actor2 parent codes;
- first-mention tone buckets + means, reporting-intensity means,
  and first-mention outlet domains;
- source-actor composition points (events vs mean Lund phase);
- a source-actor subtype-1 rollup with its coded share.

Compositions use the SAME crosswalk the rest of the backend
serves (DDA_SIMPLEXES_JSON via dev_dda_store.load_simplexes, with
the JOINT_DELTA zero replacement): the mirror's own soc_long copy
carries fewer mapped codes (245 vs 286 — flagged in the Event
detail proposal, 2026-11-16), and the mirror DDA holds no p
columns, so serve-time consistency argues for the backend copy.
Catch-all codes (no simplex) count toward every volume figure
and toward no composition; each window reports its mapped share.

An actor counts as country-level when its name is one of the
country's bare-country forms — the country name itself, its
formal variants, and its demonym (RUSSIA / RUSSIAN / RUSSIAN
FEDERATION for RUS). The form set is derived per country from
gdelt_codes_country's name plus a stem rule over that country's
own actor names, with a small table of irregular demonyms
(FRENCH, DUTCH, AMERICAN, ...). This is a display heuristic,
not a statistic: GDELT types city actors with the bare country
code too (116 distinct RUS names carry type_code = RUS,
including MOSCOW), so no record-structural rule separates the
bare country actor from a city. Sector-typed records carrying
a country name (e.g. UKRAINE with type UKRGOV) are excluded
from the named-actor views by the same rule; they remain
visible in the sector strip and subtype rollup.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .dev_dda_store import JOINT_DELTA

MAP_GEO_TYPES = ("WORLD_CITY", "US_CITY", "WORLD_STATE", "US_STATE")
TONE_EDGES = (-7.5, -5.0, -2.5, 0.0, 2.5)
WINDOWS = (7, 28)


def connect(path: Path) -> sqlite3.Connection:
    """Fresh read-only connection (same discipline as store.py)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def clean_simplexes(
    simplexes: dict[str, tuple[float, ...] | None],
) -> dict[str, tuple[float, ...]]:
    """Usable per-code simplexes: drop None, zeros become
    JOINT_DELTA, rescale to sum 1 (dev_dda_store's joint rule)."""
    out: dict[str, tuple[float, ...]] = {}
    for code, simplex in simplexes.items():
        if simplex is None:
            continue
        replaced = [JOINT_DELTA if v == 0 else float(v) for v in simplex]
        total = sum(replaced)
        if total > 0:
            out[code] = tuple(v / total for v in replaced)
    return out


#: Irregular demonyms the stem rule cannot reach (the country
#: name's stem is not a prefix of the demonym).
_DEMONYM_EXCEPTIONS: dict[str, set[str]] = {
    "USA": {"AMERICAN"}, "GBR": {"BRITISH", "ENGLISH"},
    "FRA": {"FRENCH"}, "NLD": {"DUTCH"}, "ESP": {"SPANISH"},
    "DEU": {"GERMAN"}, "GRC": {"GREEK"}, "DNK": {"DANISH"},
    "SWE": {"SWEDISH"}, "FIN": {"FINNISH"}, "NOR": {"NORWEGIAN"},
    "POL": {"POLISH"}, "TUR": {"TURKISH"}, "THA": {"THAI"},
    "CHE": {"SWISS"}, "IRL": {"IRISH"}, "ITA": {"ITALIAN"},
    "PRT": {"PORTUGUESE"}, "CZE": {"CZECH"}, "PHL": {"FILIPINO"},
}


def _country_form_names(
    conn: sqlite3.Connection, iso: str, candidates: set[str],
) -> set[str]:
    """Bare-country actor names for one ISO code, derived from
    the country's name in gdelt_codes_country: the name itself,
    formal variants starting with it, short prefixes of it, and
    single-word stem variants (demonyms and their misspellings,
    e.g. UKRAINIAN / UKRAINA for UKRAINE), plus the irregular-
    demonym table. Candidates are the actor names present in
    the window slice (the only names the forms are ever checked
    against), so no scan of gdelt_actors is needed — the mirror
    carries no index on that table, and a DISTINCT scan costs
    ~1s per country.
    """
    row = conn.execute(
        "SELECT full_name FROM gdelt_codes_country WHERE code = ?",
        (iso,),
    ).fetchone()
    base = (row[0] if row and row[0] else iso).upper()
    stem = base.rstrip("AEIOU") if len(base) > 4 else base
    forms = {
        name for name in _DEMONYM_EXCEPTIONS.get(iso, set())
        if name in candidates
    }
    for name in candidates:
        if not name:
            continue
        if (
            name == base
            or name.startswith(base)
            or (len(name) >= 4 and base.startswith(name))
            or (
                " " not in name
                and len(stem) >= 3
                and name.startswith(stem)
                and len(name) <= len(base) + 4
            )
        ):
            forms.add(name)
    return forms


def _mean_phase(simplex: list[float]) -> float:
    return sum((k + 1) * v for k, v in enumerate(simplex))


def _dominant(simplex: list[float]) -> int:
    return max(range(5), key=lambda k: simplex[k]) + 1


class _Acc:
    """Running composition + volume over a group of events."""

    __slots__ = ("n", "volume", "n_mapped", "simp", "tone_sum")

    def __init__(self) -> None:
        self.n = 0
        self.volume = 0
        self.n_mapped = 0
        self.simp = [0.0] * 5
        self.tone_sum = 0.0

    def add(self, row: dict, simplex: tuple[float, ...] | None) -> None:
        self.n += 1
        self.volume += row["mentions"] or 0
        if row["tone"] is not None:
            self.tone_sum += row["tone"]
        if simplex is not None:
            self.n_mapped += 1
            for k in range(5):
                self.simp[k] += simplex[k]

    def composition(self) -> dict | None:
        if not self.n_mapped:
            return None
        mean = [v / self.n_mapped for v in self.simp]
        return {
            "simplex": [round(v, 6) for v in mean],
            "dominant_phase": _dominant(mean),
            "mean_phase": round(_mean_phase(mean), 4),
        }


def _window_payload(
    rows: list[dict],
    clean: dict[str, tuple[float, ...]],
    forms1: set[str],
    forms2: set[str],
    subtype_labels: dict[str, str],
) -> dict:
    n = len(rows)
    n_mapped = 0
    geo_acc: dict[int, _Acc] = {}
    geo_meta: dict[int, dict] = {}
    n_country_geo = 0
    adm1: dict[str, int] = {}
    src_names: dict[str, int] = {}
    tgt_names: dict[str, int] = {}
    sectors: dict[str, int] = {}
    sectors_coded = 0
    tone_buckets = [0] * 6
    tone_sum = 0.0
    rep = {"sources": 0.0, "articles": 0.0, "mentions": 0.0}
    outlets: dict[str, int] = {}
    xy_acc: dict[str, _Acc] = {}
    sub_acc: dict[str, _Acc] = {}
    sub_actors: dict[str, set[str]] = {}
    sub_coded = 0

    for row in rows:
        simplex = clean.get(row["cameo_code"])
        if simplex is not None:
            n_mapped += 1
        if row["tone"] is not None:
            tone_sum += row["tone"]
            bucket = 0
            while bucket < 5 and row["tone"] >= TONE_EDGES[bucket]:
                bucket += 1
            tone_buckets[bucket] += 1
        rep["sources"] += row["sources"] or 0
        rep["articles"] += row["articles"] or 0
        rep["mentions"] += row["mentions"] or 0
        if row["domain"]:
            outlets[row["domain"]] = outlets.get(row["domain"], 0) + 1

        if row["geo_id"] is not None and row["geo_type"]:
            if row["adm1"]:
                adm1[row["adm1"]] = adm1.get(row["adm1"], 0) + 1
            if row["geo_type"] == "COUNTRY":
                n_country_geo += 1
            elif row["geo_type"] in MAP_GEO_TYPES:
                gid = row["geo_id"]
                if gid not in geo_acc:
                    geo_acc[gid] = _Acc()
                    geo_meta[gid] = {
                        "name": row["geo_name"], "adm1": row["adm1"],
                        "type": row["geo_type"], "lat": row["lat"],
                        "lon": row["lon"],
                    }
                geo_acc[gid].add(row, simplex)

        name1 = row["actor1_name"]
        if name1:
            if name1 not in forms1:
                src_names[name1] = src_names.get(name1, 0) + 1
                acc = xy_acc.setdefault(name1, _Acc())
                acc.add(row, simplex)
            sub = row["actor1_subtype"]
            if sub:
                sub_coded += 1
                acc = sub_acc.setdefault(sub, _Acc())
                acc.add(row, simplex)
                sub_actors.setdefault(sub, set()).add(name1)
        name2 = row["actor2_name"]
        if name2 and name2 not in forms2:
            tgt_names[name2] = tgt_names.get(name2, 0) + 1
        parent = row["parent2"]
        if parent:
            sectors_coded += 1
            sector = parent.split(":", 1)[1].split("-", 1)[0] \
                if ":" in parent else parent
            sectors[sector] = sectors.get(sector, 0) + 1

    map_points = []
    for gid, acc in geo_acc.items():
        comp = acc.composition()
        point = {
            **geo_meta[gid],
            "n_events": acc.n,
            "volume": acc.volume,
            "mean_tone": round(acc.tone_sum / acc.n, 4) if acc.n else None,
        }
        if comp is not None:
            point.update(comp)
        map_points.append(point)
    map_points.sort(key=lambda p: p["volume"], reverse=True)

    xy = []
    for name, acc in xy_acc.items():
        if acc.n < 2:
            continue
        comp = acc.composition()
        if comp is None:
            continue
        xy.append({
            "name": name, "n_events": acc.n, "volume": acc.volume,
            **comp,
        })
    xy.sort(key=lambda r: r["n_events"], reverse=True)

    subtypes = []
    for code, acc in sub_acc.items():
        comp = acc.composition()
        subtypes.append({
            "code": code,
            "label": subtype_labels.get(code, code),
            "n_events": acc.n,
            "n_actors": len(sub_actors.get(code, ())),
            "mean_phase": comp["mean_phase"] if comp else None,
            "dominant_phase": comp["dominant_phase"] if comp else None,
        })
    subtypes.sort(key=lambda r: r["n_events"], reverse=True)

    def _ranked(names: dict[str, int], cap: int = 8) -> list[dict]:
        return [
            {"name": name, "n": count}
            for name, count in sorted(
                names.items(), key=lambda kv: (-kv[1], kv[0]))[:cap]
        ]

    return {
        "n_events": n,
        "n_mapped": n_mapped,
        "mapped_share": round(n_mapped / n, 4) if n else None,
        "map_points": map_points[:200],
        "n_country_geo": n_country_geo,
        "actors_source": _ranked(src_names),
        "actors_target": _ranked(tgt_names),
        "target_sectors": {
            "coded": sectors_coded,
            "rows": [
                {"sector": sector, "n": count}
                for sector, count in sorted(
                    sectors.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            ],
        },
        "tone": {
            "buckets": tone_buckets,
            "mean": round(tone_sum / n, 4) if n else None,
        },
        "reporting": {
            key: (round(val / n, 4) if n else None)
            for key, val in rep.items()
        },
        "outlets": _ranked(outlets),
        "actor_xy": xy[:60],
        "subtypes": {
            "coded_share": round(sub_coded / n, 4) if n else None,
            "rows": subtypes,
        },
        "_adm1": adm1,
    }


def fetch_event_detail(
    conn: sqlite3.Connection,
    simplexes: dict[str, tuple[float, ...] | None],
    iso1: str,
    iso2: str,
    as_of: str | None,
) -> dict:
    """Aggregates for one dyad over the 28 days ending at as_of
    (default: the dyad's latest event date in the mirror)."""
    clean = clean_simplexes(simplexes)
    if as_of is None:
        row = conn.execute(
            "SELECT max(event_date) FROM gdelt_events_core "
            "WHERE actor1_country_code = ? AND actor2_country_code = ?",
            (iso1, iso2),
        ).fetchone()
        as_of = row[0] if row else None
    if as_of is None:
        return {
            "iso1": iso1, "iso2": iso2, "as_of": None,
            "windows": {str(w): _window_payload([], clean, set(), set(), {})
                        for w in WINDOWS},
            "adm1_compare": [],
        }
    end = date.fromisoformat(as_of)
    start28 = (end - timedelta(days=27)).isoformat()
    start7 = (end - timedelta(days=6)).isoformat()

    raw = conn.execute(
        """
        SELECT e.event_date AS d, e.cameo_code AS cameo_code,
               CAST(e.geo_id AS INTEGER) AS geo_id,
               g.full_name AS geo_name, g.adm1_code AS adm1,
               g.type_code AS geo_type, g.latitude AS lat,
               g.longitude AS lon,
               e.actor1_id AS a1, e.actor2_id AS a2,
               e.actor2_parent_code AS parent2,
               e.min_num_sources AS sources,
               e.min_num_articles AS articles,
               e.min_num_mentions AS mentions,
               e.init_avg_tone AS tone, e.init_source_url AS url
        FROM gdelt_events_core e
        LEFT JOIN gdelt_geos g ON g.id = CAST(e.geo_id AS INTEGER)
        WHERE e.event_date BETWEEN ? AND ?
          AND e.actor1_country_code = ? AND e.actor2_country_code = ?
        """,
        (start28, as_of, iso1, iso2),
    ).fetchall()
    rows = [dict(r) for r in raw]

    # The mirror carries no index on gdelt_actors, so resolving
    # names is the expensive step if done naively: an IN(...)
    # lookup per chunk costs a full 2.9M-row scan per chunk
    # (measured 4.3s for a heavy dyad), and iterating the table
    # in Python is no better (~4s of row overhead). A temp-table
    # join keeps the single unavoidable scan in C and probes
    # the temp PK per row: ~0.9s for the same dyad. (An index
    # on gdelt_actors(id) would make this moot; index builds
    # are the operator's call, not this endpoint's.)
    wanted = {r["a1"] for r in rows} | {r["a2"] for r in rows}
    wanted.discard(None)
    actors: dict[int, dict] = {}
    if wanted:
        conn.execute(
            "CREATE TEMP TABLE _want_actors (id INTEGER PRIMARY KEY)")
        try:
            conn.executemany(
                "INSERT INTO _want_actors VALUES (?)",
                [(aid,) for aid in wanted],
            )
            for aid, name, sub in conn.execute(
                "SELECT a.id, a.full_name, a.subtype1_code "
                "FROM gdelt_actors a "
                "JOIN _want_actors w ON w.id = a.id"
            ):
                actors[aid] = {"name": name, "subtype": sub}
        finally:
            conn.execute("DROP TABLE IF EXISTS _want_actors")
    for r in rows:
        a1 = actors.get(r["a1"]) or {}
        a2 = actors.get(r["a2"]) or {}
        r["actor1_name"] = a1.get("name")
        r["actor1_subtype"] = a1.get("subtype")
        r["actor2_name"] = a2.get("name")
        url = r["url"] or ""
        domain = (
            url.split("://", 1)[-1].split("/", 1)[0].lower()
            if "://" in url else None
        )
        if domain and domain.startswith("www."):
            domain = domain[4:]
        r["domain"] = domain

    names1 = {r["actor1_name"] for r in rows if r["actor1_name"]}
    names2 = {r["actor2_name"] for r in rows if r["actor2_name"]}
    forms1 = _country_form_names(conn, iso1, names1)
    forms2 = _country_form_names(conn, iso2, names2)
    subtype_labels = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT code, label FROM gdelt_codes_actor_subtype")
    }

    windows: dict[str, dict] = {}
    adm1_by_window: dict[int, dict[str, int]] = {}
    for w in WINDOWS:
        start = start7 if w == 7 else start28
        sub = [r for r in rows if r["d"] >= start]
        payload = _window_payload(sub, clean, forms1, forms2,
                                  subtype_labels)
        adm1_by_window[w] = payload.pop("_adm1")
        windows[str(w)] = payload
    adm1_compare = [
        {"adm1": adm1, "n28": count,
         "n7": adm1_by_window[7].get(adm1, 0)}
        for adm1, count in sorted(
            adm1_by_window[28].items(), key=lambda kv: (-kv[1], kv[0])
        )[:8]
    ]
    return {
        "iso1": iso1, "iso2": iso2, "as_of": as_of,
        "windows": windows, "adm1_compare": adm1_compare,
    }
