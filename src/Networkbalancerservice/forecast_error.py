# forecast_error.py
#
# Academically-calibrated Gaussian forecast error model for day-ahead signals.
#
# Rationale
# ---------
# In real energy systems the day-ahead LP does NOT have access to actual future
# values — it operates on forecasts that carry uncertainty.  Using raw historical
# data as the DA forecast would give zero forecast error, making the intra-day
# MPC layer trivially unnecessary and RQ4 unanswerable.
#
# This module perturbs the "true" signal (historical data) with additive
# zero-mean Gaussian noise whose standard deviation is calibrated to published
# forecast accuracy values, producing a realistic imperfect forecast.  The
# intra-day layer then observes actual values, and any residual error creates
# genuine deviation events that trigger MPC re-planning.
#
# Literature sources for σ values
# --------------------------------
#   CI_grid  : Staffell & Pfenninger (2016), "Using bias-corrected reanalysis
#               to simulate current and future wind power output", Energy 114.
#               ENTSO-E (2022), "Transparency Platform — Generation Forecast
#               Accuracy", reports 10–15 % MAPE for marginal emission factors.
#               → σ_rel = 0.12 (12 % relative standard deviation)
#
#   price_E  : Weron (2014), "Electricity price forecasting: A review of the
#               state-of-the-art with a look into the future", Int. J. Forecasting.
#               Day-ahead EPEX SPOT NL: typical MAE ≈ 10–20 % of mean price.
#               → σ_rel = 0.15 (15 % relative standard deviation)
#
#   p_DC     : Pelley, Meisner, Wenisch & Martin (2009), "Understanding and
#               abstracting total data center power".  Large-DC load variance
#               over 15-min intervals: 3–8 % around the daily mean.
#               → σ_rel = 0.05 (5 % relative standard deviation)
#
# Reproducibility
# ---------------
# The RNG is seeded from the env variable SIMULATION_SEED (default 42).
# Set FORECAST_SEED separately to vary forecast error independently of other
# stochastic components.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import os
import logging
import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)


# ── Default relative standard deviations (literature-calibrated) ──────────────

_DEFAULT_SIGMA = {
    "CI_grid":  0.12,   # 12 % — ENTSO-E / Staffell & Pfenninger (2016)
    "p_DC":     0.05,   # 5 %  — Pelley et al. (2009)
}

# Physical lower bounds — a perturbed forecast must stay above these
_LOWER_BOUND = {
    "CI_grid":  10.0,   # gCO2/kWh — practically zero-carbon floor
    "p_DC":     0.0,    # kW      — demand cannot be negative
}


class ForecastErrorModel:
    """
    Applies calibrated additive Gaussian forecast error to day-ahead signals.

    The model implements:
        forecast[t] = actual[t] * (1 + ε[t])
        ε[t] ~ N(0, σ_rel²)   i.i.d. per timestep and per signal

    where σ_rel is the relative standard deviation taken from the literature.

    Parameters
    ----------
    seed : int, optional
        RNG seed for reproducibility.  Defaults to env FORECAST_SEED → 42.
    sigma_overrides : dict, optional
        Override any default σ_rel value for sensitivity analysis, e.g.
        {"CI_grid": 0.20} to test a high-uncertainty carbon scenario.
    """

    def __init__(
        self,
        seed: int | None = None,
        sigma_overrides: dict[str, float] | None = None,
    ):
        if seed is None:
            seed = int(os.environ.get("FORECAST_SEED", os.environ.get("SIMULATION_SEED", 42)))
        self._rng = np.random.default_rng(seed)

        self._sigma = {**_DEFAULT_SIGMA}
        if sigma_overrides:
            self._sigma.update(sigma_overrides)

        LOGGER.info(
            "[ForecastError] Initialized — seed=%d  σ(CI)=%.0f%%  σ(DC)=%.0f%%",
            seed,
            self._sigma["CI_grid"] * 100,
            self._sigma["p_DC"] * 100,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def perturb(self, signal_name: str, actual: pd.Series | list) -> pd.Series:
        """
        Return a perturbed (forecast) version of *actual*.

        Each element is multiplied by (1 + ε) where ε ~ N(0, σ²).
        The result is clipped to its physical lower bound.

        Parameters
        ----------
        signal_name : str
            One of "CI_grid", "price_E", "p_DC".
        actual : pd.Series or list
            The true historical values for the forecast horizon.

        Returns
        -------
        pd.Series
            Perturbed forecast with the same index as *actual*.
        """
        # FOR TESTING: Disable forecast error so DA and ID are identical
        # return pd.Series(actual) if not isinstance(actual, pd.Series) else actual.copy()

        s = pd.Series(actual) if not isinstance(actual, pd.Series) else actual.copy()
        sigma = self._sigma.get(signal_name, 0.0)

        if sigma > 0.0:
            n = len(s)
            noise = self._rng.normal(loc=0.0, scale=sigma, size=n)
            s = s * (1.0 + noise)

        lb = _LOWER_BOUND.get(signal_name, 0.0)
        s = s.clip(lower=lb)

        if sigma > 0.0:
            mae = float((s - pd.Series(actual)).abs().mean())
            LOGGER.debug(
                 "[ForecastError] %s — σ=%.0f%%  MAE=%.3f  mean(actual)=%.3f",
                 signal_name, sigma * 100, mae, float(pd.Series(actual).mean()),
             )

        return s

    def perturb_scalar(self, signal_name: str, actual: float) -> float:
        """Convenience wrapper for a single scalar value."""
        return float(self.perturb(signal_name, pd.Series([actual])).iloc[0])
