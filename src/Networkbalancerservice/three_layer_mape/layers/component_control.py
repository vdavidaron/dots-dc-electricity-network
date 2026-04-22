# ══════════════════════════════════════════════════════════════════════════════
# layers/component_control.py
#
# Physical execution layer — no MAPE-K loop here.
# Executes setpoints, enforces hard safety limits, reports status upward.
# Time scale: milliseconds → seconds (simulated per hour step)
# ══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd
from .goal_management import SystemConfig


@dataclass
class ComponentState:
    SOC:       float    # Realised SOC [%]
    p_ch_b:    float    # Realised charge power [kW]  (+ charge / − discharge)
    p_grid:    float    # Realised grid draw [kW]
    p_DC:      float    # DC load [kW]
    unserved:  float    # Unserved DC load [kW]
    carbon:    float    # Scope 2 CO2 this hour [gCO2]
    alarm:     bool  = False
    alarm_msg: str   = ""


class ComponentControlLayer:
    """
    Executes one hourly timestep.
    - Takes setpoint from Change Management
    - Checks actual grid availability (may differ from forecast)
    - Enforces hard SOC and power limits (always overrides upper layers)
    - Returns ComponentState upward for MAPE-K Loop 2 monitoring
    """

    def __init__(self):
        pass

    def execute_step(
        self,
        setpoint:   float,          # Requested p_ch_b from Change Management [kW]
        soc_actual: float,          # Actual SOC [%] from external state/sensor
        grid_avail: bool,           # Actual grid availability this hour
        p_DC:       float,          # Actual DC load [kW]
        CI_grid:    float,          # Carbon intensity [gCO2/kWh]
        sys_config: SystemConfig,    # Dynamic system configuration
    ) -> ComponentState:

        dt        = sys_config.dt
        alarm     = False
        alarm_msg = ""
        p_ref     = setpoint

        # ── Override: if grid unexpectedly down, discharge to cover DC load ──
        if not grid_avail:
            p_ref     = -min(sys_config.P_DCH_MAX, p_DC)
            alarm     = True
            alarm_msg = "Unplanned grid outage — emergency discharge"

        # ── Hard power clamp ─────────────────────────────────────────────────
        p_ref = np.clip(p_ref, -sys_config.P_DCH_MAX, sys_config.P_CH_MAX)

        # ── SOC bounds ───────────────────────────────────────────────────────
        if sys_config.E_BAT > 0.0:
            # delta_soc = (power * dt / energy) * 100
            # Charging: p_ref > 0, efficiency applied
            # Discharging: p_ref < 0, efficiency applied
            if p_ref >= 0:
                delta_soc = (sys_config.EFF_CH * p_ref * dt / sys_config.E_BAT) * 100.0
            else:
                delta_soc = (p_ref * dt / (sys_config.EFF_DCH * sys_config.E_BAT)) * 100.0
            
            new_soc = soc_actual + delta_soc

            if new_soc > sys_config.SOC_MAX:
                # Clamp to max SOC and recalculate p_ref
                p_ref = ((sys_config.SOC_MAX - soc_actual) / 100.0) * sys_config.E_BAT / (dt * sys_config.EFF_CH)
                new_soc = sys_config.SOC_MAX
                alarm, alarm_msg = True, "SOC upper limit — charge clamped"
            elif new_soc < sys_config.SOC_MIN:
                # Clamp to min SOC and recalculate p_ref
                p_ref = ((sys_config.SOC_MIN - soc_actual) / 100.0) * sys_config.E_BAT * sys_config.EFF_DCH / dt
                new_soc = sys_config.SOC_MIN
                alarm, alarm_msg = True, "SOC lower limit — discharge clamped"
        else:
            p_ref = 0.0
            new_soc = 0.0

        # ── Energy balance ───────────────────────────────────────────────────
        if grid_avail:
            # Grid draw = DC load + Battery charging (p_ref > 0) - Battery discharging (p_ref < 0)
            p_grid_calc = p_DC + p_ref
            p_grid = np.clip(p_grid_calc, 0, sys_config.P_GRID_MAX)
            # If DC load + charging exceeds grid limit, DC load is priority, charging is clamped
            if p_grid_calc > sys_config.P_GRID_MAX:
                # This is a simplification; in reality, we'd reduce p_ref first
                unserved = max(0.0, p_DC + p_ref - sys_config.P_GRID_MAX)
            else:
                unserved = 0.0
        else:
            p_grid   = 0.0
            covered  = min(p_DC, abs(p_ref)) if p_ref < 0 else 0.0
            unserved = max(0.0, p_DC - covered)

        # ── Carbon accounting ────────────────────────────────────────────────
        carbon = CI_grid * p_grid * dt          # gCO2

        return ComponentState(
            SOC=new_soc, p_ch_b=p_ref, p_grid=p_grid,
            p_DC=p_DC, unserved=unserved,
            carbon=carbon,
            alarm=alarm, alarm_msg=alarm_msg,
        )
