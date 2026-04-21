# ══════════════════════════════════════════════════════════════════════════════
# simulation/orchestrator.py
#
# Wires the two MAPE-K loops and Component Control together.
# Runs a full-year simulation day by day, hour by hour.
# ══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations
import pandas as pd
import numpy as np

import config
from data.data_generator import generate_day_ahead_forecast, generate_full_year
from layers.goal_management  import GoalManagementLayer
from layers.change_management import ChangeManagementLayer
from layers.component_control import ComponentControlLayer, ComponentState


class BESSOrchestrator:
    """
    Full simulation loop.

    Per day (00:00):
        MAPE-K Loop 1 (GoalManagement):
            monitor forecast → analyze → PuLP LP → push plan to ChangeMgmt

    Per hour:
        MAPE-K Loop 2 (ChangeManagement):
            monitor actual SOC/grid → analyze deviation → MPC replan if needed
            → setpoint to ComponentControl

        ComponentControl:
            execute setpoint → enforce safety → return ComponentState
    """

    def __init__(self, scenario: str = "cost"):
        self.goal_layer    = GoalManagementLayer(scenario)
        self.change_layer  = ChangeManagementLayer()
        self.control_layer = ComponentControlLayer()

    def run_year(self, year_data: pd.DataFrame) -> pd.DataFrame:
        """
        Simulate a full year hour by hour.

        Parameters
        ----------
        year_data : DataFrame with columns:
            price_E, CI_grid, grid_available, p_DC
            indexed by hourly DatetimeIndex

        Returns
        -------
        results : DataFrame with per-hour simulation output
        """
        records = []
        days    = pd.date_range(
            start=year_data.index[0].normalize(),
            end=year_data.index[-1].normalize(),
            freq="D"
        )

        soc_carry = config.SOC_INIT   # carry SOC across days

        for day in days:
            # ── MAPE-K Loop 1: day-ahead LP (once per day) ──────────────────
            forecast = {
                "price_E":        year_data.loc[str(day.date()), "price_E"],
                "CI_grid":        year_data.loc[str(day.date()), "CI_grid"],
                "grid_available": year_data.loc[str(day.date()), "grid_available"],
                "p_DC":           year_data.loc[str(day.date()), "p_DC"],
            }

            # Skip days with missing data
            if len(forecast["price_E"]) == 0:
                continue

            goals, lp_plan = self.goal_layer.execute(forecast, soc_init=soc_carry)
            self.change_layer.load_day_ahead_plan(goals, lp_plan)

            # ── Hourly loop ──────────────────────────────────────────────────
            hours = forecast["price_E"].index
            for h, t in enumerate(hours):
                actual_grid = bool(year_data.loc[t, "grid_available"])
                actual_p_DC = float(year_data.loc[t, "p_DC"])
                price_E     = float(year_data.loc[t, "price_E"])
                CI_grid     = float(year_data.loc[t, "CI_grid"])

                # MAPE-K Loop 2: intra-day MPC
                setpoint, _ = self.change_layer.execute(
                    hour=h,
                    soc_actual=self.control_layer.SOC,
                    grid_actual=actual_grid,
                )

                # Component Control: execute + safety
                state: ComponentState = self.control_layer.execute_step(
                    setpoint=setpoint,
                    grid_avail=actual_grid,
                    p_DC=actual_p_DC,
                    price_E=price_E,
                    CI_grid=CI_grid,
                )

                records.append({
                    "timestamp":    t,
                    "SOC":          state.SOC,
                    "p_ch_b":       state.p_ch_b,
                    "p_grid":       state.p_grid,
                    "p_DC":         state.p_DC,
                    "unserved_kw":  state.unserved,
                    "cost_eur":     state.cost,
                    "carbon_gco2":  state.carbon,
                    "grid_avail":   actual_grid,
                    "alarm":        state.alarm,
                    "alarm_msg":    state.alarm_msg,
                    "price_E":      price_E,
                    "CI_grid":      CI_grid,
                })

            soc_carry = self.control_layer.SOC   # carry SOC to next day

        results = pd.DataFrame(records).set_index("timestamp")
        print(f"\n[Orchestrator] Simulation complete — {len(results)} hours")
        print(f"  Total cost:       {results['cost_eur'].sum():.2f} €")
        print(f"  Total CO2:        {results['carbon_gco2'].sum()/1e6:.2f} tCO2")
        print(f"  Unserved energy:  {results['unserved_kw'].sum():.1f} kWh")
        print(f"  Grid outage hours:{int((~results['grid_avail']).sum())}")
        return results