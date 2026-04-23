# layers/goal_management.py
#
# MAPE-K Loop 1 — DAY-AHEAD (slow loop, runs once per day at 00:00)
# ─────────────────────────────────────────────────────────────────
#  Monitor  → read day-ahead forecast (price, CI, grid availability, DC load)
#  Analyze  → select operation mode, compute SOC target, detect risk hours
#  Plan     → run PuLP LP to produce optimal 24h charge/discharge schedule
#  Execute  → push Goals + SchedulePlan down to Change Management Layer
#
# Knowledge store: forecast data, active goals, LP solution
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
import pulp
from dots_infrastructure.Logger import LOGGER


@dataclass
class SystemConfig:
    dt: float = 1.0
    E_BAT: float = 1000.0
    P_CH_MAX: float = 200.0
    P_DCH_MAX: float = 200.0
    SOC_MIN: float = 10.0
    SOC_MAX: float = 90.0
    EFF_CH: float = 0.95
    EFF_DCH: float = 0.95
    P_GRID_MAX: float = 200.0

# ── Shared types ──────────────────────────────────────────────────────────────

class OperationMode(Enum):
    CARBON_MINIMISE = "carbon"
    NONFIRM         = "nonfirm"


@dataclass
class Goals:
    mode:           OperationMode
    SOC_target_end: float           # Desired SOC at end of horizon [%]
    CI_grid:        pd.Series       # 24h carbon intensity [gCO2/kWh]
    grid_available: pd.Series       # 24h grid availability [bool]
    p_DC:           pd.Series       # 24h DC load forecast  [kW]


@dataclass
class SchedulePlan:
    p_ch_b:   pd.Series     # Planned charge power [kW]  (+ charge / − discharge)
    SOC_plan: pd.Series     # Expected SOC trajectory [%]
    source:   str = "lp"    # "lp" | "mpc" — who produced this plan


# ── MAPE-K Loop 1 ─────────────────────────────────────────────────────────────

class GoalManagementLayer:
    """
    Slow MAPE-K loop.
    Runs every morning to produce a day-ahead Goals + SchedulePlan via PuLP LP.

    Scenarios
    ---------
    cost    → minimise total electricity cost
    carbon  → minimise total Scope 2 CO2 emissions
    nonfirm → minimise unserved load during grid outages (feasibility first)
    """

    def __init__(self, scenario: str = "carbon"):
        assert scenario in ["carbon", "nonfirm"], f"Unknown scenario: {scenario}"
        self.scenario  = scenario
        self.goals:    Optional[Goals]        = None
        self.current_schedule:     Optional[SchedulePlan] = None
        # ── Knowledge store ──
        self._knowledge: dict = {}

    # ── Monitor ───────────────────────────────────────────────────────────────

    def monitor(self, forecast: dict) -> dict:
        """Ingest day-ahead forecast data into knowledge store."""
        self._knowledge["forecast"] = forecast
        n_outages = int((~forecast["grid_available"]).sum())
        LOGGER.info(f"[GoalMgmt | Monitor]  forecast loaded — "
                    f"outages={n_outages}h  "
                    f"avg_CI={forecast['CI_grid'].mean():.0f} gCO2/kWh")
        return forecast

    # ── Analyze ───────────────────────────────────────────────────────────────

    def analyze(self, forecast: dict, sys_config: SystemConfig) -> Goals:
        """Select operation mode and compute SOC target."""
        CI_grid        = forecast["CI_grid"]
        grid_available = forecast["grid_available"]
        p_DC           = forecast["p_DC"]

        mode_map = {
            "carbon":  OperationMode.CARBON_MINIMISE,
            "nonfirm": OperationMode.NONFIRM,
        }
        mode = mode_map[self.scenario]

        if mode == OperationMode.NONFIRM:
            outage_hours  = int((~grid_available).sum())
            energy_needed = p_DC.mean() * outage_hours          # kWh
            
            if sys_config.E_BAT > 0:
                soc_target = min(
                    sys_config.SOC_MAX,
                    sys_config.SOC_MIN + (energy_needed / sys_config.E_BAT) * 100.0
                )
            else:
                soc_target = 0.0
        else:
            soc_target = 50.0

        goals = Goals(
            mode=mode,
            SOC_target_end=soc_target,
            CI_grid=CI_grid,
            grid_available=grid_available,
            p_DC=p_DC,
        )
        self._knowledge["goals"] = goals
        print(f"[GoalMgmt | Analyze] mode={mode.value}  SOC_target={soc_target:.1f}%")
        return goals

    # ── Plan (PuLP LP) ────────────────────────────────────────────────────────

    def plan(self, goals: Goals, sys_config: SystemConfig, soc_init: float = 50.0) -> SchedulePlan:
        """
        PuLP LP — day-ahead charge/discharge schedule.
        """
        T   = goals.CI_grid.index
        n   = len(T)
        dt  = sys_config.dt

        prob = pulp.LpProblem("BESS_DayAhead", pulp.LpMinimize)

        # ── Decision variables ────────────────────────────────────────────────
        p_ch    = [pulp.LpVariable(f"p_ch_{t}",    lowBound=0, upBound=sys_config.P_CH_MAX)  for t in range(n)]
        p_dch   = [pulp.LpVariable(f"p_dch_{t}",   lowBound=0, upBound=sys_config.P_DCH_MAX) for t in range(n)]
        soc     = [pulp.LpVariable(f"soc_{t}",     lowBound=sys_config.SOC_MIN, upBound=sys_config.SOC_MAX) for t in range(n)]
        unserved= [pulp.LpVariable(f"unserved_{t}", lowBound=0) for t in range(n)]

        if goals.mode == OperationMode.CARBON_MINIMISE:
            # Minimise total Scope 2 CO2
            prob += pulp.lpSum(
                goals.CI_grid.iloc[t] * (goals.p_DC.iloc[t] + p_ch[t] - p_dch[t]) * dt
                for t in range(n)
            )

        elif goals.mode == OperationMode.NONFIRM:
            # Minimise unserved DC load during outages
            # Secondary: minimise carbon when grid is available
            w_unserved = 1e6   # heavy penalty for unserved load
            w_carbon   = 1.0
            prob += (
                w_unserved * pulp.lpSum(unserved[t] for t in range(n))
                + w_carbon   * pulp.lpSum(
                    # Grid draw: p_DC + p_ch - p_dch
                    goals.CI_grid.iloc[t] * (goals.p_DC.iloc[t] + p_ch[t] - p_dch[t]) * dt
                    for t in range(n) if goals.grid_available.iloc[t]
                )
            )

        # ── Constraints ───────────────────────────────────────────────────────
        for t in range(n):
            avail  = bool(goals.grid_available.iloc[t])
            p_dc_t = goals.p_DC.iloc[t]

            # SOC dynamics
            if t == 0:
                soc_prev = soc_init
            else:
                soc_prev = soc[t - 1]

            if sys_config.E_BAT > 0.0:
                prob += soc[t] == soc_prev + (
                    (sys_config.EFF_CH  * p_ch[t]  * dt / sys_config.E_BAT) * 100.0
                  - (p_dch[t] * dt / (sys_config.EFF_DCH * sys_config.E_BAT)) * 100.0
                )
            else:
                prob += soc[t] == 0.0
                prob += p_ch[t] == 0.0
                prob += p_dch[t] == 0.0

            # Extra SOC floor in NONFIRM mode: keep SOC high whenever grid is available
            if goals.mode == OperationMode.NONFIRM and avail and sys_config.E_BAT > 0.0:
                prob += soc[t] >= 70.0

            # Grid availability
            if avail:
                # Grid draw must be non-negative and within limit
                prob += p_dc_t + p_ch[t] - p_dch[t] >= 0
                prob += p_dc_t + p_ch[t] - p_dch[t] <= sys_config.P_GRID_MAX
                prob += unserved[t] == 0
            else:
                # No grid — battery must cover DC load
                prob += p_ch[t]  == 0
                prob += p_dch[t] + unserved[t] >= p_dc_t
                prob += p_dch[t] <= p_dc_t      # don't discharge more than needed

        # End-of-day SOC target (soft: within ±10%)
        prob += soc[n - 1] >= goals.SOC_target_end - 10.0
        prob += soc[n - 1] <= goals.SOC_target_end + 10.0

        # ── Solve ─────────────────────────────────────────────────────────────
        # Use a safe CMD solver for background execution to prevent temporary file clashes and blocking.
        solver = pulp.PULP_CBC_CMD(msg=0, threads=1, keepFiles=False)
        prob.solve(solver)
        status = pulp.LpStatus[prob.status]
        print(f"[GoalMgmt | Plan]    LP status={status}  "
              f"objective={pulp.value(prob.objective):.2f}")

        # ── Extract solution ──────────────────────────────────────────────────
        p_net  = pd.Series(
            [pulp.value(p_ch[t]) - pulp.value(p_dch[t]) for t in range(n)],
            index=T, name="p_ch_b"
        )
        soc_s  = pd.Series(
            [pulp.value(soc[t]) for t in range(n)],
            index=T, name="SOC_plan"
        )

        schedule = SchedulePlan(p_ch_b=p_net, SOC_plan=soc_s, source="lp")
        self._knowledge["plan"] = schedule
        self.current_schedule  = schedule
        self.goals = goals
        return schedule

    # ── Execute ───────────────────────────────────────────────────────────────

    def execute(self, forecast: dict, sys_config: SystemConfig, soc_init: float = 50.0) -> tuple[Goals, SchedulePlan]:
        """
        Full MAPE-K cycle for one day.
        Returns (Goals, SchedulePlan) for Change Management Layer.
        """
        forecast = self.monitor(forecast)
        goals    = self.analyze(forecast, sys_config)
        plan     = self.plan(goals, sys_config, soc_init=soc_init)
        return goals, plan
