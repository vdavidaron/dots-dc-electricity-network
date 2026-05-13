# layers/change_management.py
#
# MAPE-K Loop 2 — INTRA-DAY (fast loop, runs every 15 min)
# ────────────────────────────────────────────────────────
#  Monitor  → read actual SOC, grid meter, demand from Component Control
#  Analyze  → detect deviation from LP plan:
#               · SOC drift > threshold
#               · Unplanned grid outage
#               · Demand spike > threshold  ← NEW
#  Plan     → rolling MPC: re-solve LP over remaining horizon with actual
#             SOC and corrected demand forecast
#  Execute  → push updated setpoint to Component Control Layer
#
# Knowledge store: active LP plan, current SOC, demand history, deviation history
# ────────────────────────────────────────────────────────

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import pulp

from .goal_management import Goals, SchedulePlan, SystemConfig


# ── Deviation event ───────────────────────────────────────────────────────────

@dataclass
class DeviationEvent:
    hour:             int
    soc_actual:       float
    soc_planned:      float
    soc_drift:        float
    unplanned_outage: bool
    demand_delta:     float   # p_dc_actual - p_dc_forecast [kW]; positive = spike
    demand_spike:     bool    # True when demand_delta > DEMAND_SPIKE_THRESHOLD
    triggered_replan: bool


# ── MAPE-K Loop 2 ─────────────────────────────────────────────────────────────

class ChangeManagementLayer:
    """
    Fast MAPE-K loop — intra-day reactive control.

    Every hour it:
      1. Monitors actual SOC and grid status from Component Control
      2. Analyzes whether the LP plan is still feasible
      3. Re-plans (MPC rolling horizon) if deviation exceeds threshold
      4. Executes by returning the current-hour setpoint

    MPC window: configurable (default 6h rolling horizon).
    Trigger conditions:
      - |SOC_actual - SOC_planned| > SOC_DRIFT_THRESHOLD
      - Unplanned grid outage detected
    """

    SOC_DRIFT_THRESHOLD    = 5.0   # [%]  — replan if SOC drifts more than this
    DEMAND_SPIKE_THRESHOLD = 0.10   # [fraction] — replan if actual demand > forecast by 10 %
    MPC_HORIZON            = 6     # timesteps — rolling window for MPC re-solve
    REPLAN_COOLDOWN        = 4     # steps to wait between replans (4 × 15min = 1h)

    def __init__(self):
        self.plan:     Optional[SchedulePlan] = None
        self.goals:    Optional[Goals]        = None
        self._steps_since_replan: int = 999  # allow replan on first trigger
        self._knowledge: dict = {
            "soc_history":    [],
            "demand_history": [],
            "deviation_history": [],
            "replan_count":   0,
        }

    def load_day_ahead_plan(self, goals: Goals, plan: SchedulePlan) -> None:
        """Called once per day by orchestrator after Goal Management runs."""
        self.goals = goals
        self.plan  = plan
        self._knowledge["replan_count"] = 0
        print(f"[ChangeMgmt | Load]  Day-ahead LP plan loaded ({len(plan.p_ch_b)}h)")

    # ── Monitor ───────────────────────────────────────────────────────────────

    def monitor(self, hour: int, soc_actual: float, grid_actual: bool, p_dc_actual_kw: float = 0.0) -> dict:
        """
        Collect real-time state from Component Control.

        Parameters
        ----------
        hour           : current 15-min step index within the day
        soc_actual     : measured battery SoC [%]
        grid_actual    : whether the grid is available right now
        p_dc_actual_kw : actual DC demand observed this step [kW]
        """
        p_dc_forecast_kw = (
            float(self.goals.p_DC.iloc[hour])
            if self.goals is not None and hour < len(self.goals.p_DC)
            else p_dc_actual_kw
        )
        state = {
            "hour":             hour,
            "soc_actual":       soc_actual,
            "grid_actual":      grid_actual,
            "soc_planned":      self.plan.SOC_plan.iloc[hour] if self.plan else soc_actual,
            "p_dc_actual_kw":   p_dc_actual_kw,
            "p_dc_forecast_kw": p_dc_forecast_kw,
        }
        self._knowledge["soc_history"].append(soc_actual)
        self._knowledge["demand_history"].append(p_dc_actual_kw)
        return state

    # ── Analyze ───────────────────────────────────────────────────────────────

    def analyze(self, state: dict) -> DeviationEvent:
        """Detect whether the current LP plan needs to be revised."""
        hour             = state["hour"]
        soc_actual       = state["soc_actual"]
        soc_planned      = state["soc_planned"]
        grid_actual      = state["grid_actual"]
        p_dc_actual_kw   = state.get("p_dc_actual_kw", 0.0)
        p_dc_forecast_kw = state.get("p_dc_forecast_kw", p_dc_actual_kw)

        # Was this step expected to have grid?
        grid_forecast    = bool(self.goals.grid_available.iloc[hour]) if self.goals else True
        unplanned_outage = (not grid_actual) and grid_forecast

        # SoC deviation
        soc_drift = abs(soc_actual - soc_planned)

        # Demand spike: actual demand exceeds forecast by more than threshold
        demand_delta = p_dc_actual_kw - p_dc_forecast_kw
        demand_spike = (
            p_dc_forecast_kw > 0.0
            and (demand_delta / p_dc_forecast_kw) > self.DEMAND_SPIKE_THRESHOLD
        )

        # Only allow replan if cooldown has elapsed
        deviation_detected = (
            soc_drift > self.SOC_DRIFT_THRESHOLD
            or unplanned_outage
            or demand_spike
        )
        triggered = deviation_detected and self._steps_since_replan >= self.REPLAN_COOLDOWN
        self._steps_since_replan += 1

        event = DeviationEvent(
            hour=hour,
            soc_actual=soc_actual,
            soc_planned=soc_planned,
            soc_drift=soc_drift,
            unplanned_outage=unplanned_outage,
            demand_delta=demand_delta,
            demand_spike=demand_spike,
            triggered_replan=triggered,
        )
        self._knowledge["deviation_history"].append(event)

        if triggered:
            reason = []
            if soc_drift > self.SOC_DRIFT_THRESHOLD:
                reason.append(f"SOC drift={soc_drift:.1f}%")
            if unplanned_outage:
                reason.append("unplanned outage")
            if demand_spike:
                reason.append(
                    f"demand spike={demand_delta:+.1f} kW "
                    f"({demand_delta / p_dc_forecast_kw * 100:.1f}% above forecast)"
                )
            print(f"[ChangeMgmt | Analyze] Deviation at step={hour}: {', '.join(reason)}")

        return event

    # ── Plan (MPC rolling horizon) ────────────────────────────────────────────

    def replan_mpc(self, event: DeviationEvent, sys_config: SystemConfig) -> SchedulePlan:
        """
        Rolling MPC: re-solve LP over the next MPC_HORIZON steps
        using the actual SOC and (if a demand spike occurred) the
        observed demand as the corrected forecast for the remaining horizon.

        Uses the same PuLP LP structure as Goal Management,
        but over a shorter rolling window.
        """
        hour     = event.hour
        soc_init = event.soc_actual
        n_total  = len(self.goals.p_DC)
        horizon  = min(self.MPC_HORIZON, n_total - hour)

        if horizon <= 0:
            return self.plan

        # Slice forecast to MPC window
        T_mpc   = self.goals.p_DC.index[hour: hour + horizon]
        ci      = self.goals.CI_grid.iloc[hour: hour + horizon]
        avail   = self.goals.grid_available.iloc[hour: hour + horizon]
        pv      = self.goals.p_PV.iloc[hour: hour + horizon] if self.goals.p_PV is not None else pd.Series([0.0]*horizon, index=T_mpc)
        price   = self.goals.price_E.iloc[hour: hour + horizon] if self.goals.price_E is not None else pd.Series([0.0]*horizon, index=T_mpc)

        # If a demand spike was detected, patch the forecast for the MPC horizon
        # with the observed actual demand so the LP doesn't optimise against a
        # stale (too-low) load estimate.
        p_dc = self.goals.p_DC.iloc[hour: hour + horizon].copy()
        if event.demand_spike:
            corrected_kw = float(self.goals.p_DC.iloc[hour]) + event.demand_delta
            p_dc = pd.Series(
                [corrected_kw] * horizon,
                index=T_mpc,
                name="p_DC"
            )
            print(f"[ChangeMgmt | Plan]  Demand forecast patched: "
                  f"{self.goals.p_DC.iloc[hour]:.1f} → {corrected_kw:.1f} kW "
                  f"for next {horizon} steps")

        prob = pulp.LpProblem("BESS_MPC", pulp.LpMinimize)
        n    = horizon
        dt   = sys_config.dt

        p_ch     = [pulp.LpVariable(f"mpc_pch_{t}",  lowBound=0, upBound=sys_config.P_CH_MAX)  for t in range(n)]
        p_dch    = [pulp.LpVariable(f"mpc_pdch_{t}", lowBound=0, upBound=sys_config.P_DCH_MAX) for t in range(n)]
        p_grid   = [pulp.LpVariable(f"mpc_pgrid_{t}", lowBound=0, upBound=sys_config.P_GRID_MAX) for t in range(n)]
        soc_var  = [pulp.LpVariable(f"mpc_soc_{t}",  lowBound=sys_config.SOC_MIN, upBound=sys_config.SOC_MAX) for t in range(n)]
        unserved = [pulp.LpVariable(f"mpc_uns_{t}",  lowBound=0) for t in range(n)]
        soc_slack = [pulp.LpVariable(f"mpc_soc_slack_{t}", lowBound=0) for t in range(n)]

        # Objective — same as Goal Management
        w_unserved = sys_config.w_unserved
        w_carbon   = sys_config.w_carbon
        w_price    = getattr(sys_config, "w_price", 0.0)
        w_effort   = sys_config.w_effort
        w_soc_low  = sys_config.w_soc_low

        prob += (
            w_unserved * pulp.lpSum(unserved[t] for t in range(n))
            + w_soc_low * pulp.lpSum(soc_slack[t] for t in range(n))
            + w_carbon * pulp.lpSum(
                ci.iloc[t] * p_grid[t] * dt
                for t in range(n)
            )
            + w_price * pulp.lpSum(
                price.iloc[t] * p_grid[t] * dt
                for t in range(n)
            )
            + w_effort * pulp.lpSum(p_ch[t] + p_dch[t] for t in range(n))
        )

        # Constraints
        for t in range(n):
            soc_prev = soc_init if t == 0 else soc_var[t - 1]
            p_dc_t = p_dc.iloc[t]
            p_pv_t = pv.iloc[t]
            
            if sys_config.E_BAT > 0.0:
                prob += soc_var[t] == soc_prev + (
                    (sys_config.EFF_CH * p_ch[t] * dt / sys_config.E_BAT) * 100.0
                  - (p_dch[t] * dt / (sys_config.EFF_DCH * sys_config.E_BAT)) * 100.0
                )
            else:
                prob += soc_var[t] == 0.0
                prob += p_ch[t] == 0.0
                prob += p_dch[t] == 0.0

            # Soft SOC target: prefer keeping SOC above the configurable baseline
            if sys_config.E_BAT > 0.0:
                prob += soc_var[t] + soc_slack[t] >= sys_config.soc_baseline

            # Grid limits and balance
            prob += p_grid[t] >= p_dc_t - p_pv_t + p_ch[t] - p_dch[t] - unserved[t]
            
            if not bool(avail.iloc[t]):
                prob += p_grid[t] == 0

        # Use a safe CMD solver for background execution to prevent temporary file clashes and blocking.
        solver = pulp.PULP_CBC_CMD(
            msg=0, 
            threads=1, 
            keepFiles=False, 
            timeLimit=10,
            options=['-presolve', 'off']
        )
        prob.solve(solver)

        status = pulp.LpStatus[prob.status]
        self._knowledge["replan_count"] += 1
        self._steps_since_replan = 0  # reset cooldown

        if status != "Optimal":
            print(f"[ChangeMgmt | Plan]  MPC replan #{self._knowledge['replan_count']} "
                  f"h={hour}->{hour+horizon-1}  LP status={status} — keeping existing plan")
            return self.plan

        # Merge MPC solution back into full-day plan
        # Convention: positive = discharge, negative = charge
        merged_p   = self.plan.p_ch_b.copy()
        merged_soc = self.plan.SOC_plan.copy()

        for t in range(n):
            idx = hour + t
            val_dch = pulp.value(p_dch[t])
            val_ch  = pulp.value(p_ch[t])
            val_soc = pulp.value(soc_var[t])
            if val_dch is not None and val_ch is not None:
                merged_p.iloc[idx] = val_dch - val_ch
            if val_soc is not None:
                merged_soc.iloc[idx] = val_soc

        self.plan = SchedulePlan(p_ch_b=merged_p, SOC_plan=merged_soc, source="mpc")
        print(f"[ChangeMgmt | Plan]  MPC replan #{self._knowledge['replan_count']} "
              f"h={hour}->{hour+horizon-1}  LP status={status}")
        return self.plan

    # ── Execute ───────────────────────────────────────────────────────────────

    def execute(
        self,
        hour: int,
        soc_actual: float,
        grid_actual: bool,
        sys_config: SystemConfig,
        p_dc_actual_kw: float = 0.0,
    ) -> tuple[float, SchedulePlan]:
        """
        Full MAPE-K cycle for one timestep.
        Returns (setpoint_kw, updated_plan).

        Parameters
        ----------
        p_dc_actual_kw : actual DC demand this step [kW]; forwarded to monitor()
                         so demand spikes can trigger replanning.
        """
        state = self.monitor(hour, soc_actual, grid_actual, p_dc_actual_kw=p_dc_actual_kw)
        event = self.analyze(state)

        if event.triggered_replan:
            self.replan_mpc(event, sys_config)

        setpoint = float(self.plan.p_ch_b.iloc[hour])
        return setpoint, self.plan

    def get_replan_count(self) -> int:
        return self._knowledge["replan_count"]
