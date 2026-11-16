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

Only events whose geo lies in the source or target country are
included (Dan, 2026-11-16: third-country locations confuse the
page's readers); events with no resolved geo are excluded too.
Every composition is computed under two weightings (Dan,
2026-11-16): by unique events — each matching record in the
mirror counts once — and by articles — each record weighted
by its min_num_articles. The composition itself is the
component-wise MEDIAN simplex across the group's mapped
events (Dan, 2026-11-16, replacing the mean after he reviewed
the Ottawa/USA_CAN case): each phase's value is the median
of that component (the articles-weighted median under the
articles weighting), renormalized to sum to 1 so it stays a
simplex; the summary reports that simplex, its dominant
phase, and its median phase (the phase centroid of the
median simplex). Component-wise medians mix components from
different events by construction — that is the statistic
Dan reviewed. (A per-event modal-phase variant and the
mean-simplex variant both shipped briefly and were removed
at Dan's direction; there is no fully unweighted variant —
the by-events scheme is the record-count weighting.)

Tone is aggregated three ways wherever it is displayed
(Dan, 2026-11-16): the mean (the original), the median, and
a modal bucket. The mode construction is Dan's: round
init_avg_tone to 0 decimals (half away from zero), exclude
rounded zeros entirely, bucket the rest into 2.0-point
buckets ({1, 2}, {3, 4}, ... and {-2, -1}, {-4, -3}, ...),
and report the modal bucket's midpoint; ties resolve toward
the bucket closest to zero, then to the negative bucket. A
group whose tones all round to zero has no mode (None).

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

import math
import sqlite3
import statistics
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


def _round0(value: float) -> int:
    """Round to 0 decimals, half away from zero (the plain
    reading of 'round to 0 decimals'; Python's round() would
    banker's-round exact halves)."""
    return int(math.floor(abs(value) + 0.5)) * (
        -1 if value < 0 else 1)


def _tone_mode(tones: list[float]) -> float | None:
    """Modal 2.0-point bucket of the rounded tones, zeros
    excluded, reported at the bucket midpoint (Dan's
    construction; see the module docstring)."""
    counts: dict[int, int] = {}
    for tone in tones:
        r = _round0(tone)
        if r == 0:
            continue
        if r > 0:
            low = (r - 1) // 2 * 2 + 1
        else:
            low = -(((-r - 1) // 2) * 2 + 2)
        counts[low] = counts.get(low, 0) + 1
    if not counts:
        return None
    best = max(
        counts,
        key=lambda lo: (counts[lo], -abs(lo + 0.5), -lo))
    return best + 0.5


def _tone_stats(tones: list[float]) -> dict:
    """Mean / median / mode for a group of first-mention
    tones (each rounded to 4 decimals; mode at its natural
    1-decimal midpoint)."""
    if not tones:
        return {"mean": None, "median": None, "mode": None}
    return {
        "mean": round(sum(tones) / len(tones), 4),
        "median": round(statistics.median(tones), 4),
        "mode": _tone_mode(tones),
    }


def _mean_phase(simplex: list[float]) -> float:
    return sum((k + 1) * v for k, v in enumerate(simplex))


def _dominant(simplex: list[float]) -> int:
    return max(range(5), key=lambda k: simplex[k]) + 1


class _Acc:
    """Running composition + volume over a group of events.

    Mapped events keep their (simplex, articles) pairs: the
    composition is a component-wise median, which cannot be
    accumulated as a running sum. Group sizes are bounded by
    the window slice, so the storage is proportional to the
    slice the endpoint already materializes.
    """

    __slots__ = ("n", "volume", "tones", "items")

    def __init__(self) -> None:
        self.n = 0
        self.volume = 0
        self.tones: list[float] = []
        self.items: list[tuple[tuple[float, ...], float]] = []

    @property
    def n_mapped(self) -> int:
        return len(self.items)

    def add(self, row: dict, simplex: tuple[float, ...] | None) -> None:
        self.n += 1
        self.volume += row["mentions"] or 0
        if row["tone"] is not None:
            self.tones.append(row["tone"])
        if simplex is not None:
            self.items.append((simplex, row["articles"] or 0))

    def _summarize(self, weighted: bool) -> dict | None:
        if not self.items:
            return None
        if weighted and not sum(a for _, a in self.items):
            return None
        med: list[float] = []
        for k in range(5):
            if weighted:
                pairs = sorted(
                    (sx[k], art) for sx, art in self.items)
                total = sum(w for _, w in pairs)
                acc_w = 0.0
                value = pairs[-1][0]
                for v, w in pairs:
                    acc_w += w
                    if acc_w >= total / 2:
                        value = v
                        break
                med.append(value)
            else:
                med.append(statistics.median(
                    [sx[k] for sx, _ in self.items]))
        total = sum(med)
        if total <= 0:
            return None
        simplex = [v / total for v in med]
        return {
            "simplex": [round(v, 6) for v in simplex],
            "dominant_phase": _dominant(simplex),
            "median_phase": round(_mean_phase(simplex), 4),
        }

    def composition(self) -> dict:
        return {
            "by_events": self._summarize(weighted=False),
            "by_articles": self._summarize(weighted=True),
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
    tones_all: list[float] = []
    rep = {"sources": 0.0, "articles": 0.0, "mentions": 0.0}
    outlets: dict[str, list] = {}
    pairs: dict[tuple[str, str], int] = {}
    xy_acc: dict[str, _Acc] = {}
    sub_acc: dict[str, _Acc] = {}
    sub_actors: dict[str, set[str]] = {}
    sub_coded = 0

    for row in rows:
        simplex = clean.get(row["cameo_code"])
        if simplex is not None:
            n_mapped += 1
        if row["tone"] is not None:
            tones_all.append(row["tone"])
            bucket = 0
            while bucket < 5 and row["tone"] >= TONE_EDGES[bucket]:
                bucket += 1
            tone_buckets[bucket] += 1
        rep["sources"] += row["sources"] or 0
        rep["articles"] += row["articles"] or 0
        rep["mentions"] += row["mentions"] or 0
        if row["domain"]:
            slot = outlets.setdefault(row["domain"], [0, []])
            slot[0] += 1
            if row["tone"] is not None:
                slot[1].append(row["tone"])

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
        if (name1 and name1 not in forms1
                and name2 and name2 not in forms2):
            key = (name1, name2)
            pairs[key] = pairs.get(key, 0) + 1
        parent = row["parent2"]
        if parent:
            sectors_coded += 1
            sector = parent.split(":", 1)[1].split("-", 1)[0] \
                if ":" in parent else parent
            sectors[sector] = sectors.get(sector, 0) + 1

    map_points = []
    for gid, acc in geo_acc.items():
        stats = _tone_stats(acc.tones)
        point = {
            **geo_meta[gid],
            "n_events": acc.n,
            "volume": acc.volume,
            "mean_tone": stats["mean"],
            "median_tone": stats["median"],
            "mode_tone": stats["mode"],
        }
        point.update(acc.composition())
        map_points.append(point)
    map_points.sort(key=lambda p: p["volume"], reverse=True)

    xy = []
    for name, acc in xy_acc.items():
        if acc.n < 2:
            continue
        comp = acc.composition()
        if comp["by_events"] is None:
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
            "by_events": comp["by_events"],
            "by_articles": comp["by_articles"],
        })
    subtypes.sort(key=lambda r: r["n_events"], reverse=True)

    pair_src: dict[str, int] = {}
    pair_tgt: dict[str, int] = {}
    for (s, t), count in pairs.items():
        pair_src[s] = pair_src.get(s, 0) + count
        pair_tgt[t] = pair_tgt.get(t, 0) + count
    top_src = [name for name, _ in sorted(
        pair_src.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
    top_tgt = [name for name, _ in sorted(
        pair_tgt.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
    pair_rows = [
        {"source": s, "target": t, "n": count}
        for (s, t), count in sorted(
            pairs.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
        if s in top_src and t in top_tgt
    ]

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
            **_tone_stats(tones_all),
        },
        "reporting": {
            key: (round(val / n, 4) if n else None)
            for key, val in rep.items()
        },
        "outlets": [
            {"name": name, "n": slot[0],
             **{f"{key}_tone": value
                for key, value in _tone_stats(slot[1]).items()}}
            for name, slot in sorted(
                outlets.items(), key=lambda kv: (-kv[1][0], kv[0]))[:8]
        ],
        "actor_pairs": {
            "sources": top_src, "targets": top_tgt, "rows": pair_rows,
        },
        "actor_xy": xy[:60],
        "subtypes": {
            "coded_share": round(sub_coded / n, 4) if n else None,
            "rows": subtypes,
        },
        "_adm1": adm1,
    }


def _adm1_labels(
    conn: sqlite3.Connection,
    rows: list[dict],
    adm1_compare: list[dict],
) -> None:
    """Attach a display label ("Moscow (RS48)") to each ADM1
    row: users do not know ADM1 codes (Dan, 2026-11-16). Name
    sources, in order: the COUNTRY geo's own name for a
    country-level ADM1 ('Russia'); a WORLD_STATE / US_STATE
    geo's name (first comma component) for a real region;
    otherwise the ADM1 component parsed from the commonest
    city geo name in the slice ('Moscow, Moskva, Russia' ->
    'Moskva'). Falls back to the bare code when no name exists.
    """
    if not adm1_compare:
        return
    wanted = [row["adm1"] for row in adm1_compare]
    state_names: dict[str, str] = {}
    for adm1_code, full_name in conn.execute(
        "SELECT adm1_code, full_name FROM gdelt_geos "
        "WHERE type_code IN ('WORLD_STATE', 'US_STATE') "
        f"AND adm1_code IN ({','.join('?' * len(wanted))})",
        wanted,
    ):
        if adm1_code not in state_names and full_name:
            state_names[adm1_code] = full_name.split(",")[0].strip()
    country_names: dict[str, str] = {}
    city_names: dict[str, dict[str, int]] = {}
    for r in rows:
        adm1 = r["adm1"]
        if adm1 not in wanted or not r["geo_name"]:
            continue
        if r["geo_type"] == "COUNTRY":
            country_names.setdefault(adm1, r["geo_name"])
        else:
            bucket = city_names.setdefault(adm1, {})
            bucket[r["geo_name"]] = bucket.get(r["geo_name"], 0) + 1
    for row in adm1_compare:
        adm1 = row["adm1"]
        name = country_names.get(adm1) or state_names.get(adm1)
        if name is None and city_names.get(adm1):
            commonest = max(
                city_names[adm1].items(), key=lambda kv: kv[1])[0]
            parts = commonest.split(", ")
            name = (", ".join(parts[1:-1]) if len(parts) >= 3
                    else parts[0])
        row["label"] = f"{name} ({adm1})" if name else adm1


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
               g.longitude AS lon, g.country_code AS geo_country,
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
    # Geo restriction (Dan, 2026-11-16): only events located in
    # the source or target country; third-country locations and
    # unresolved geos are excluded from every panel.
    rows = [dict(r) for r in raw
            if r["geo_country"] in (iso1, iso2)]

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
    _adm1_labels(conn, rows, adm1_compare)
    return {
        "iso1": iso1, "iso2": iso2, "as_of": as_of,
        "windows": windows, "adm1_compare": adm1_compare,
    }
