# data/data_generator.py
# Simulates day-ahead electricity price, carbon intensity,
# grid availability (non-firm ATO), and data center load.

import numpy as np
import pandas as pd
import config


def generate_timeseries(year: int = config.YEAR, freq: str = config.FREQ) -> pd.DatetimeIndex:
    """Full-year hourly timeseries."""
    #return pd.date_range(start=f"{year}-01-01", end=f"{year}-12-31 23:00", freq=freq)
    start = f"{year}-01-01"
    T = pd.date_range(start=start, periods=config.N_HOURS, freq=freq)
    return T

def simulate_price(T: pd.DatetimeIndex) -> pd.Series:
    """
    Synthetic day-ahead electricity price [€/kWh].
    Adds:
      - daily pattern (cheap at night, expensive morning/evening peak)
      - seasonal pattern (higher in winter)
      - random noise
    """
    n = len(T)
    hour    = T.hour
    month   = T.month

    daily   = 0.04 * np.sin(2 * np.pi * (hour - 14) / 24)   # peak ~14:00
    morning = 0.03 * np.exp(-((hour - 8) ** 2) / 8)          # morning ramp
    seasonal= 0.02 * np.cos(2 * np.pi * (month - 1) / 12)    # winter higher
    noise   = np.random.normal(0, config.PRICE_STD * 0.5, n)

    price = config.PRICE_MEAN + daily + morning + seasonal + noise
    price = np.clip(price, config.PRICE_MIN, config.PRICE_MAX)
    return pd.Series(price, index=T, name="price_E")


def simulate_carbon_intensity(T: pd.DatetimeIndex) -> pd.Series:
    """
    Synthetic grid carbon intensity [gCO2/kWh].
    Adds:
      - daily pattern (cleaner midday due to solar)
      - seasonal pattern (dirtier in winter, less solar)
      - random noise
    """
    n = len(T)
    hour    = T.hour
    month   = T.month

    solar   = -60 * np.exp(-((hour - 13) ** 2) / 10)         # clean midday
    seasonal= 40  * np.cos(2 * np.pi * (month - 1) / 12)     # dirty winter
    noise   = np.random.normal(0, config.CI_STD * 0.4, n)

    ci = config.CI_MEAN + solar + seasonal + noise
    ci = np.clip(ci, config.CI_MIN, config.CI_MAX)
    return pd.Series(ci, index=T, name="CI_grid")


def simulate_grid_availability(T: pd.DatetimeIndex, seed: int = 42) -> pd.Series:
    """
    Non-firm ATO grid availability [bool].
    Grid is available 85% of hours. Outages are clustered
    (not purely random) to mimic realistic curtailment events.
    """
    rng = np.random.default_rng(seed)
    n   = len(T)

    available = np.ones(n, dtype=bool)
    target_outage_hours = int(n * (1 - config.GRID_AVAILABILITY))

    # Generate clustered outage blocks (1–8 hours each)
    outage_hours = 0
    while outage_hours < target_outage_hours:
        start  = rng.integers(0, n)
        length = rng.integers(1, 9)              # 1–8 hour blocks
        end    = min(start + length, n)
        available[start:end] = False
        outage_hours += (end - start)

    # Trim back to exactly target
    actual_outages = np.where(~available)[0]
    if len(actual_outages) > target_outage_hours:
        restore = actual_outages[target_outage_hours:]
        available[restore] = True

    return pd.Series(available, index=T, name="grid_available")


def simulate_dc_load(T: pd.DatetimeIndex) -> pd.Series:
    """
    Data center load [kW].
    Constant base load with small random fluctuation (±2%).
    """
    n    = len(T)
    noise= np.random.normal(0, config.P_DC_BASE * 0.02, n)
    load = np.clip(config.P_DC_BASE + noise, 0, None)
    return pd.Series(load, index=T, name="p_DC")


def generate_day_ahead_forecast(date: pd.Timestamp) -> dict:
    """
    Generate a 24-hour day-ahead forecast for a single day.
    Returns dict of Series indexed by hourly timestamps.
    Used by Goal Management Layer each morning.
    """
    T_day = pd.date_range(start=date, periods=24, freq="h")
    return {
        "price_E":        simulate_price(T_day),
        "CI_grid":        simulate_carbon_intensity(T_day),
        "grid_available": simulate_grid_availability(T_day, seed=int(date.timestamp()) % 10000),
        "p_DC":           simulate_dc_load(T_day),
    }


def generate_full_year() -> pd.DataFrame:
    """
    Generate full-year simulation data as a single DataFrame.
    Used for annual scenario evaluation.
    """
    T = generate_timeseries()
    df = pd.DataFrame({
        "price_E":        simulate_price(T),
        "CI_grid":        simulate_carbon_intensity(T),
        "grid_available": simulate_grid_availability(T),
        "p_DC":           simulate_dc_load(T),
    })
    return df
