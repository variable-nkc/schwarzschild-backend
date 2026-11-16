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
simplex; the summary reports that simplex and its median
phase (the phase centroid of the median simplex). (The
dominant phase — the median simplex's largest component —
was removed from the payload on 2026-11-16 at Dan's
direction: the page now colours by a continuous gradient
on the median phase instead.) Component-wise medians mix
components from
different events by construction — that is the statistic
Dan reviewed. (A per-event modal-phase variant and the
mean-simplex variant both shipped briefly and were removed
at Dan's direction; there is no fully unweighted variant —
the by-events scheme is the record-count weighting.)

Tone is aggregated as the MEDIAN only, under both page
weightings (Dan, 2026-11-16, replacing the mean/median/mode
trio he had added that morning — he judged the pooled mean
and the bucketed mode carried no discriminating variance):
by_events is the plain median of the group's first-mention
tones, each event counting once; by_articles weights each
event by its min_num_articles and takes the
halfway-crossing value, the rule the simplex medians use.

Set shares (Dan, 2026-11-16): when the caller supplies
CAMEO set membership (cameo_groups.json, loaded by the
endpoint when DDA_SETS_JSON is configured), each window
also carries the five sets' shares of the slice — the
weight of events whose code is in the set over the weight
of ALL events in the slice, under both weightings (the
S0m-style denominator; codes in no set count in the
denominator only). The sets overlap, so the shares are
independent marginals and do not sum to 1. Without
membership the window's set_shares is None and the page
shows a note instead — the rest of the page does not
depend on the groups file.

Colouring supplements (Dan, 2026-11-16): source-actor
rows also carry their tone medians (the actor figure
colours by tone, Lund being its y-axis); outlet rows
carry a full Lund composition alongside their tone
medians (the outlets chart can colour by either); and
each map point carries hot_share_by_events /
hot_share_by_articles — the share of the point's own
events whose code is in critical_incidents ∪ escalatory.
Dan asked for the SUM of the two sets' shares; it is
computed as the union share instead, because 7 codes sit
in both sets and the literal sum double-counts their
events (it can exceed 1 — a colour scale needs a share).
Without set membership the hot shares are None. Each
point also carries tilt_by_events / tilt_by_articles:
the hot-family share (escalatory ∪ instability ∪
critical_incidents) minus the calm-family share
(stability ∪ deescalatory) at that point — a lean index
in the app's balance grammar, added so a dyad that is
unstable-but-not-severe (CHN_TWN) reads differently from
a calm one (CHN_USA), which the severe share alone
cannot distinguish. 33 codes sit in both families, so
the tilt is a difference of marginals, not a partition.
The composition, tone medians, hot shares and tilts are
computed by one accumulator, so every grouping on the
page (regions, actors, pair cells, outlets, subtypes)
exposes the same summary shape and the page's single
colour-by setting fills them all.

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
import statistics
from datetime import date, timedelta
from pathlib import Path

from .dev_dda_store import JOINT_DELTA

MAP_GEO_TYPES = ("WORLD_CITY", "US_CITY", "WORLD_STATE", "US_STATE")
#: Tone histogram bins (Dan, 2026-11-16): five bins over
#: the locked -10..0 scale — tone <= -8 ("8+"), then
#: -8..-6, -6..-4, -4..-2, and everything above -2 ("< 2",
#: positives included). Bucket index 0 is the severe end.
TONE_EDGES = (-8.0, -6.0, -4.0, -2.0)
#: A Lund state "holds" an event when the event's simplex
#: puts strictly more than this on the phase (Dan,
#: 2026-11-16). Strictness keeps near-uniform catch-all
#: simplexes (0.2 per component) out of every state.
STATE_THRESHOLD = 0.20
WINDOWS = (7, 28)
#: The five CAMEO action sets, in the app's display order
#: (mirrors dev_dda_store.ACTION_SETS).
SET_ORDER = ("escalatory", "instability", "stability",
             "deescalatory", "critical_incidents")


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


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    """Halfway-crossing weighted median of (value, weight)
    pairs — the rule the simplex medians use."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    acc_w = 0.0
    for value, w in ordered:
        acc_w += w
        if acc_w >= total / 2:
            return float(value)
    return float(ordered[-1][0])


def _tone_medians(pairs: list[tuple[float, float]]) -> dict:
    """Median first-mention tone for one group under both
    weightings (see the module docstring). Pairs are
    (tone, min_num_articles)."""
    if not pairs:
        return {"by_events": None, "by_articles": None}
    return {
        "by_events": round(
            float(statistics.median([t for t, _ in pairs])), 4),
        "by_articles": round(_weighted_median(pairs), 4),
    }


def _mean_phase(simplex: list[float]) -> float:
    return sum((k + 1) * v for k, v in enumerate(simplex))


class _Acc:
    """Running composition + volume over a group of events.

    Mapped events keep their (simplex, articles) pairs: the
    composition is a component-wise median, which cannot be
    accumulated as a running sum. Group sizes are bounded by
    the window slice, so the storage is proportional to the
    slice the endpoint already materializes.

    Every accumulator also carries the set-membership
    counters (severe / tilt-hot / calm, events + articles)
    behind hot_share and tilt, so EVERY grouping on the page
    — map points, regions, actors, pairs, outlets, subtypes
    — exposes the same metric summary and the page's single
    colour-by setting can fill any of them (Dan, 2026-11-16).
    """

    __slots__ = ("n", "volume", "articles", "tone_pairs",
                 "items", "sev", "thot", "calm")

    def __init__(self) -> None:
        self.n = 0
        self.volume = 0
        self.articles = 0
        self.tone_pairs: list[tuple[float, float]] = []
        self.items: list[tuple[tuple[float, ...], float]] = []
        self.sev = [0, 0]
        self.thot = [0, 0]
        self.calm = [0, 0]

    @property
    def n_mapped(self) -> int:
        return len(self.items)

    def add(self, row: dict, simplex: tuple[float, ...] | None,
            flags: tuple[bool, bool, bool] | None = None) -> None:
        self.n += 1
        self.volume += row["mentions"] or 0
        art = row["articles"] or 0
        self.articles += art
        if flags is not None:
            if flags[0]:
                self.sev[0] += 1
                self.sev[1] += art
            if flags[1]:
                self.thot[0] += 1
                self.thot[1] += art
            if flags[2]:
                self.calm[0] += 1
                self.calm[1] += art
        if row["tone"] is not None:
            self.tone_pairs.append((row["tone"], art))
        if simplex is not None:
            self.items.append((simplex, art))

    def metrics(self, with_sets: bool) -> dict:
        """Tone medians + hot share + tilt under both
        weightings — the flat metric keys every grouping
        shares. Hot/tilt are None without set membership."""
        medians = _tone_medians(self.tone_pairs)
        out = {
            "tone_by_events": medians["by_events"],
            "tone_by_articles": medians["by_articles"],
            "hot_share_by_events": None,
            "hot_share_by_articles": None,
            "tilt_by_events": None,
            "tilt_by_articles": None,
        }
        if with_sets and self.n:
            out["hot_share_by_events"] = round(
                self.sev[0] / self.n, 4)
            out["tilt_by_events"] = round(
                self.thot[0] / self.n - self.calm[0] / self.n, 4)
        if with_sets and self.articles:
            out["hot_share_by_articles"] = round(
                self.sev[1] / self.articles, 4)
            out["tilt_by_articles"] = round(
                self.thot[1] / self.articles
                - self.calm[1] / self.articles, 4)
        return out

    def summary(self, with_sets: bool) -> dict:
        """Composition (by_events / by_articles) + metrics:
        the uniform per-group payload the page colours by."""
        return {**self.composition(), **self.metrics(with_sets)}

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
    sets: dict[str, set[str]] | None = None,
) -> dict:
    n = len(rows)
    n_mapped = 0
    geo_acc: dict[int, _Acc] = {}
    geo_meta: dict[int, dict] = {}
    n_country_geo = 0
    adm1_acc: dict[str, _Acc] = {}
    tgt_acc: dict[str, _Acc] = {}
    sectors: dict[str, int] = {}
    sectors_coded = 0
    tone_buckets = [0] * 5
    tone_bucket_articles = [0] * 5
    tone_pairs_all: list[tuple[float, float]] = []
    state_counts = [[0, 0] for _ in range(5)]
    rep = {"sources": 0.0, "articles": 0.0, "mentions": 0.0}
    outlets: dict[str, _Acc] = {}
    hot_codes: set[str] = set()
    calm_codes: set[str] = set()
    tilt_hot_codes: set[str] = set()
    if sets is not None:
        hot_codes = (set(sets.get("critical_incidents", ()))
                     | set(sets.get("escalatory", ())))
        calm_codes = (set(sets.get("stability", ()))
                      | set(sets.get("deescalatory", ())))
        tilt_hot_codes = (hot_codes
                          | set(sets.get("instability", ())))
    pair_acc: dict[tuple[str, str], _Acc] = {}
    xy_acc: dict[str, _Acc] = {}
    sub_acc: dict[str, _Acc] = {}
    sub_actors: dict[str, set[str]] = {}
    sub_coded = 0
    set_counts: dict[str, list[int]] = {}

    for row in rows:
        flags = None
        if sets is not None:
            code = row["cameo_code"]
            flags = (code in hot_codes, code in tilt_hot_codes,
                     code in calm_codes)
            for name in SET_ORDER:
                if code in sets.get(name, ()):
                    slot = set_counts.setdefault(name, [0, 0])
                    slot[0] += 1
                    slot[1] += row["articles"] or 0
        simplex = clean.get(row["cameo_code"])
        if simplex is not None:
            n_mapped += 1
            for k in range(5):
                if simplex[k] > STATE_THRESHOLD:
                    state_counts[k][0] += 1
                    state_counts[k][1] += row["articles"] or 0
        if row["tone"] is not None:
            tone_pairs_all.append(
                (row["tone"], row["articles"] or 0))
            bucket = 0
            while bucket < 4 and row["tone"] >= TONE_EDGES[bucket]:
                bucket += 1
            tone_buckets[bucket] += 1
            tone_bucket_articles[bucket] += row["articles"] or 0
        rep["sources"] += row["sources"] or 0
        rep["articles"] += row["articles"] or 0
        rep["mentions"] += row["mentions"] or 0
        if row["domain"]:
            acc_o = outlets.setdefault(row["domain"], _Acc())
            acc_o.add(row, simplex, flags)

        if row["geo_id"] is not None and row["geo_type"]:
            if row["adm1"]:
                adm1_acc.setdefault(row["adm1"], _Acc()).add(
                    row, simplex, flags)
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
                geo_acc[gid].add(row, simplex, flags)

        name1 = row["actor1_name"]
        if name1:
            if name1 not in forms1:
                acc = xy_acc.setdefault(name1, _Acc())
                acc.add(row, simplex, flags)
            sub = row["actor1_subtype"]
            if sub:
                sub_coded += 1
                acc = sub_acc.setdefault(sub, _Acc())
                acc.add(row, simplex, flags)
                sub_actors.setdefault(sub, set()).add(name1)
        name2 = row["actor2_name"]
        if name2 and name2 not in forms2:
            tgt_acc.setdefault(name2, _Acc()).add(
                row, simplex, flags)
        if (name1 and name1 not in forms1
                and name2 and name2 not in forms2):
            pair_acc.setdefault((name1, name2), _Acc()).add(
                row, simplex, flags)
        parent = row["parent2"]
        if parent:
            sectors_coded += 1
            sector = parent.split(":", 1)[1].split("-", 1)[0] \
                if ":" in parent else parent
            sectors[sector] = sectors.get(sector, 0) + 1

    with_sets = sets is not None
    map_points = []
    for gid, acc in geo_acc.items():
        point = {
            **geo_meta[gid],
            "n_events": acc.n,
            "articles": acc.articles,
            "volume": acc.volume,
            **acc.summary(with_sets),
        }
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
            "name": name, "n_events": acc.n,
            "articles": acc.articles, "volume": acc.volume,
            **acc.summary(with_sets),
        })
    xy.sort(key=lambda r: r["n_events"], reverse=True)

    subtypes = []
    for code, acc in sub_acc.items():
        subtypes.append({
            "code": code,
            "label": subtype_labels.get(code, code),
            "n_events": acc.n,
            "articles": acc.articles,
            "n_actors": len(sub_actors.get(code, ())),
            **acc.summary(with_sets),
        })
    subtypes.sort(key=lambda r: r["n_events"], reverse=True)

    pair_src: dict[str, int] = {}
    pair_tgt: dict[str, int] = {}
    for (s, t), acc in pair_acc.items():
        pair_src[s] = pair_src.get(s, 0) + acc.n
        pair_tgt[t] = pair_tgt.get(t, 0) + acc.n
    top_src = [name for name, _ in sorted(
        pair_src.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
    top_tgt = [name for name, _ in sorted(
        pair_tgt.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
    pair_rows = [
        {"source": s, "target": t, "n": acc.n,
         "articles": acc.articles, **acc.summary(with_sets)}
        for (s, t), acc in sorted(
            pair_acc.items(),
            key=lambda kv: (-kv[1].n, kv[0][0], kv[0][1]))
        if s in top_src and t in top_tgt
    ]

    def _ranked(accs: dict[str, _Acc],
                cap: int = 8) -> list[dict]:
        return [
            {"name": name, "n": acc.n, "articles": acc.articles,
             **acc.summary(with_sets)}
            for name, acc in sorted(
                accs.items(), key=lambda kv: (-kv[1].n, kv[0]))[:cap]
        ]

    total_articles = rep["articles"]
    state_shares = [
        {"state": f"p{k + 1}",
         "n_events": state_counts[k][0],
         "n_articles": state_counts[k][1],
         "share_by_events": (
             round(state_counts[k][0] / n, 4) if n else None),
         "share_by_articles": (
             round(state_counts[k][1] / total_articles, 4)
             if total_articles else None)}
        for k in range(5)
    ]
    set_shares = None
    if sets is not None:
        set_shares = []
        for name in SET_ORDER:
            n_set, a_set = set_counts.get(name, [0, 0])
            set_shares.append({
                "set": name,
                "n_events": n_set,
                "n_articles": a_set,
                "share_by_events": (
                    round(n_set / n, 4) if n else None),
                "share_by_articles": (
                    round(a_set / total_articles, 4)
                    if total_articles else None),
            })
    return {
        "n_events": n,
        "n_mapped": n_mapped,
        "mapped_share": round(n_mapped / n, 4) if n else None,
        "map_points": map_points[:200],
        "n_country_geo": n_country_geo,
        "actors_source": _ranked(xy_acc),
        "actors_target": _ranked(tgt_acc),
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
            "bucket_articles": tone_bucket_articles,
            **{f"median_{key}": value for key, value
               in _tone_medians(tone_pairs_all).items()},
        },
        "state_shares": state_shares,
        "set_shares": set_shares,
        "reporting": {
            key: (round(val / n, 4) if n else None)
            for key, val in rep.items()
        },
        "outlets": [
            {"name": name, "n": acc_o.n,
             "articles": acc_o.articles,
             **acc_o.summary(with_sets)}
            for name, acc_o in sorted(
                outlets.items(), key=lambda kv: (-kv[1].n, kv[0]))[:8]
        ],
        "actor_pairs": {
            "sources": top_src, "targets": top_tgt, "rows": pair_rows,
        },
        "actor_xy": xy[:60],
        "subtypes": {
            "coded_share": round(sub_coded / n, 4) if n else None,
            "rows": subtypes,
        },
        "_adm1_acc": adm1_acc,
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
    sets: dict[str, set[str]] | None = None,
) -> dict:
    """Aggregates for one dyad over the 28 days ending at as_of
    (default: the dyad's latest event date in the mirror).
    `sets` is optional CAMEO set membership for the set-shares
    block; None leaves set_shares None in every window."""
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
            "windows": {str(w): _window_payload(
                [], clean, set(), set(), {}, sets)
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
    adm1_by_window: dict[int, dict[str, _Acc]] = {}
    for w in WINDOWS:
        start = start7 if w == 7 else start28
        sub = [r for r in rows if r["d"] >= start]
        payload = _window_payload(sub, clean, forms1, forms2,
                                  subtype_labels, sets)
        adm1_by_window[w] = payload.pop("_adm1_acc")
        windows[str(w)] = payload
    with_sets = sets is not None

    def _adm1_summary(acc: _Acc | None) -> dict | None:
        return acc.summary(with_sets) if acc is not None else None

    adm1_compare = [
        {"adm1": adm1, "n28": acc28.n,
         "n7": (adm1_by_window[7][adm1].n
                if adm1 in adm1_by_window[7] else 0),
         "a28": acc28.articles,
         "a7": (adm1_by_window[7][adm1].articles
                if adm1 in adm1_by_window[7] else 0),
         "w28": _adm1_summary(acc28),
         "w7": _adm1_summary(adm1_by_window[7].get(adm1))}
        for adm1, acc28 in sorted(
            adm1_by_window[28].items(),
            key=lambda kv: (-kv[1].n, kv[0])
        )[:8]
    ]
    _adm1_labels(conn, rows, adm1_compare)
    return {
        "iso1": iso1, "iso2": iso2, "as_of": as_of,
        "windows": windows, "adm1_compare": adm1_compare,
    }
