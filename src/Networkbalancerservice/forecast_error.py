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
# All four tunable parameters of this model — the three relative-σ values and the
# RNG seed — are exposed as ESDL DoubleKPI attributes on the ElectricityNetwork
# asset (forecast_sigma_ci, forecast_sigma_p_dc, forecast_sigma_price,
# forecast_seed) and read by the Network Balancer at federate initialisation.
# This lets each experimental run select its own forecast-uncertainty profile
# without rebuilding any service container. The literature-calibrated defaults
# below act only as a fallback when the model is instantiated outside the ESDL
# path (e.g., the standalone three_layer_mape harness).
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import numpy as np
import pandas as pd

from dots_infrastructure.Logger import LOGGER


# ── Fallback relative standard deviations (literature-calibrated) ────────────
# These are used only when the constructor is called with no explicit sigmas.
# Production runs receive sigmas from the ESDL via the Network Balancer.

_FALLBACK_SIGMA = {
    "CI_grid":  0.12,   # 12 % — ENTSO-E / Staffell & Pfenninger (2016)
    "p_DC":     0.05,   # 5 %  — Pelley et al. (2009)
    "price_E":  0.15,   # 15 % — Weron (2014) day-ahead EPEX SPOT NL
}

# Physical / market lower bounds — perturbed forecasts must stay above these.
# These are not ESDL-configurable because they encode genuine physical or
# market-rule constraints rather than modelling assumptions.
_LOWER_BOUND = {
    "CI_grid":  10.0,   # gCO2/kWh — practically zero-carbon floor
    "p_DC":     0.0,    # kW      — demand cannot be negative
    "price_E":  -500.0, # EUR/MWh — EPEX SPOT NL day-ahead technical floor
}


class ForecastErrorModel:
    """
    Applies calibrated additive Gaussian forecast error to day-ahead signals.

    The model implements:
        forecast[t] = actual[t] * (1 + ε[t])
        ε[t] ~ N(0, σ_rel²)   i.i.d. per timestep and per signal

    where σ_rel is the relative standard deviation supplied by the caller
    (typically the Network Balancer reading the ESDL).

    Parameters
    ----------
    seed : int, optional
        RNG seed for reproducibility. Defaults to 42 when not supplied.
    sigma_ci, sigma_p_dc, sigma_price : float, optional
        Relative standard deviations per signal. Each defaults to its
        literature-calibrated fallback value (see _FALLBACK_SIGMA) when not
        supplied; production callers should pass the ESDL-driven values.
    """

    def __init__(
        self,
        seed: int = 42,
        sigma_ci:    float | None = None,
        sigma_p_dc:  float | None = None,
        sigma_price: float | None = None,
    ):
        self._rng = np.random.default_rng(int(seed))
        self._sigma = {
            "CI_grid": _FALLBACK_SIGMA["CI_grid"] if sigma_ci    is None else float(sigma_ci),
            "p_DC":    _FALLBACK_SIGMA["p_DC"]    if sigma_p_dc  is None else float(sigma_p_dc),
            "price_E": _FALLBACK_SIGMA["price_E"] if sigma_price is None else float(sigma_price),
        }
        LOGGER.info(
            "[ForecastError] Initialised — seed=%d  σ(CI)=%.1f%%  σ(p_DC)=%.1f%%  σ(price)=%.1f%%",
            int(seed),
            self._sigma["CI_grid"] * 100,
            self._sigma["p_DC"] * 100,
            self._sigma["price_E"] * 100,
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
