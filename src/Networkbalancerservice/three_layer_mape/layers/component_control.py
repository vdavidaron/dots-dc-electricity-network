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
    carbon:    float    # Scope 2 CO2 this step [gCO2]
    cost:      float    # Energy cost this step [EUR]
    CI_battery: float   # Carbon intensity of energy in battery [gCO2/kWh]
    Price_battery: float # Price intensity of energy in battery [EUR/MWh]
    CI_DC_consumption: float # Effective CI of energy consumed by DC [gCO2/kWh]
    Price_DC_consumption: float # Effective price of energy consumed by DC [EUR/MWh]
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
        CI_battery_prev: float = 250.0, # CI of energy already in battery [gCO2/kWh]
        Price_grid: float = 0.0,    # Day-ahead price [EUR/MWh]
        Price_battery_prev: float = 0.0, # Price intensity of energy in battery [EUR/MWh]
        p_PV:       float = 0.0,    # Actual PV generation [kW]
        p_grid_limit_kw: float = 1e9, # Actual dynamic grid limit [kW]
        grid_cfe_frac: float = None  # Actual grid carbon-free share [fraction 0..1]
    ) -> ComponentState:

        dt        = sys_config.dt
        alarm     = False
        alarm_msg = ""
        p_ref     = setpoint # + discharge / - charge

        # Use the provided dynamic limit if it's smaller than the nameplate capacity
        actual_limit_kw = min(sys_config.P_GRID_MAX, p_grid_limit_kw) if grid_avail else 0.0

        # ── Hard carbon-free-operation gate (block mode) ─────────────────────
        # In block mode (cfe_constraint_mode == 2) the grid may be drawn from only
        # when its real-time carbon-free share meets the floor. A dirtier step is
        # handled exactly like a grid outage: PV + battery serve the load (limited
        # by the battery's actual state of charge) and any remainder becomes
        # unserved energy. Routing it through the islanding path — rather than
        # merely zeroing the grid limit — is what makes the floor honest: an
        # undersized battery shows real unserved energy instead of a phantom
        # over-discharge below SOC_MIN.
        cfe_mode = float(getattr(sys_config, 'cfe_constraint_mode', 0.0))
        cfe_thr  = float(getattr(sys_config, 'cfe_min_fraction', 0.0))
        grid_blocked = (cfe_mode == 2 and cfe_thr > 0.0
                        and grid_cfe_frac is not None and grid_cfe_frac < cfe_thr)
        grid_usable = grid_avail and not grid_blocked

        # ── Battery Enablement Check ─────────────────────────────────────────
        battery_enabled = getattr(sys_config, 'enable_battery', True)
        if not battery_enabled:
            p_ref = 0.0
        else:
            # ── Override: if grid unusable (outage or CFE-blocked), discharge to cover net DC load ──
            if not grid_usable:
                p_ref     = np.clip(p_DC - p_PV, -sys_config.P_CH_MAX, sys_config.P_DCH_MAX)
                alarm     = True
                alarm_msg = ("Carbon-free block — islanding on PV+battery"
                             if grid_blocked else
                             "Unplanned grid outage — emergency islanding")

            # ── Hard power clamp ─────────────────────────────────────────────────
            p_ref = np.clip(p_ref, -sys_config.P_CH_MAX, sys_config.P_DCH_MAX)

        # ── SOC bounds ───────────────────────────────────────────────────────
        if battery_enabled and sys_config.E_BAT > 0.0:
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
        # Grid draw = DC load - PV generation + Battery charging (-p_ref if p_ref < 0) - Battery discharging (p_ref if p_ref > 0)
        # So p_grid = p_DC - p_PV - p_ref
        if grid_usable:
            p_grid_calc = p_DC - p_PV - p_ref

            # If grid limit hit, we must first reduce charging (if any), then discharge more (if possible), then shed load.
            if p_grid_calc > actual_limit_kw:
                # We need p_grid = p_DC - p_PV - p_ref_new <= actual_limit_kw
                # So p_ref_new >= p_DC - p_PV - actual_limit_kw
                p_ref_new = max(p_ref, p_DC - p_PV - actual_limit_kw)
                p_ref = np.clip(p_ref_new, -sys_config.P_CH_MAX, sys_config.P_DCH_MAX)
                p_grid_calc = p_DC - p_PV - p_ref
                
            p_grid = np.clip(p_grid_calc, 0, actual_limit_kw)
            unserved = max(0.0, p_DC - p_PV - p_ref - actual_limit_kw)
        else:
            p_grid   = 0.0
            # Grid down: PV and battery must cover DC
            covered  = p_PV + max(0.0, p_ref)
            unserved = max(0.0, p_DC - covered)

        # ── Mass-balance accounting (carbon + price) ─────────────────────────
        energy_in_battery = (soc_actual / 100.0) * sys_config.E_BAT  # [kWh]
        carbon_in_battery = energy_in_battery * CI_battery_prev      # [gCO2]
        # price reservoir is in EUR/MWh × kWh = milliEUR (kept in EUR·kWh/MWh units)
        price_in_battery  = energy_in_battery * Price_battery_prev   # [EUR·kWh/MWh]

        if p_ref < 0:
            # Charging: figure out if grid or PV is providing the charge
            p_charge = -p_ref
            p_PV_to_DC = min(p_DC, p_PV)
            p_PV_excess = max(0.0, p_PV - p_DC)
            p_PV_to_BESS = min(p_charge, p_PV_excess)
            p_grid_to_BESS = max(0.0, p_charge - p_PV_to_BESS)

            energy_added = p_charge * dt * sys_config.EFF_CH
            # Zero CI / zero price for PV charge; grid CI / grid price for grid charge
            carbon_added = (p_grid_to_BESS * CI_grid + p_PV_to_BESS * 0.0) * dt * sys_config.EFF_CH
            price_added  = (p_grid_to_BESS * Price_grid + p_PV_to_BESS * 0.0) * dt * sys_config.EFF_CH

            carbon_in_battery += carbon_added
            price_in_battery  += price_added
            energy_in_battery += energy_added

            p_grid_to_DC = max(0.0, p_grid - p_grid_to_BESS)
            p_bess_to_DC = 0.0
        else:
            # Discharging: remove proportional carbon & price reservoir
            p_discharge = p_ref
            energy_removed = (p_discharge * dt) / sys_config.EFF_DCH
            carbon_in_battery -= energy_removed * CI_battery_prev
            price_in_battery  -= energy_removed * Price_battery_prev
            energy_in_battery -= energy_removed

            p_grid_to_DC = p_grid
            p_bess_to_DC = p_discharge

        if energy_in_battery > 0.01:
            CI_battery_new    = carbon_in_battery / energy_in_battery
            Price_battery_new = price_in_battery  / energy_in_battery
        else:
            CI_battery_new    = CI_battery_prev
            Price_battery_new = Price_battery_prev

        # Effective CI / price of DC consumption (grid + battery only).
        total_dc_carbon = (CI_grid    * p_grid_to_DC + CI_battery_prev    * p_bess_to_DC) * dt
        total_dc_price  = (Price_grid * p_grid_to_DC + Price_battery_prev * p_bess_to_DC) * dt
        # NOTE: Unserved load is deliberately NOT charged backup-generator carbon
        # here. Backup dispatch is owned by the BackupGen federate and only
        # happens when the controller actually commits it (which, on the traces
        # studied, it does not). The previous behaviour added 600 gCO2/kWh to
        # every kWh of *unserved* load whenever the backup asset was merely
        # enabled, while that same energy was still counted as unserved upstream.
        # That double-counted the energy (unserved AND served-by-backup) and
        # inflated cumulative carbon for any backup-enabled run, producing a
        # spurious gap between the 0 MW and non-zero backup-capacity points and
        # the artefactual "+12.45% carbon at zero storage" headline. Genuine
        # Scope-1 backup emissions are accounted by the BackupGen federate when
        # (and only when) it actually supplies power.

        if p_DC > 0.01:
            CI_DC_consumption    = total_dc_carbon / (p_DC * dt)
            Price_DC_consumption = total_dc_price  / (p_DC * dt)
        else:
            CI_DC_consumption    = CI_grid
            Price_DC_consumption = Price_grid

        # Step monetary cost = price × grid energy / 1000 (EUR/MWh × kWh → EUR)
        step_cost_eur = Price_grid * (p_grid * dt) / 1000.0

        return ComponentState(
            SOC=new_soc, p_ch_b=p_ref, p_grid=p_grid,
            p_DC=p_DC, unserved=unserved,
            carbon=total_dc_carbon,
            cost=step_cost_eur,
            CI_battery=CI_battery_new,
            Price_battery=Price_battery_new,
            CI_DC_consumption=CI_DC_consumption,
            Price_DC_consumption=Price_DC_consumption,
            alarm=alarm, alarm_msg=alarm_msg,
        )
