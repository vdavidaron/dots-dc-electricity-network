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
    p_PV:           Optional[pd.Series] = None # 24h PV forecast [kW]


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
        p_PV           = forecast.get("p_PV", pd.Series([0.0]*len(p_DC), index=p_DC.index))

        mode_map = {
            "carbon":  OperationMode.CARBON_MINIMISE,
            "nonfirm": OperationMode.NONFIRM,
        }
        mode = mode_map[self.scenario]

        if mode == OperationMode.NONFIRM:
            outage_hours  = int((~grid_available).sum())
            energy_needed = max(0, (p_DC - p_PV).mean()) * outage_hours          # kWh
            
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
            p_PV=p_PV,
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
        p_grid  = [pulp.LpVariable(f"p_grid_{t}",  lowBound=0, upBound=sys_config.P_GRID_MAX) for t in range(n)]
        soc     = [pulp.LpVariable(f"soc_{t}",     lowBound=sys_config.SOC_MIN, upBound=sys_config.SOC_MAX) for t in range(n)]
        unserved= [pulp.LpVariable(f"unserved_{t}", lowBound=0) for t in range(n)]
        soc_slack = [pulp.LpVariable(f"soc_slack_{t}", lowBound=0) for t in range(n)]

        # ── Objective ─────────────────────────────────────────────────────────
        # Priority 1: Minimise unserved load (Reliability)
        # Priority 2: Keep SOC at target (Recharge battery when possible)
        # Priority 3: Minimise carbon
        # Priority 4: Minimise battery "effort" (reduces jumping/oscillation)
        
        w_unserved = 1e9   # Huge penalty for unserved load (backup service)
        w_carbon   = 1.0
        w_effort   = 0.01  # Small penalty to avoid violent oscillations
        w_soc_low  = 1e6   # Heavy penalty for dropping below target SOC (forces recharge)

        if goals.mode == OperationMode.CARBON_MINIMISE:
            # Standard carbon mode — now with penalties to ensure service reliability and recharging
            prob += (
                w_unserved * pulp.lpSum(unserved[t] for t in range(n))
                + w_soc_low * pulp.lpSum(soc_slack[t] for t in range(n))
                + pulp.lpSum(
                    goals.CI_grid.iloc[t] * p_grid[t] * dt
                    for t in range(n)
                )
                + w_effort * pulp.lpSum(p_ch[t] + p_dch[t] for t in range(n))
            )

        elif goals.mode == OperationMode.NONFIRM:
            prob += (
                w_unserved * pulp.lpSum(unserved[t] for t in range(n))
                + w_soc_low  * pulp.lpSum(soc_slack[t] for t in range(n))
                + w_carbon   * pulp.lpSum(
                    goals.CI_grid.iloc[t] * p_grid[t] * dt
                    for t in range(n)
                )
                + w_effort * pulp.lpSum(p_ch[t] + p_dch[t] for t in range(n))
            )

        # ── Constraints ───────────────────────────────────────────────────────
        for t in range(n):
            avail  = bool(goals.grid_available.iloc[t])
            p_dc_t = goals.p_DC.iloc[t]
            p_pv_t = goals.p_PV.iloc[t] if goals.p_PV is not None else 0.0

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

            # Soft SOC target: prefer keeping SOC above a baseline (e.g. 50% in carbon mode, 70% in nonfirm)
            if sys_config.E_BAT > 0.0:
                soc_baseline = 70.0 if goals.mode == OperationMode.NONFIRM else 50.0
                prob += soc[t] + soc_slack[t] >= soc_baseline

            # Grid limits and balance
            # p_grid_t must satisfy the energy balance (allow PV curtailment if p_grid_t >= 0 and RHS < 0)
            prob += p_grid[t] >= p_dc_t - p_pv_t + p_ch[t] - p_dch[t] - unserved[t]
            
            if not avail:
                prob += p_grid[t] == 0


        # End-of-day SOC target (soft: within ±10%)
        prob += soc[n - 1] >= goals.SOC_target_end - 10.0
        prob += soc[n - 1] <= goals.SOC_target_end + 10.0

        # ── Solve ─────────────────────────────────────────────────────────────
        # Use a safe CMD solver for background execution to prevent temporary file clashes and blocking.
        solver = pulp.PULP_CBC_CMD(msg=0, threads=1, keepFiles=False)
        prob.solve(solver)
        status = pulp.LpStatus[prob.status]
        obj_val = pulp.value(prob.objective)
        obj_str = f"{obj_val:.2f}" if obj_val is not None else "N/A"
        print(f"[GoalMgmt | Plan]    LP status={status}  objective={obj_str}")

        # ── Guard: infeasible LP → return zero-setpoint fallback plan ─────────
        if status != "Optimal":
            print(f"[GoalMgmt | Plan]    LP {status} — returning zero-setpoint fallback")
            p_net = pd.Series([0.0] * n, index=T, name="p_ch_b")
            soc_s = pd.Series([soc_init] * n, index=T, name="SOC_plan")
            schedule = SchedulePlan(p_ch_b=p_net, SOC_plan=soc_s, source="lp_fallback")
            self._knowledge["plan"] = schedule
            self.current_schedule = schedule
            self.goals = goals
            return schedule

        # ── Extract solution ──────────────────────────────────────────────────
        # Convention: positive = discharge, negative = charge
        # (matches ComponentControlLayer expectation)
        p_net  = pd.Series(
            [pulp.value(p_dch[t]) - pulp.value(p_ch[t]) for t in range(n)],
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

