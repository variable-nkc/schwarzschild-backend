"""Reference anchors for the R&D forecast section (Dan,
2026-11-25): the population references measured by the
simplex-hull diagnostic (goal workspace
latent-dyad-state-modeling-methods-review,
files/simplex-hull-diagnostic-2026-11-25/), served live so
the R&D spectrum can draw a forecast against a chosen
reference and read its distance from that reference against
the reference's own spread.

Definitions are the diagnostic's, exactly — this module is
verified against its published numbers on the same data:

- Crosswalk: literal zeros replaced with 5e-3 and rows
  rescaled to sum 1 (the diagnostic's rule; the PoC series
  engine's multiplicative replacement differs in the third
  decimal of replaced cells — the anchors follow the
  diagnostic, whose numbers these are).
- A dyad's composition ("trait") is the obs-weighted
  Aitchison mean of its codes: clr = (code shares) @ clr(S).
  Unmapped codes are excluded from emission, as everywhere.
- Sufficient stratum: rollup label != 'insufficient' on
  >= 10 as-ofs (a frozen span-level classification).
- population_mix ("typical report"): event-weighted pooled
  composition over sufficient dyads' records. Its distance
  yardstick is simulated: days of `volume_used` reports
  drawn iid from that mix.
- typical_dyad: the dyad-equal centre of sufficient dyads'
  traits. Its yardstick is empirical: trait distances of
  solid sufficient dyads (>= 300 reports — the diagnostic's
  reporting set; thinner traits carry estimation noise that
  would inflate the spread).
- random_dyad_day (the 2-level reference): same centre as
  typical_dyad; its yardstick is simulated: draw a
  sufficient dyad uniformly, then a day of `volume_used`
  reports from that dyad's own mix.
- own_trait: the focal dyad's own long-run composition
  (all dyads, sufficient or not). Its yardstick is
  empirical: the dyad's own daily compositions' distances
  to its trait (needs >= 10 active days).

`volume_used` is the focal dyad's median mapped daily
volume over its trailing 90 days of activity — the
volume-conditional references are meaningless without a
stated volume, so the payload carries the one used.

Distances are Aitchison (Euclidean in clr; identical to
the ILR norm). Distance quantiles are returned on a fixed
probability grid; the caller interpolates a percentile.

These are descriptive references, not adopted methodology:
they say how atypical a composition is against a chosen
population, never how uncertain a forecast is. Population
structures are cached per (dda, rollup, simplexes) file
identity; the volume-dependent simulations re-run per
request with a fixed seed, so repeat calls agree.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

from . import dev_dda_store, poc_pipeline

ZERO_REPL = 5e-3
SUFF_MIN_ASOFS = 10
SOLID_MIN_REPORTS = 300
MIN_OWN_DAYS = 10
N_DRAWS = 2000
SEED = 20251125
QPROBS = [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]

_CACHE: dict = {}


def _clr(x: np.ndarray) -> np.ndarray:
    lx = np.log(x)
    return lx - lx.mean(axis=-1, keepdims=True)


def _closure_from_clr(c: np.ndarray) -> np.ndarray:
    e = np.exp(c - c.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def _crosswalk_arrays(crosswalk: dict) -> tuple[list, np.ndarray]:
    """(codes, S_clr) with the diagnostic's zero handling."""
    codes = sorted(crosswalk)
    S = np.array([crosswalk[c] for c in codes], dtype=float)
    if (S == 0).any():
        S = np.where(S == 0, ZERO_REPL, S)
        S = S / S.sum(axis=1, keepdims=True)
    return codes, _clr(S)


def _quantiles(values: np.ndarray) -> dict:
    q = np.quantile(values, QPROBS)
    return {"probs": QPROBS, "values": [float(v) for v in q]}


def _population(dda_path: Path, rollup_path: Path,
                simplexes_path: Path) -> dict:
    key = (str(dda_path), dda_path.stat().st_mtime_ns,
           str(rollup_path), rollup_path.stat().st_mtime_ns,
           str(simplexes_path), simplexes_path.stat().st_mtime_ns)
    if key in _CACHE:
        return _CACHE[key]

    simplexes = dev_dda_store.load_simplexes(simplexes_path)
    crosswalk = poc_pipeline.crosswalk_from_simplexes(simplexes)
    codes, S_clr = _crosswalk_arrays(crosswalk)
    code_idx = {c: i for i, c in enumerate(codes)}

    rcon = sqlite3.connect(f"file:{rollup_path}?mode=ro", uri=True)
    try:
        suff = {
            f"{r[0]}_{r[1]}"
            for r in rcon.execute(
                "SELECT iso1, iso2 FROM dyad_status_rollup "
                "WHERE label != 'insufficient' "
                "GROUP BY iso1, iso2 HAVING COUNT(*) >= ?",
                (SUFF_MIN_ASOFS,))
        }
    finally:
        rcon.close()

    dcon = sqlite3.connect(f"file:{dda_path}?mode=ro", uri=True)
    try:
        first, last = dcon.execute(
            "SELECT MIN(obs_date), MAX(obs_date) FROM dda").fetchone()
        rows = dcon.execute(
            "SELECT iso1, iso2, cameo_code, SUM(obs_count) "
            "FROM dda GROUP BY iso1, iso2, cameo_code").fetchall()
    finally:
        dcon.close()

    dyad_names: list[str] = []
    dyad_index: dict[str, int] = {}
    di = np.empty(len(rows), dtype=np.int64)
    ci = np.empty(len(rows), dtype=np.int64)
    ct = np.empty(len(rows), dtype=float)
    n_used = 0
    for iso1, iso2, code, total in rows:
        j = code_idx.get(str(code))
        if j is None:
            continue
        name = f"{iso1}_{iso2}"
        k = dyad_index.get(name)
        if k is None:
            k = len(dyad_names)
            dyad_index[name] = k
            dyad_names.append(name)
        di[n_used] = k
        ci[n_used] = j
        ct[n_used] = float(total)
        n_used += 1
    di, ci, ct = di[:n_used], ci[:n_used], ct[:n_used]
    M = np.zeros((len(dyad_names), len(codes)))
    np.add.at(M, (di, ci), ct)
    N = M.sum(axis=1)
    Wn = M / N[:, None]
    mu_clr = Wn @ S_clr

    is_suff = np.array([nm in suff for nm in dyad_names])
    suff_idx = np.where(is_suff)[0]
    # event-weighted pooled mix over sufficient dyads' records
    w_suff = M[suff_idx].sum(axis=0)
    w_suff = w_suff / w_suff.sum()
    b_clr = w_suff @ S_clr
    centre_clr = mu_clr[suff_idx].mean(axis=0)
    solid = suff_idx[N[suff_idx] >= SOLID_MIN_REPORTS]
    trait_dist = np.linalg.norm(mu_clr[solid] - centre_clr, axis=1)

    pop = {
        "codes": codes, "code_idx": code_idx, "S_clr": S_clr,
        "dyad_index": dyad_index, "mu_clr": mu_clr, "N": N,
        "is_suff": is_suff, "suff_idx": suff_idx,
        "Wn_suff": Wn[suff_idx], "w_suff": w_suff,
        "b_clr": b_clr, "centre_clr": centre_clr,
        "trait_quantiles": _quantiles(trait_dist),
        "n_suff": int(len(suff_idx)), "n_solid": int(len(solid)),
        "span": {"first": first, "last": last},
    }
    _CACHE.clear()
    _CACHE[key] = pop
    return pop


def _own_days(dda_path: Path, iso1: str, iso2: str,
              pop: dict) -> tuple[np.ndarray, np.ndarray]:
    """(day_clr, day_volumes) for the focal dyad's active
    days (mapped records only), ordered by date; also the
    dates, for the trailing-volume window."""
    dcon = sqlite3.connect(f"file:{dda_path}?mode=ro", uri=True)
    try:
        rows = dcon.execute(
            "SELECT obs_date, cameo_code, obs_count FROM dda "
            "WHERE iso1 = ? AND iso2 = ? ORDER BY obs_date",
            (iso1, iso2)).fetchall()
    finally:
        dcon.close()
    code_idx, S_clr = pop["code_idx"], pop["S_clr"]
    by_date: dict[str, np.ndarray] = {}
    vol: dict[str, float] = {}
    for obs_date, code, count in rows:
        j = code_idx.get(str(code))
        if j is None:
            continue
        acc = by_date.get(obs_date)
        if acc is None:
            acc = np.zeros(5)
            by_date[obs_date] = acc
            vol[obs_date] = 0.0
        acc += float(count) * S_clr[j]
        vol[obs_date] += float(count)
    dates = sorted(by_date)
    if not dates:
        return dates, np.zeros((0, 5)), np.zeros(0)
    day_clr = np.array([by_date[d] / vol[d] for d in dates])
    day_vol = np.array([vol[d] for d in dates])
    return dates, day_clr, day_vol


def build_payload(dda_path: Path, rollup_path: Path,
                  simplexes_path: Path, iso1: str,
                  iso2: str) -> dict:
    pop = _population(dda_path, rollup_path, simplexes_path)
    dyad = f"{iso1}_{iso2}"
    dates, day_clr, day_vol = _own_days(dda_path, iso1, iso2, pop)

    volume_used = None
    if len(dates):
        last_ord = int(np.datetime64(dates[-1], "D").astype(int))
        ords = np.array(
            [int(np.datetime64(d, "D").astype(int)) for d in dates])
        trailing = day_vol[ords >= last_ord - 89]
        if len(trailing):
            volume_used = max(1, int(round(float(
                np.median(trailing)))))

    rng = np.random.default_rng(SEED)
    refs: dict = {}

    pop_ref: dict = {"shares": [float(v) for v in
                                _closure_from_clr(pop["b_clr"])]}
    if volume_used is not None:
        counts = rng.multinomial(volume_used, pop["w_suff"],
                                 size=N_DRAWS)
        day_clr_sim = (counts / volume_used) @ pop["S_clr"]
        dist = np.linalg.norm(day_clr_sim - pop["b_clr"], axis=1)
        pop_ref["distance_quantiles"] = _quantiles(dist)
    else:
        pop_ref["distance_quantiles"] = None
    pop_ref["basis"] = (
        "simulated days at the stated volume, drawn iid from "
        "the pooled sufficient-stratum mix")
    refs["population_mix"] = pop_ref

    refs["typical_dyad"] = {
        "shares": [float(v) for v in
                   _closure_from_clr(pop["centre_clr"])],
        "distance_quantiles": pop["trait_quantiles"],
        "basis": ("empirical trait distances of solid "
                  f"sufficient dyads (>= {SOLID_MIN_REPORTS} "
                  "reports) from the trait centre"),
    }

    rd_ref: dict = {"shares": refs["typical_dyad"]["shares"]}
    if volume_used is not None:
        Wn = pop["Wn_suff"]
        pick = rng.integers(0, len(Wn), size=N_DRAWS)
        counts = np.array([rng.multinomial(volume_used, Wn[i])
                           for i in pick])
        day_clr_sim = (counts / volume_used) @ pop["S_clr"]
        dist = np.linalg.norm(day_clr_sim - pop["centre_clr"],
                              axis=1)
        rd_ref["distance_quantiles"] = _quantiles(dist)
    else:
        rd_ref["distance_quantiles"] = None
    rd_ref["basis"] = (
        "simulated: a sufficient dyad drawn uniformly, then "
        "a day at the stated volume from its own mix; "
        "distances to the trait centre")
    refs["random_dyad_day"] = rd_ref

    k = pop["dyad_index"].get(dyad)
    if k is None:
        refs["own_trait"] = {
            "shares": None, "distance_quantiles": None,
            "n_days": 0, "n_reports": 0,
            "basis": "dyad has no mapped records in the DDA",
        }
    else:
        own_clr = pop["mu_clr"][k]
        own: dict = {
            "shares": [float(v) for v in
                       _closure_from_clr(own_clr)],
            "n_days": int(len(dates)),
            "n_reports": int(pop["N"][k]),
            "basis": ("this dyad's own daily compositions vs "
                      "its long-run composition"),
        }
        if len(dates) >= MIN_OWN_DAYS:
            dist = np.linalg.norm(day_clr - own_clr, axis=1)
            own["distance_quantiles"] = _quantiles(dist)
        else:
            own["distance_quantiles"] = None
        refs["own_trait"] = own

    return {
        "dyad": dyad,
        "span": pop["span"],
        "stratum": {
            "definition": ("rollup label != 'insufficient' on "
                           f">= {SUFF_MIN_ASOFS} as-ofs"),
            "n_dyads": pop["n_suff"],
            "n_solid": pop["n_solid"],
        },
        "volume_used": volume_used,
        "references": refs,
    }
