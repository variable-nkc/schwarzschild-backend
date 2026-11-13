"""Per-country rollup aggregates + country polygons for the
Context choropleth (DEV ONLY — not part of the v1 contract).

Metrics (Dan, 2026-11-03, as agreed after review):
- vol_change_log2: log2 of week-over-week trailing-7d volume,
  v_7d(t) / v_7d(t-7). Null when either week's volume is zero
  (a log cannot represent 0, and an infinite spike would
  break the color scale). The x ratio rides alongside for
  tooltips.
- log10_vol: log10 of trailing-7d volume. Null at zero volume.
- temp_p75: 75th percentile (linear interpolation) of dyad
  temperature over dyads with v_7d >= 30 (the ratified PCS
  volume floor) and non-null temperature. Null when no dyad
  qualifies.
- mode_label: modal label over SUFFICIENT dyads only
  (insufficient excluded — it is ~98% of the panel, so the
  unfiltered mode would paint every country one color).
  Ties break by triage priority (hot > drifting > atypical >
  steady). Null when no sufficient dyad exists.
- share_flagged: share of the country's dyads labelled hot,
  drifting, or atypical (gallery precedent), insufficient
  included in the denominator.
- n_flagged: the flagged count behind that share.
- pill_max: the highest validated exceedance (exc45_max from
  the predictive build's dyad_predictive_summary at the
  effective as-of) over the dyads feeding the country, when
  the caller supplies a pill dict; null otherwise. Annotation
  only — it never alters a rollup metric.

Grouping is the involvement view by default (gallery
precedent): a dyad counts toward BOTH its countries, so each
dyad's volume counts twice panel-wide — captioned on the
figure, never hidden. The ``view`` argument can restrict each
dyad to its target (iso2) or source (iso1) slot instead, and
``dyads`` can scope the fold to a caller-supplied dyad set
(the Scan context map): BOTH slices are scoped identically,
so vol_change stays a like-for-like ratio. Ratios are
computed from summed volumes, never averaged from dyad
ratios. "Previous day" is the effective
as-of: the latest rollup as-of <= requested (or latest
overall); the prior slice resolves the same way at
effective minus 7 days, and its actual as-of is returned.

Geometry comes from the mirror's gdelt_codes_country
(bounding_box as a Postgres array literal, polygon_type
MultiPolygon for every non-null row on this mirror; a
Polygon row is wrapped defensively). Rows with a null box
or null code are skipped — region aggregates have no
polygon and simply do not render.

Display cost: the raw mirror shapes carry ~521k points
(~20 MB of JSON) — far too heavy for a 420px overview map,
and the endpoint measured ~8 s warm on that payload alone.
So polygons are simplified (Douglas-Peucker, 0.1 degrees —
about 0.2 px at this map size, visually lossless) and
rounded to 3 decimals before serving, and the simplified
geometry is cached in-process keyed by a fingerprint of
the codes table (row count, non-null count, total box
bytes), so the mirror can change without a stale cache
and tests with different fixtures never leak geometry.
Rings are also rewound to the orientation d3-geo reads
(exteriors positive spherical area, holes negative):
GDELT's table mixes windings, and a reversed ring renders
in d3 as the sphere minus the polygon — on 2026-11-03
eight features carried one, Vietnam's drawn last, so its
complement painted the whole Context map one color.
Degenerate triangles (vertices collinear, enclosing no
area) are dropped outright: d3 renders those as the
sphere too, and one Norwegian island triangle survived
the rewind to keep the oceans orange until it was cut.
First request per fingerprint pays the simplification;
later requests pay only the aggregation. If the mirror
ever changes shape materially, the tolerance lives here.

Performance note: the slice query selects only iso1, iso2,
v_7d, temperature, label at one as-of — exactly the
columns of serving index idx_slice_cover, so on an indexed
serving copy it is a covering scan, not 26k table lookups.
Two slices (current + prior) are read, then aggregated in
Python. If the plan ever stops covering, this endpoint is
the first place that will feel it.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta

FLAG_LABELS = ("hot", "drifting", "atypical")
# Tie-break for the modal label: highest triage priority wins.
MODE_PRIORITY = ("hot", "drifting", "atypical", "steady")
TEMP_VOLUME_FLOOR = 30.0
# Display simplification: degrees, ~0.2 px at overview-map size.
SIMPLIFY_TOL_DEG = 0.1
COORD_DECIMALS = 3

_GEOMETRY_CACHE: dict = {}

_SLICE_SQL = (
    "SELECT iso1, iso2, v_7d, temperature, label"
    " FROM dyad_status_rollup WHERE as_of = ?"
)


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _latest_as_of(con: sqlite3.Connection,
                  requested: str | None) -> str | None:
    if requested is None:
        row = con.execute(
            "SELECT MAX(as_of) AS m FROM dyad_status_rollup").fetchone()
    else:
        row = con.execute(
            "SELECT MAX(as_of) AS m FROM dyad_status_rollup"
            " WHERE as_of <= ?", (requested,)).fetchone()
    return row["m"] if row is not None else None


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile over an already-sorted list."""
    if not sorted_values:
        raise ValueError("percentile of empty list")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct / 100.0 * (len(sorted_values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[int(rank)]
    frac = rank - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _parse_polygon(box: str, polygon_type: str | None) -> list | None:
    try:
        coords = json.loads(box.replace("{", "[").replace("}", "]"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None
    if polygon_type == "Polygon":
        coords = [coords]
    if not isinstance(coords, list) or not coords:
        return None
    return coords


def _perp_distance(px: float, py: float, ax: float, ay: float,
                   bx: float, by: float) -> float:
    """Perpendicular distance from P to segment AB (degrees,
    equirectangular — fine for display simplification)."""
    dx, dy = bx - ax, by - ay
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify_ring(ring: list, tol: float = SIMPLIFY_TOL_DEG) -> list:
    """Douglas-Peucker over one ring, iterative (no recursion
    limit). Endpoints are always kept, so a closed ring stays
    closed. Rings collapsing below 4 points are dropped by
    the caller (they cannot render as polygons)."""
    if len(ring) <= 2:
        return list(ring)
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        start, end = stack.pop()
        ax, ay = ring[start]
        bx, by = ring[end]
        farthest, farthest_d = -1, tol
        for i in range(start + 1, end):
            d = _perp_distance(ring[i][0], ring[i][1], ax, ay, bx, by)
            if d > farthest_d:
                farthest, farthest_d = i, d
        if farthest != -1:
            keep[farthest] = True
            stack.append((start, farthest))
            stack.append((farthest, end))
    return [[round(x, COORD_DECIMALS), round(y, COORD_DECIMALS)]
            for i, (x, y) in enumerate(ring) if keep[i]]


def _spherical_signed_area(ring: list) -> float:
    """Chamberlain-Duquette signed spherical area. Longitude
    differences wrap to [-180, 180], so antimeridian rings
    measure correctly. Sign convention verified against the
    renderer (2026-11-03): a positive area renders as the
    polygon itself in d3-geo; a negative area renders as
    the sphere minus the polygon."""
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(ring, ring[1:]):
        dlon = math.radians(((lon2 - lon1 + 180.0) % 360.0) - 180.0)
        total += dlon * (2.0 + math.sin(math.radians(lat1))
                         + math.sin(math.radians(lat2)))
    return total / 2.0


def _rewind_ring(ring: list, exterior: bool) -> list:
    """Orient one ring the way d3-geo reads it: exteriors
    positive area, holes negative. GDELT's table mixes
    windings — on 2026-11-03 eight features carried a
    reversed ring (Vietnam's tiny island triangle among
    them); drawn last, its complement painted the whole
    Context map one color. Reversal preserves closure: a
    closed ring reversed still starts and ends on the same
    point."""
    area = _spherical_signed_area(ring)
    if (exterior and area < 0) or (not exterior and area > 0):
        return list(reversed(ring))
    return ring


def _is_degenerate_triangle(ring: list) -> bool:
    """True for a 4-point ring (triangle + closure) whose
    vertices are collinear: the ring encloses no area, so
    d3-geo renders it as the sphere, painting the whole
    map its color (2026-11-03: one such Norwegian island
    triangle survived the rewind and kept the Context
    oceans orange). Smallest-angle sine below 0.01
    (~0.6 degrees) counts as collinear — genuine small
    triangles (smallest sine ~0.05, e.g. Vietnam's island)
    survive by a wide margin. Only triangles are tested:
    a sharp vertex in a larger ring is legitimate
    coastline detail, not a degenerate ring."""
    if len(ring) != 4:
        return False
    pts = ring[:3]
    min_sin = 1.0
    for i in range(3):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % 3]
        cx, cy = pts[(i + 2) % 3]
        v1 = (bx - ax, by - ay)
        v2 = (cx - ax, cy - ay)
        n1 = math.hypot(*v1)
        n2 = math.hypot(*v2)
        if n1 == 0 or n2 == 0:
            return True
        min_sin = min(
            min_sin,
            abs(v1[0] * v2[1] - v1[1] * v2[0]) / (n1 * n2))
    return min_sin < 0.01


def _geometry_fingerprint(mirror_conn: sqlite3.Connection) -> tuple:
    row = mirror_conn.execute(
        "SELECT COUNT(*), COUNT(bounding_box),"
        " COALESCE(SUM(LENGTH(bounding_box)), 0)"
        " FROM gdelt_codes_country").fetchone()
    return tuple(row)


def _load_geometry(mirror_conn: sqlite3.Connection) -> list:
    """Simplified country features, cached per table fingerprint."""
    key = _geometry_fingerprint(mirror_conn)
    cached = _GEOMETRY_CACHE.get(key)
    if cached is not None:
        return cached
    features = []
    for iso_code, full_name, polygon_type, box in mirror_conn.execute(
            "SELECT iso_code, full_name, polygon_type, bounding_box"
            " FROM gdelt_codes_country"
            " WHERE bounding_box IS NOT NULL AND iso_code IS NOT NULL"):
        coords = _parse_polygon(box, polygon_type)
        if coords is None:
            continue
        simplified = []
        for polygon in coords:
            rings = []
            for ring_index, ring in enumerate(polygon):
                if len(ring) < 2:
                    continue
                simple = simplify_ring(ring)
                if len(simple) >= 4:
                    fixed = _rewind_ring(simple, ring_index == 0)
                    if _is_degenerate_triangle(fixed):
                        continue
                    rings.append(fixed)
            if rings:
                simplified.append(rings)
        if not simplified:
            continue
        features.append({
            "type": "Feature",
            "id": iso_code,
            "properties": {"name": full_name or iso_code},
            "geometry": {"type": "MultiPolygon",
                         "coordinates": simplified},
        })
    _GEOMETRY_CACHE[key] = features
    return features


def fetch_country_aggregates(
    rollup_conn: sqlite3.Connection,
    mirror_conn: sqlite3.Connection,
    as_of: str | None,
    dyads: set[tuple[str, str]] | None = None,
    view: str = "involvement",
    pill_by_dyad: dict[tuple[str, str], float | None] | None = None,
) -> dict:
    effective = _latest_as_of(rollup_conn, as_of)
    if effective is None:
        return {"status": "no_data", "as_of": None, "prior_as_of": None,
                "countries": [], "geometry": {"type": "FeatureCollection",
                                              "features": []}}
    prior_date = (date.fromisoformat(effective) - timedelta(days=7))
    prior_as_of = _latest_as_of(rollup_conn, prior_date.isoformat())

    def _feeds(iso1: str, iso2: str) -> tuple:
        # Which country slot(s) a counted dyad feeds.
        if view == "targets":
            return (iso2,)
        if view == "sources":
            return (iso1,)
        return (iso1, iso2)

    # Per-country accumulators. `current` tracks the countries
    # fed by the current slice — a country fed only in the
    # prior slice emits no row (all its current metrics would
    # be null anyway).
    acc: dict[str, dict] = {}
    current: set[str] = set()

    def _slot(country: str) -> dict:
        slot = acc.get(country)
        if slot is None:
            slot = {"vol_now": 0.0, "vol_prior": 0.0, "temps": [],
                    "label_counts": {}, "n_dyads": 0, "n_flagged": 0,
                    "pill_max": None}
            acc[country] = slot
        return slot

    for iso1, iso2, v_7d, _temp, label in rollup_conn.execute(
            _SLICE_SQL, (effective,)):
        if dyads is not None and (iso1, iso2) not in dyads:
            continue
        vol = float(v_7d) if v_7d is not None else 0.0
        pill = (pill_by_dyad.get((iso1, iso2))
                if pill_by_dyad is not None else None)
        for country in _feeds(iso1, iso2):
            if country is None:
                continue
            current.add(country)
            slot = _slot(country)
            slot["vol_now"] += vol
            slot["n_dyads"] += 1
            if label in FLAG_LABELS:
                slot["n_flagged"] += 1
            if label is not None and label != "insufficient":
                counts = slot["label_counts"]
                counts[label] = counts.get(label, 0) + 1
            if _temp is not None and vol >= TEMP_VOLUME_FLOOR:
                slot["temps"].append(float(_temp))
            if pill is not None:
                slot["pill_max"] = (
                    pill if slot["pill_max"] is None
                    else max(slot["pill_max"], pill))

    if prior_as_of is not None:
        for iso1, iso2, v_7d, _temp, _label in rollup_conn.execute(
                _SLICE_SQL, (prior_as_of,)):
            if dyads is not None and (iso1, iso2) not in dyads:
                continue
            vol = float(v_7d) if v_7d is not None else 0.0
            for country in _feeds(iso1, iso2):
                if country is None:
                    continue
                _slot(country)["vol_prior"] += vol

    countries = []
    for country in sorted(current):
        slot = acc[country]
        vol_now = slot["vol_now"]
        vol_prior = slot["vol_prior"]
        if vol_now > 0 and vol_prior > 0:
            vol_ratio = vol_now / vol_prior
            vol_change_log2 = math.log2(vol_ratio)
        else:
            vol_ratio = None
            vol_change_log2 = None
        temps = sorted(slot["temps"])
        mode_label = None
        best = -1
        for label in MODE_PRIORITY:
            count = slot["label_counts"].get(label, 0)
            if count > best:
                best = count
                mode_label = label if count > 0 else None
        countries.append({
            "country": country,
            "vol_now": vol_now,
            "vol_prior": vol_prior,
            "vol_ratio": vol_ratio,
            "vol_change_log2": vol_change_log2,
            "log10_vol": math.log10(vol_now) if vol_now > 0 else None,
            "temp_p75": _percentile(temps, 75) if temps else None,
            "n_temp": len(temps),
            "mode_label": mode_label,
            "n_sufficient": sum(slot["label_counts"].values()),
            "n_dyads": slot["n_dyads"],
            "n_flagged": slot["n_flagged"],
            "share_flagged": (slot["n_flagged"] / slot["n_dyads"]
                              if slot["n_dyads"] else None),
            "pill_max": slot["pill_max"],
        })

    features = _load_geometry(mirror_conn)

    return {"status": "ok", "as_of": effective, "prior_as_of": prior_as_of,
            "countries": countries,
            "geometry": {"type": "FeatureCollection", "features": features}}
