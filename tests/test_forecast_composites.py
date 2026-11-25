"""Composite forecast models (Dan, 2026-11-25): POOL (equal-
weight F1+F2 pool) in both engines; ANCH05 (declared λ=0.5)
and ANCHBT (backtest-estimated λ) in the R&D engine.

The identities asserted here are grouping-invariance of the
mixture rule: a two-stage mixture (pool per origin, then mix
across origins) has the same mean and total variance as the
flat mixture, so a composite leaf's ilr_mean / ilr_var are
exact functions of its components' leaves.
"""
import numpy as np

from app import hybrid_forecast, poc_pipeline


def _series(t=520, seed=7):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.05, size=(t, 4))
    steps[:, 0] += 0.002  # a gentle drift for F1 to find
    Z = np.cumsum(steps, axis=0)
    w = np.full(t, 10.0)
    n_eff = np.full(t, 50.0)
    return Z, w, n_eff


def test_poc_pool_and_anchored_identities():
    Z, w, n_eff = _series()
    horizons = [45, 60]
    out = poc_pipeline.forecast_from_arrays(
        Z, w, n_eff, horizons,
        ["F0m", "F1", "F2", "POOL", "ANCH05", "ANCHBT"],
        origins=[0])
    for h in horizons:
        f1 = np.array(out["F1"][h]["ilr_mean"])
        f2 = np.array(out["F2"][h]["ilr_mean"])
        pool = np.array(out["POOL"][h]["ilr_mean"])
        assert np.allclose(pool, (f1 + f2) / 2, atol=1e-10)
        v1 = np.array(out["F1"][h]["ilr_var"])
        v2 = np.array(out["F2"][h]["ilr_var"])
        pvar = np.array(out["POOL"][h]["ilr_var"])
        assert np.allclose(
            pvar, (v1 + v2) / 2 + (f1 - f2) ** 2 / 4, atol=1e-10)
        anch = np.array(out["ANCH05"][h]["ilr_mean"])
        f0m = np.array(out["F0m"][h]["ilr_mean"])
        assert np.allclose(anch, (f0m + pool) / 2, atol=1e-10)
        assert out["ANCH05"][h]["lambda"] == 0.5
        assert out["ANCH05"][h]["lambda_estimated"] is False
        bt = out["ANCHBT"][h]
        assert 0.0 <= bt["lambda"] <= 1.0
        assert bt["lambda_origins"] >= 3
        assert bt["lambda_estimated"] is True
        for m in ("POOL", "ANCH05", "ANCHBT"):
            path = np.array(out[m][h]["path"])
            assert abs(path.sum() - 1.0) < 1e-9
            assert (path > 0).all()


def test_poc_anchbt_lambda_deterministic():
    Z, w, n_eff = _series()
    a = poc_pipeline.forecast_from_arrays(
        Z, w, n_eff, [60], ["ANCHBT"], origins=[0])
    b = poc_pipeline.forecast_from_arrays(
        Z, w, n_eff, [60], ["ANCHBT"], origins=[0])
    assert a["ANCHBT"][60]["lambda"] == b["ANCHBT"][60]["lambda"]


def test_workflow_pool_leaf():
    Z, w, n_eff = _series()
    kw = dict(horizon=60, origins=[0, 7, 15, 30, 45])
    f1 = hybrid_forecast.forecast_multi_origin("F1", Z, w, n_eff, **kw)
    f2 = hybrid_forecast.forecast_multi_origin("F2", Z, w, n_eff, **kw)
    pool = hybrid_forecast.forecast_multi_origin("POOL", Z, w, n_eff, **kw)
    mu_pool = hybrid_forecast.ilr(np.array(pool["path"]))
    mu_avg = (hybrid_forecast.ilr(np.array(f1["path"]))
              + hybrid_forecast.ilr(np.array(f2["path"]))) / 2
    assert np.allclose(mu_pool, mu_avg, atol=1e-9)
    assert abs(sum(pool["path"]) - 1.0) < 1e-9
    assert "regime_now" not in pool
    assert pool["origin_points"]
    assert "POOL" in hybrid_forecast.HYBRID_MODELS
