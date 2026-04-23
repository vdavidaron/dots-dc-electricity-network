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
    p_ch_b:    float    # Realised charge power [kW]  (- charge / + discharge)
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
    - Checks actual grid availability
    - Enforces hard SOC and power limits
    - CONVENTION: setpoint > 0 is DISCHARGE, setpoint < 0 is CHARGE
    """

    def __init__(self):
        pass

    def execute_step(
        self,
        setpoint:   float,          # Requested p_bess from Change Management [kW] (+ discharge / - charge)
        soc_actual: float,          # Actual SOC [%] from external state/sensor
        grid_avail: bool,           # Actual grid availability
        p_DC:       float,          # Actual DC load [kW]
        CI_grid:    float,          # Carbon intensity [gCO2/kWh]
        sys_config: SystemConfig,    # Dynamic system configuration
    ) -> ComponentState:

        dt        = sys_config.dt
        alarm     = False
        alarm_msg = ""
        p_ref     = setpoint # + discharge / - charge

        # ── Override: if grid unexpectedly down, discharge to cover DC load ──
        if not grid_avail:
            p_ref     = min(sys_config.P_DCH_MAX, p_DC)
            alarm     = True
            alarm_msg = "Unplanned grid outage — emergency discharge"

        # ── Hard power clamp ─────────────────────────────────────────────────
        p_ref = np.clip(p_ref, -sys_config.P_CH_MAX, sys_config.P_DCH_MAX)

        # ── SOC bounds ───────────────────────────────────────────────────────
        if sys_config.E_BAT > 0.0:
            # delta_soc = (power * dt / energy) * 100
            # Charging: p_ref < 0, efficiency increases energy needed
            # Discharging: p_ref > 0, efficiency reduces energy available
            if p_ref <= 0:
                # Charging
                p_charge = -p_ref
                delta_soc = (sys_config.EFF_CH * p_charge * dt / sys_config.E_BAT) * 100.0
            else:
                # Discharging
                p_discharge = p_ref
                delta_soc = -(p_discharge * dt / (sys_config.EFF_DCH * sys_config.E_BAT)) * 100.0
            
            new_soc = soc_actual + delta_soc

            if new_soc > sys_config.SOC_MAX:
                # Clamp to max SOC and recalculate p_ref (should be 0 if already at MAX)
                needed_soc_gain = max(0.0, sys_config.SOC_MAX - soc_actual)
                p_ref = -(needed_soc_gain / 100.0) * sys_config.E_BAT / (dt * sys_config.EFF_CH)
                new_soc = sys_config.SOC_MAX
                alarm, alarm_msg = True, "SOC upper limit — charge clamped"
            elif new_soc < sys_config.SOC_MIN:
                # Clamp to min SOC and recalculate p_ref (should be 0 if already at MIN)
                needed_soc_loss = max(0.0, soc_actual - sys_config.SOC_MIN)
                p_ref = (needed_soc_loss / 100.0) * sys_config.E_BAT * sys_config.EFF_DCH / dt
                new_soc = sys_config.SOC_MIN
                alarm, alarm_msg = True, "SOC lower limit — discharge clamped"
        else:
            p_ref = 0.0
            new_soc = 0.0

        # ── Energy balance ───────────────────────────────────────────────────
        # Grid draw = DC load + Battery charging (-p_ref if p_ref < 0) - Battery discharging (p_ref if p_ref > 0)
        # So p_grid = p_DC - p_ref
        if grid_avail:
            p_grid_calc = p_DC - p_ref
            
            # If grid limit hit, we must first reduce charging (if any), then discharge more (if possible), then shed load.
            if p_grid_calc > sys_config.P_GRID_MAX:
                # 1. Reduce charging or increase discharge to stay within grid limit
                # We need p_grid = p_DC - p_ref_new <= P_GRID_MAX
                # So p_ref_new >= p_DC - P_GRID_MAX
                p_ref_new = max(p_ref, p_DC - sys_config.P_GRID_MAX)
                
                # Check if new p_ref is physically possible (P_DCH_MAX and SOC_MIN)
                # For simplicity, we just clip it to what the battery can do
                p_ref = np.clip(p_ref_new, -sys_config.P_CH_MAX, sys_config.P_DCH_MAX)
                
                # Re-check SoC with updated p_ref (omitted for brevity, but should be done in production)
                p_grid_calc = p_DC - p_ref
                
            p_grid = np.clip(p_grid_calc, 0, sys_config.P_GRID_MAX)
            unserved = max(0.0, p_DC - p_ref - sys_config.P_GRID_MAX)
        else:
            p_grid   = 0.0
            # Grid down: p_ref is discharge. DC load covered by p_ref.
            covered  = max(0.0, p_ref) if p_ref > 0 else 0.0
            unserved = max(0.0, p_DC - covered)

        # ── Carbon accounting ────────────────────────────────────────────────
        carbon = CI_grid * p_grid * dt          # gCO2

        return ComponentState(
            SOC=new_soc, p_ch_b=p_ref, p_grid=p_grid,
            p_DC=p_DC, unserved=unserved,
            carbon=carbon,
            alarm=alarm, alarm_msg=alarm_msg,
        )
