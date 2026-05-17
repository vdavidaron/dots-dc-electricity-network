from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List
import threading
import logging
import json
import math
import time
import requests

# Monkey-patch requests to ensure InfluxDB never hangs indefinitely
_orig_request = requests.Session.request
def _patched_request(self, method, url, **kwargs):
    if 'timeout' not in kwargs or kwargs['timeout'] is None:
        kwargs['timeout'] = 5.0  # 5 second timeout for DB operations
    return _orig_request(self, method, url, **kwargs)
requests.Session.request = _patched_request

from esdl import esdl, EnergySystem
import helics as h

from dots_infrastructure.DataClasses import EsdlId, TimeStepInformation
from dots_infrastructure.Logger import LOGGER
from dots_infrastructure import CalculationServiceHelperFunctions

from networkbalancerservice_base import NetworkbalancerserviceBase
from networkbalancerservice_dataclasses import NetworkDispatchOutput

from three_layer_mape.layers.goal_management import (
    GoalManagementLayer, SystemConfig, Goals, SchedulePlan
)
from three_layer_mape.layers.change_management import ChangeManagementLayer, DeviationEvent
from three_layer_mape.layers.component_control import ComponentControlLayer, ComponentState

import pandas as pd
import numpy as np

from forecast_error import ForecastErrorModel

LOGGER = logging.getLogger(__name__)


class Networkbalancerservice(NetworkbalancerserviceBase):
    """
    Intelligent network balancer with dynamic ESDL-based configuration.
    """

    def __init__(self):
        super().__init__()
        from dots_infrastructure.influxdb_connector import InfluxDBConnector
        self.influx_connector = InfluxDBConnector(
            self.simulator_configuration.influx_host, 
            self.simulator_configuration.influx_port, 
            self.simulator_configuration.influx_username, 
            self.simulator_configuration.influx_password, 
            self.simulator_configuration.influx_database_name
        )
        self.current_soc = 50.0

        # ── Forecast error model ───────────────────────────────
        # Constructed for real in init_calculation_service() once the ESDL
        # KPIs have been parsed; until then, a fallback model is in place.
        self._forecast_error = ForecastErrorModel()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def init_calculation_service(self, energy_system: esdl.EnergySystem):
        from esdl import ElectricityNetwork
        from dots_infrastructure.EsdlHelperFunctions import EsdlHelperFunctions
        all_esdl_objs = EsdlHelperFunctions.get_all_esdl_objects_from_type(energy_system.eAllContents(), ElectricityNetwork)
        for esdl_obj in all_esdl_objs:
            if hasattr(esdl_obj, "id"):
                self.esdl_obj_mapping[esdl_obj.id] = esdl_obj
        
        LOGGER.info("Initializing Network Balancer Service...")

        # Default fallback values
        self.grid_import_limit_w = 4_000_000.0
        self.dc_base_load_w = 4_000_000.0

        # Refresh from ESDL
        self._refresh_system_params(energy_system)

        # Save toggle values before reconstructing SystemConfig (which would reset them)
        _enable_battery = getattr(self.sys_config, 'enable_battery', True)
        _enable_backup_generator = getattr(self.sys_config, 'enable_backup_generator', True)
        _enable_renewable_service = getattr(self.sys_config, 'enable_renewable_service', True)
        _enable_change_management = getattr(self.sys_config, 'enable_change_management', True)
        _enable_goal_management = getattr(self.sys_config, 'enable_goal_management', True)
        _w_unserved = getattr(self.sys_config, 'w_unserved', 1e9)
        _w_carbon = getattr(self.sys_config, 'w_carbon', 1.0)
        _w_price = getattr(self.sys_config, 'w_price', 0.0)
        _w_effort = getattr(self.sys_config, 'w_effort', 0.01)
        _w_soc_low = getattr(self.sys_config, 'w_soc_low', 1e6)
        _soc_baseline = getattr(self.sys_config, 'soc_baseline', 50.0)
        _enable_mandate = getattr(self.sys_config, 'enable_mandate', True)
        _mpc_soc_drift   = getattr(self.sys_config, 'mpc_soc_drift_threshold', 5.0)
        _mpc_demand_spike = getattr(self.sys_config, 'mpc_demand_spike_threshold', 0.10)
        _mpc_horizon     = getattr(self.sys_config, 'mpc_horizon_steps', 24)
        _mpc_cooldown    = getattr(self.sys_config, 'mpc_replan_cooldown', 4)
        _f_sigma_ci      = getattr(self.sys_config, 'forecast_sigma_ci', 0.12)
        _f_sigma_p_dc    = getattr(self.sys_config, 'forecast_sigma_p_dc', 0.05)
        _f_sigma_price   = getattr(self.sys_config, 'forecast_sigma_price', 0.15)
        _f_seed          = getattr(self.sys_config, 'forecast_seed', 42)

        self.sys_config = SystemConfig(
            dt=0.25, 
            E_BAT=self.sys_config.E_BAT if hasattr(self, 'sys_config') else 0.0, 
            P_CH_MAX=self.sys_config.P_CH_MAX if hasattr(self, 'sys_config') else 0.0, 
            P_DCH_MAX=self.sys_config.P_DCH_MAX if hasattr(self, 'sys_config') else 0.0, 
            P_GRID_MAX=self.grid_import_limit_w / 1000.0,
            SOC_MIN=0.0,
            SOC_MAX=100.0
        )

        # Re-apply toggles and weights
        self.sys_config.enable_battery = _enable_battery
        self.sys_config.enable_backup_generator = _enable_backup_generator
        self.sys_config.enable_renewable_service = _enable_renewable_service
        self.sys_config.enable_change_management = _enable_change_management
        self.sys_config.enable_goal_management = _enable_goal_management
        self.sys_config.w_unserved = _w_unserved
        self.sys_config.w_carbon = _w_carbon
        self.sys_config.w_price = _w_price
        self.sys_config.w_effort = _w_effort
        self.sys_config.w_soc_low = _w_soc_low
        self.sys_config.soc_baseline = _soc_baseline
        self.sys_config.enable_mandate = _enable_mandate
        self.sys_config.mpc_soc_drift_threshold = _mpc_soc_drift
        self.sys_config.mpc_demand_spike_threshold = _mpc_demand_spike
        self.sys_config.mpc_horizon_steps = _mpc_horizon
        self.sys_config.mpc_replan_cooldown = _mpc_cooldown
        self.sys_config.forecast_sigma_ci    = _f_sigma_ci
        self.sys_config.forecast_sigma_p_dc  = _f_sigma_p_dc
        self.sys_config.forecast_sigma_price = _f_sigma_price
        self.sys_config.forecast_seed        = _f_seed

        # Handle battery baseline: zero out all battery parameters
        if not self.sys_config.enable_battery:
            self.sys_config.E_BAT = 0.0
            self.sys_config.P_CH_MAX = 0.0
            self.sys_config.P_DCH_MAX = 0.0

        LOGGER.info(
            "SystemConfig: dt=%.2f  E_BAT=%.1f kWh  P_CH=%.1f kW  P_DCH=%.1f kW  "
            "P_GRID=%.1f kW  SOC=[%.0f,%.0f]%%  EFF_CH=%.2f  EFF_DCH=%.2f",
            self.sys_config.dt, self.sys_config.E_BAT,
            self.sys_config.P_CH_MAX, self.sys_config.P_DCH_MAX,
            self.sys_config.P_GRID_MAX,
            self.sys_config.SOC_MIN, self.sys_config.SOC_MAX,
            self.sys_config.EFF_CH, self.sys_config.EFF_DCH,
        )
        LOGGER.info(
            "Toggles: battery=%s  backup=%s  renewable=%s  change_mgmt=%s  goal_mgmt=%s",
            self.sys_config.enable_battery, self.sys_config.enable_backup_generator,
            self.sys_config.enable_renewable_service, self.sys_config.enable_change_management,
            self.sys_config.enable_goal_management,
        )

        # Layers
        self.goal_layer    = GoalManagementLayer()
        self.change_layer  = ChangeManagementLayer()

        self.control_layer = ComponentControlLayer()

        # Reconstruct the forecast-error model with ESDL-derived parameters
        # (the __init__-time instance used literature-fallback defaults because
        # the ESDL hadn't been parsed yet).
        self._forecast_error = ForecastErrorModel(
            seed        = self.sys_config.forecast_seed,
            sigma_ci    = self.sys_config.forecast_sigma_ci,
            sigma_p_dc  = self.sys_config.forecast_sigma_p_dc,
            sigma_price = self.sys_config.forecast_sigma_price,
        )

        self.current_day_step_idx = 0
        self._pending_da_result: Optional[Tuple[Goals, SchedulePlan]] = None
        self._mpc_running  = False
        self.current_ci_battery    = 250.0  # [gCO2/kWh] Initial assumption
        self.current_price_battery = 0.0    # [EUR/MWh] Initial assumption

        # Cumulative running totals (reset on service init only — not per day)
        self._cum_carbon_g       = 0.0   # gCO2
        self._cum_cost_eur       = 0.0   # EUR
        self._cum_unserved_kwh   = 0.0   # kWh
        self._cum_grid_energy_kwh = 0.0  # kWh — denominator for effective CI/price

        # Counterfactual "no-optimisation" baseline: spot-price × instantaneous DC load
        self._cum_baseline_cost_eur = 0.0  # EUR — what DC would have paid at spot
        self._cum_baseline_carbon_g = 0.0  # gCO2 — same baseline for carbon

        # InfluxDB write pacing — see notes at end of _do_network_dispatch
        self._influx_flush_every_n_steps = 96   # once per simulated day (96 × 15min)
        self._steps_since_influx_flush  = 0
        
        self._state_cache = {
            "actual_power_limit_ID": self.grid_import_limit_w,
            "actual_carbon_intensity_ID": 250.0,
            "actual_electricity_price_ID": 50.0,
            "mandated_min_power_draw_ID": 0.0,
            "available_max_power": 0.0
        }

    # ── Background thread helpers ─────────────────────────────────────────────

    def _run_day_ahead_lp(self, simulation_time: datetime, esdl_id: str, raw_limit: list[float], raw_ci: list[float], raw_price: list[float], raw_demand: list[float], raw_pv: list[float]) -> None:
        try:
            if raw_limit:
                LOGGER.info("[DA thread] Received power_limit_plan_DA VECTOR")
            else:
                LOGGER.warning("[DA thread] Timed out waiting for power_limit_plan_DA. Using fallback.")
                
            if raw_demand:
                LOGGER.info("[DA thread] Received demand_power_plan_da VECTOR")
            else:
                LOGGER.warning("[DA thread] Timed out waiting for demand_power_plan_da. Using fallback.")

            grid_limits_kw = self._parse_vector(raw_limit, self.grid_import_limit_w / 1000.0, divide_by_1000=True)
            n = len(grid_limits_kw)

            if raw_ci:
                ci_actual = self._parse_vector(raw_ci, 250.0, divide_by_1000=False)
            else:
                month = simulation_time.month
                seasonal_offset = 40.0 * np.cos(2 * np.pi * (month - 1) / 12)
                ci_actual = [
                    float(np.clip(
                        250.0 + seasonal_offset
                        - 60.0 * np.exp(-((i - 52) ** 2) / 40.0),
                        50.0, 600.0
                    ))
                    for i in range(n)
                ]

            dc_base_kw = self.dc_base_load_w / 1000.0
            if raw_demand:
                dc_demand_actual = self._parse_vector(raw_demand, dc_base_kw, divide_by_1000=True)
            else:
                dc_demand_actual = [
                    dc_base_kw * (1.0 + 0.03 * np.sin(2 * np.pi * i / n))
                    for i in range(n)
                ]

            pv_actual = self._parse_vector(raw_pv, 0.0, divide_by_1000=True)

            price_actual = self._parse_vector(raw_price, 50.0, divide_by_1000=False)
            if len(price_actual) != n:
                price_actual = (price_actual + [price_actual[-1]] * n)[:n]

            forecast = {
                "CI_grid":        self._forecast_error.perturb("CI_grid",  pd.Series(ci_actual)),
                "grid_available": pd.Series([(v > 0) for v in grid_limits_kw]),
                "p_DC":           self._forecast_error.perturb("p_DC",     pd.Series(dc_demand_actual)),
                "p_PV":           pd.Series(pv_actual),
                "price_E":        self._forecast_error.perturb("price_E", pd.Series(price_actual)),
            }

            LOGGER.info(
                "[DA Forecast] t=%s  n=%d steps  CI_mean=%.0f gCO2/kWh  p_DC_mean=%.0f kW  p_PV_mean=%.0f kW  price_mean=%.1f EUR/MWh",
                simulation_time.isoformat(), n,
                float(forecast["CI_grid"].mean()),
                float(forecast["p_DC"].mean()),
                float(forecast["p_PV"].mean()),
                float(forecast["price_E"].mean())
            )

            if getattr(self.sys_config, 'enable_goal_management', True):
                goals, plan = self.goal_layer.execute(forecast, self.sys_config, soc_init=self.current_soc)
            else:
                LOGGER.info("[DA thread] Goal Management disabled. Generating flat baseline plan.")
                goals = Goals(
                    SOC_target_end=self.current_soc,
                    CI_grid=forecast["CI_grid"],
                    grid_available=forecast["grid_available"],
                    p_DC=forecast["p_DC"],
                    p_PV=forecast["p_PV"],
                    price_E=forecast["price_E"]
                )
                flat_p_ch_b = pd.Series([0.0] * n, index=forecast["CI_grid"].index)
                target_soc = 0.0 if self.sys_config.E_BAT == 0.0 else self.current_soc
                flat_soc = pd.Series([target_soc] * n, index=forecast["CI_grid"].index)
                plan = SchedulePlan(p_ch_b=flat_p_ch_b, SOC_plan=flat_soc, source="baseline")

            self._pending_da_result = (goals, plan)

            # ── LOG Full Future DA Plan ──────────────────────────────────────
            p_bess_json, soc_json = [], []
            for i in range(len(plan.p_ch_b)):
                future_time = simulation_time + timedelta(seconds=900 * i)
                ts = future_time.isoformat()
                
                val_soc = float(plan.SOC_plan.iloc[i])
                val_p   = float(plan.p_ch_b.iloc[i]) * 1000.0
                
                # 1. Individual future points
                self.influx_connector.set_time_step_data_point(esdl_id, "DA_Future_Planned_SOC", future_time, val_soc)
                self.influx_connector.set_time_step_data_point(esdl_id, "DA_Future_Planned_BESS_Power_W", future_time, val_p)
                
                # 2. Build JSON arrays
                soc_json.append({"time": ts, "value": round(val_soc, 2)})
                p_bess_json.append({"time": ts, "value": round(val_p, 1)})

            # Log full JSON plans at the current simulation time
            self.influx_connector.set_time_step_data_point(esdl_id, "bess_power_plan_DA", simulation_time, json.dumps(p_bess_json))
            self.influx_connector.set_time_step_data_point(esdl_id, "soc_plan_DA",        simulation_time, json.dumps(soc_json))

            LOGGER.info(f"[DA thread] Plan generated and logged for T={simulation_time.isoformat()}.")
        except Exception as exc:
            LOGGER.error(f"[DA thread] Failed: {exc}")

    def _run_mpc_replan(self, event: DeviationEvent, simulation_time: datetime, esdl_id: str) -> None:
        try:
            updated_plan = self.change_layer.replan_mpc(event, self.sys_config)
            
            # ── LOG Full Future MPC Plan ─────────────────────────────────────
            # Only log the re-solved horizon
            start_hour = event.hour
            horizon = min(self.change_layer.MPC_HORIZON, len(updated_plan.p_ch_b) - start_hour)
            
            p_bess_json, soc_json = [], []
            for i in range(horizon):
                idx = start_hour + i
                future_time = simulation_time + timedelta(seconds=900 * i)
                ts = future_time.isoformat()
                
                val_soc = float(updated_plan.SOC_plan.iloc[idx])
                val_p   = float(updated_plan.p_ch_b.iloc[idx]) * 1000.0
                
                # 1. Individual future points
                self.influx_connector.set_time_step_data_point(esdl_id, "MPC_Future_Planned_SOC", future_time, val_soc)
                self.influx_connector.set_time_step_data_point(esdl_id, "MPC_Future_Planned_BESS_Power_W", future_time, val_p)
                
                # 2. Build JSON arrays
                soc_json.append({"time": ts, "value": round(val_soc, 2)})
                p_bess_json.append({"time": ts, "value": round(val_p, 1)})

            # Log full JSON plans at current time
            self.influx_connector.set_time_step_data_point(esdl_id, "bess_power_plan_MPC", simulation_time, json.dumps(p_bess_json))
            self.influx_connector.set_time_step_data_point(esdl_id, "soc_plan_MPC",        simulation_time, json.dumps(soc_json))
            
        except Exception as exc:
            LOGGER.error(f"[MPC thread] Failed: {exc}")
        finally:
            self._mpc_running = False

    # ── Calculation Callbacks ─────────────────────────────────────────────────

    def day_ahead_routing(self, param_dict, simulation_time, time_step_number, esdl_id, energy_system):
        self._refresh_system_params(energy_system)
        self.current_day_step_idx = 0
        
        raw_limit = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "power_limit_plan_DA")
        raw_ci = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "carbon_intensity_plan_DA")
        raw_price = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "electricity_price_plan_DA")
        raw_demand = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "demand_power_plan_da")
        raw_pv = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "planned_generation_DA")

        LOGGER.info(f"[{simulation_time}] Dispatching day-ahead LP thread.")
        threading.Thread(
            target=self._run_day_ahead_lp,
            args=(simulation_time, esdl_id, raw_limit, raw_ci, raw_price, raw_demand, raw_pv),
            daemon=True
        ).start()
        
        return {}

    def network_dispatch(self, param_dict, simulation_time, time_step_number, esdl_id, energy_system):
        if self._pending_da_result is not None:
            goals, plan = self._pending_da_result
            self._pending_da_result = None
            self.change_layer.load_day_ahead_plan(goals, plan)
            self.current_day_step_idx = 0 # Ensure strict alignment when loading new plan

        try:
            return self._do_network_dispatch(param_dict, simulation_time, time_step_number, esdl_id, energy_system)
        except Exception as exc:
            LOGGER.error(f"network_dispatch CRASHED at t={simulation_time}, step={self.current_day_step_idx}: {exc}", exc_info=True)
            raise

    def _do_network_dispatch(self, param_dict, simulation_time, time_step_number, esdl_id, energy_system):
        # ── 1. READ inputs ──
        demand_w = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "demand_power_w")
        if demand_w is None: demand_w = 0.0

        soc_actual = self.current_soc
        soc_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "state_of_charge")
        if soc_val is not None:
            soc_actual = soc_val
            self.current_soc = soc_actual

        lim_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "actual_power_limit_ID")
        if lim_val is not None:
            self._state_cache["actual_power_limit_ID"] = lim_val

        min_mandate_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "mandated_min_power_draw_ID")
        if min_mandate_val is not None:
            # Honour the ESDL toggle: when mandate is disabled the dispatch
            # path treats the upstream signal as zero (no surplus-absorption
            # obligation, no PV curtailment forced by grid surplus).
            if getattr(self.sys_config, 'enable_mandate', True):
                self._state_cache["mandated_min_power_draw_ID"] = min_mandate_val
            else:
                self._state_cache["mandated_min_power_draw_ID"] = 0.0

        ci_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "actual_carbon_intensity_ID")
        if ci_val is not None:
            self._state_cache["actual_carbon_intensity_ID"] = ci_val

        price_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "actual_electricity_price_ID")
        if price_val is not None:
            self._state_cache["actual_electricity_price_ID"] = price_val

        pv_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "potential_available_generation_ID")
        pv_kw = (pv_val / 1000.0) if pv_val is not None else 0.0

        # Zero out PV if renewable service is disabled
        if not getattr(self.sys_config, 'enable_renewable_service', True):
            pv_kw = 0.0

        backup_supplied_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "backup_supplied_power")
        if backup_supplied_val is not None:
            self._state_cache["backup_supplied_power"] = backup_supplied_val
        backup_max_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "available_max_power")
        if backup_max_val is not None:
            self._state_cache["available_max_power"] = backup_max_val

        limit_w      = self._state_cache["actual_power_limit_ID"]
        ci_val       = self._state_cache["actual_carbon_intensity_ID"]
        backup_supplied_w = self._state_cache.get("backup_supplied_power", 0.0)
        backup_max_w     = self._state_cache.get("available_max_power", 0.0)
        
        grid_available = limit_w > 0.0
        p_dc_kw = demand_w / 1_000.0

        # ── 2. MAPE-K LAYER TRACKING & CONTROL ──
        setpoint_kw = 0.0
        planned_soc = soc_actual
        planned_bess_kw = 0.0
        forecast_p_dc_kw = p_dc_kw
        forecast_ci = ci_val
        forecast_grid_avail = grid_available
        
        event = None

        if self.change_layer.plan is not None:
            step = min(self.current_day_step_idx, len(self.change_layer.plan.p_ch_b) - 1)
            
            # Monitoring & Analysis
            state = self.change_layer.monitor(step, soc_actual, grid_available, p_dc_actual_kw=p_dc_kw)
            
            if getattr(self.sys_config, 'enable_change_management', True):
                event = self.change_layer.analyze(state, sys_config=self.sys_config)
            else:
                # Bypass change management — DeviationEvent already imported at top
                event = DeviationEvent(step, soc_actual, float(self.change_layer.plan.SOC_plan.iloc[step]), 0.0, False, 0.0, False, False)
            
            # Forecast extraction for logging
            planned_soc = float(self.change_layer.plan.SOC_plan.iloc[step])
            planned_bess_kw = float(self.change_layer.plan.p_ch_b.iloc[step])
            
            if self.change_layer.goals is not None:
                forecast_p_dc_kw = float(self.change_layer.goals.p_DC.iloc[step])
                forecast_ci = float(self.change_layer.goals.CI_grid.iloc[step])
                forecast_grid_avail = bool(self.change_layer.goals.grid_available.iloc[step])

            # Trigger MPC Replanning if needed. Runs in a daemon thread so the
            # dispatch step never blocks on the LP subprocess: PuLP-CBC's
            # `cbc.wait()` has no hard timeout enforcement (the `timeLimit` flag
            # is only honoured by CBC itself and is unreliable under CPU
            # pressure), so a synchronous call here can hang the federate and
            # produce the broker-deadlock signature seen in long simulations.
            # Side effect: the new plan becomes visible to the *next* dispatch
            # step rather than this one. The two-second worst-case delay is
            # acceptable on a 15-minute control cadence.
            if event.triggered_replan and not self._mpc_running:
                self._mpc_running = True
                LOGGER.info(f"[{simulation_time}] Dispatching MPC replan thread.")
                threading.Thread(
                    target=self._run_mpc_replan,
                    args=(event, simulation_time, esdl_id),
                    daemon=True
                ).start()
            elif event.triggered_replan and self._mpc_running:
                LOGGER.debug(f"[{simulation_time}] MPC replan suppressed (previous replan still running).")
            
            # Setpoint extraction
            raw_setpoint = self.change_layer.plan.p_ch_b.iloc[step]
            if raw_setpoint is None or (isinstance(raw_setpoint, float) and math.isnan(raw_setpoint)):
                LOGGER.warning("Plan setpoint is NaN/None at step %d, using heuristic", step)
                setpoint_kw = self._heuristic_fallback(soc_actual, limit_w, demand_w, pv_kw)
            else:
                setpoint_kw = float(raw_setpoint)
        else:
            # Heuristic fallback uses (+ discharge / - charge) convention
            setpoint_kw = self._heuristic_fallback(soc_actual, limit_w, demand_w, pv_kw)

        # ── 3. EXECUTION ──
        price_now = self._state_cache.get("actual_electricity_price_ID", 0.0)
        exec_state = self.control_layer.execute_step(
            setpoint_kw,
            soc_actual,
            grid_available,
            p_dc_kw,
            ci_val,
            self.sys_config,
            CI_battery_prev=self.current_ci_battery,
            Price_grid=price_now,
            Price_battery_prev=self.current_price_battery,
            p_PV=pv_kw,
            p_grid_limit_kw=(limit_w / 1000.0)
        )

        self.current_day_step_idx += 1
        self.current_ci_battery    = exec_state.CI_battery
        self.current_price_battery = exec_state.Price_battery

        # BESS Allocation: (+ discharge / - charge)
        bess_w = exec_state.p_ch_b * 1000.0
        grid_w = exec_state.p_grid * 1000.0
        unserved_kw = exec_state.unserved  # kW of DC load that could not be served
        
        # Force battery setpoint to zero if battery is disabled
        if not getattr(self.sys_config, 'enable_battery', True):
            bess_w = 0.0
        
        # Calculate served/unserved power and backup dispatch
        if getattr(self.sys_config, 'enable_backup_generator', True):
            # Backup generator covers any shortfall → no outage
            backup_w = unserved_kw * 1000.0  # Request backup to cover unserved
            served_power_w = demand_w
            unserved_outage_w = 0.0
        else:
            # No backup → unserved power IS the outage
            backup_w = 0.0
            unserved_outage_w = unserved_kw * 1000.0
            served_power_w = max(0.0, demand_w - unserved_outage_w)

        # ── 4. COMPREHENSIVE LOGGING ──
        
        # Real-time Execution States
        self.influx_connector.set_time_step_data_point(esdl_id, "Actual_SOC_from_Battery", simulation_time, soc_actual)
        self.influx_connector.set_time_step_data_point(esdl_id, "Setpoint_from_Layers_kW", simulation_time, setpoint_kw)
        self.influx_connector.set_time_step_data_point(esdl_id, "Grid_Available", simulation_time, 1.0 if grid_available else 0.0)
        self.influx_connector.set_time_step_data_point(esdl_id, "Carbon_Intensity", simulation_time, ci_val)
        self.influx_connector.set_time_step_data_point(esdl_id, "Electricity_Price_EUR_per_MWh", simulation_time, price_now)
        self.influx_connector.set_time_step_data_point(esdl_id, "Step_Cost_EUR", simulation_time, exec_state.cost)
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_Grid_W", simulation_time, grid_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_BESS_W", simulation_time, bess_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Backup_Requested_Power_W", simulation_time, backup_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Total_Routed_Demand_W", simulation_time, demand_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "PV_Generation_W", simulation_time, pv_kw * 1000.0)
        self.influx_connector.set_time_step_data_point(esdl_id, "served_datacenter_power_w", simulation_time, served_power_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Unserved_Datacenter_Power_W", simulation_time, unserved_outage_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Backup_Supplied_Power_W", simulation_time, backup_supplied_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Backup_Max_Capacity_W", simulation_time, backup_max_w)
        
        # Carbon + Price Metrics (per-step)
        self.influx_connector.set_time_step_data_point(esdl_id, "Total_Carbon_g", simulation_time, exec_state.carbon)
        self.influx_connector.set_time_step_data_point(esdl_id, "CI_DC_Consumption_gCO2_kWh", simulation_time, exec_state.CI_DC_consumption)
        self.influx_connector.set_time_step_data_point(esdl_id, "CI_Battery_gCO2_kWh", simulation_time, exec_state.CI_battery)
        self.influx_connector.set_time_step_data_point(esdl_id, "Price_DC_Consumption_EUR_MWh", simulation_time, exec_state.Price_DC_consumption)
        self.influx_connector.set_time_step_data_point(esdl_id, "Price_Battery_EUR_MWh", simulation_time, exec_state.Price_battery)

        # Cumulative running totals (each measurement = sum from t=0 up to this step)
        step_unserved_kwh = exec_state.unserved * self.sys_config.dt
        self._cum_carbon_g        += exec_state.carbon
        self._cum_cost_eur        += exec_state.cost
        self._cum_unserved_kwh    += step_unserved_kwh
        self._cum_grid_energy_kwh += exec_state.p_grid * self.sys_config.dt

        self.influx_connector.set_time_step_data_point(esdl_id, "Cumulative_Carbon_g", simulation_time, self._cum_carbon_g)
        self.influx_connector.set_time_step_data_point(esdl_id, "Cumulative_Cost_EUR", simulation_time, self._cum_cost_eur)
        self.influx_connector.set_time_step_data_point(esdl_id, "Cumulative_Unserved_kWh", simulation_time, self._cum_unserved_kwh)
        self.influx_connector.set_time_step_data_point(esdl_id, "Cumulative_Grid_Energy_kWh", simulation_time, self._cum_grid_energy_kwh)

        # Counterfactual baseline: spot price × DC load (no BESS / no PV optimisation)
        step_baseline_cost_eur = price_now * (p_dc_kw * self.sys_config.dt) / 1000.0
        step_baseline_carbon_g = ci_val * (p_dc_kw * self.sys_config.dt)
        self._cum_baseline_cost_eur += step_baseline_cost_eur
        self._cum_baseline_carbon_g += step_baseline_carbon_g
        self.influx_connector.set_time_step_data_point(esdl_id, "Baseline_Step_Cost_EUR", simulation_time, step_baseline_cost_eur)
        self.influx_connector.set_time_step_data_point(esdl_id, "Baseline_Step_Carbon_g", simulation_time, step_baseline_carbon_g)
        self.influx_connector.set_time_step_data_point(esdl_id, "Cumulative_Baseline_Cost_EUR", simulation_time, self._cum_baseline_cost_eur)
        self.influx_connector.set_time_step_data_point(esdl_id, "Cumulative_Baseline_Carbon_g", simulation_time, self._cum_baseline_carbon_g)

        # Plan Comparisons (Current Step)
        self.influx_connector.set_time_step_data_point(esdl_id, "Planned_SOC", simulation_time, planned_soc)
        self.influx_connector.set_time_step_data_point(esdl_id, "Planned_BESS_Power_W", simulation_time, planned_bess_kw * 1000.0)
        self.influx_connector.set_time_step_data_point(esdl_id, "Forecasted_Grid_CI", simulation_time, forecast_ci)
        self.influx_connector.set_time_step_data_point(esdl_id, "Forecasted_Grid_Available", simulation_time, 1.0 if forecast_grid_avail else 0.0)
        self.influx_connector.set_time_step_data_point(esdl_id, "Forecasted_DC_Demand_W", simulation_time, forecast_p_dc_kw * 1000.0)

        # Diagnostic Flags & Deviations
        if event:
            self.influx_connector.set_time_step_data_point(esdl_id, "SOC_Drift_pct", simulation_time, event.soc_drift)
            self.influx_connector.set_time_step_data_point(esdl_id, "Demand_Delta_W", simulation_time, event.demand_delta * 1000.0)
            self.influx_connector.set_time_step_data_point(esdl_id, "Unplanned_Outage_Flag", simulation_time, 1.0 if event.unplanned_outage else 0.0)
            self.influx_connector.set_time_step_data_point(esdl_id, "Demand_Spike_Flag", simulation_time, 1.0 if event.demand_spike else 0.0)
            self.influx_connector.set_time_step_data_point(esdl_id, "Triggered_Replan_Flag", simulation_time, 1.0 if event.triggered_replan else 0.0)
        
        self.influx_connector.set_time_step_data_point(esdl_id, "MPC_Running_Flag", simulation_time, 1.0 if self._mpc_running else 0.0)
        self.influx_connector.set_time_step_data_point(esdl_id, "Alarm_Active_Flag", simulation_time, 1.0 if exec_state.alarm else 0.0)

        # ── 5. MANDATE-AWARE PV CURTAILMENT ──
        # mandatory_grid_import_w > 0 means the grid has surplus upstream generation
        # that the microgrid MUST absorb. PV must be curtailed so it doesn't
        # displace the mandatory grid import.
        mandate_w = self._state_cache["mandated_min_power_draw_ID"]
        mandatory_grid_import_w = max(0.0, mandate_w)  # only positive = surplus case

        # Sentinel "no curtailment" value. Must be:
        #   * larger than any physical PV inverter capacity (so it never binds in min()),
        #   * a finite float64 (so it survives HELICS publish + InfluxDB line-protocol
        #     serialisation; IEEE-754 inf is a valid double but is rejected by InfluxDB
        #     line protocol and propagates as NaN through downstream arithmetic).
        # 1 GW is six orders of magnitude above the 1 MW PV array used in this scenario.
        PV_CURTAIL_UNCAPPED = 1.0e9

        if mandatory_grid_import_w > 0.0:
            # PV can only supply what remains after the mandatory grid import covers DC load.
            # This forces the grid's surplus into the DC + battery.
            pv_curtailment_limit_w = max(0.0, demand_w - mandatory_grid_import_w)
            LOGGER.info(
                "[Mandate] Grid surplus %.1f W — curtailing PV to %.1f W (demand=%.1f W)",
                mandatory_grid_import_w, pv_curtailment_limit_w, demand_w
            )
        else:
            # No surplus — PV may generate freely up to physical inverter capacity.
            pv_curtailment_limit_w = PV_CURTAIL_UNCAPPED

        self.influx_connector.set_time_step_data_point(esdl_id, "Mandatory_Grid_Import_W", simulation_time, mandatory_grid_import_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "PV_Curtailment_Limit_W", simulation_time, pv_curtailment_limit_w)

        # ── Periodic InfluxDB flush ──
        # The dots_infrastructure connector buffers points in memory and auto-flushes
        # at MAX_AMOUNT_OF_DB_POINTS = 100 000. With ~30 fields per dispatch step this
        # accumulates over roughly 35 simulated days, producing a single 100k-point HTTP
        # POST that can exceed the 5 s requests timeout and stall the federate. Force a
        # daily flush keeps each batch under ~3 000 points (sub-second writes).
        self._steps_since_influx_flush += 1
        if self._steps_since_influx_flush >= self._influx_flush_every_n_steps:
            try:
                if self.influx_connector.data_points:
                    LOGGER.debug(
                        "[Influx] Periodic flush: %d points",
                        len(self.influx_connector.data_points),
                    )
                    self.influx_connector.write_output()
                    self.influx_connector.data_points.clear()
            except Exception as exc:
                LOGGER.warning("[Influx] Periodic flush failed: %s — buffer retained", exc)
            self._steps_since_influx_flush = 0

        return NetworkDispatchOutput(
            bess_allocation_w=bess_w,
            grid_allocation_w=grid_w,
            current_max_power_limit=pv_curtailment_limit_w,
            backup_requested_power=backup_w,
            served_datacenter_power_w=served_power_w,
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _refresh_system_params(self, energy_system):
        """Extract all relevant parameters from ESDL assets."""
        if not hasattr(self, 'sys_config'):
            self.sys_config = SystemConfig()

        for obj in energy_system.eAllContents():
            eClass = getattr(obj, "eClass", None)
            if not eClass: continue
            
            if eClass.name == "Battery":
                # FIX: Convert Wh to kWh for optimization layers
                self.sys_config.E_BAT = float(getattr(obj, "capacity", 0.0)) / 1000.0
                self.sys_config.P_CH_MAX = float(getattr(obj, "maxChargeRate", 0.0)) / 1000.0
                self.sys_config.P_DCH_MAX = float(getattr(obj, "maxDischargeRate", 0.0)) / 1000.0
                self.sys_config.EFF_CH = float(getattr(obj, "chargeEfficiency", 0.95))
                self.sys_config.EFF_DCH = float(getattr(obj, "dischargeEfficiency", 0.95))

            elif eClass.name == "PowerPlant":
                self.grid_import_limit_w = float(getattr(obj, "power", 4000000.0))

            elif eClass.name == "ElectricityNetwork":
                if hasattr(obj, "KPIs") and obj.KPIs is not None:
                    for kpi in obj.KPIs.kpi:
                        if hasattr(kpi, "name") and hasattr(kpi, "value"):
                            if kpi.name == "w_unserved":
                                self.sys_config.w_unserved = float(kpi.value)
                            elif kpi.name == "w_carbon":
                                self.sys_config.w_carbon = float(kpi.value)
                            elif kpi.name == "w_price":
                                self.sys_config.w_price = float(kpi.value)
                            elif kpi.name == "w_effort":
                                self.sys_config.w_effort = float(kpi.value)
                            elif kpi.name == "w_soc_low":
                                self.sys_config.w_soc_low = float(kpi.value)
                            elif kpi.name == "soc_baseline":
                                self.sys_config.soc_baseline = float(kpi.value)

                            elif kpi.name == "mpc_soc_drift_threshold":
                                self.sys_config.mpc_soc_drift_threshold = float(kpi.value)
                            elif kpi.name == "mpc_demand_spike_threshold":
                                self.sys_config.mpc_demand_spike_threshold = float(kpi.value)
                            elif kpi.name == "mpc_horizon_steps":
                                self.sys_config.mpc_horizon_steps = int(float(kpi.value))
                            elif kpi.name == "mpc_replan_cooldown":
                                self.sys_config.mpc_replan_cooldown = int(float(kpi.value))

                            elif kpi.name == "forecast_sigma_ci":
                                self.sys_config.forecast_sigma_ci = float(kpi.value)
                            elif kpi.name == "forecast_sigma_p_dc":
                                self.sys_config.forecast_sigma_p_dc = float(kpi.value)
                            elif kpi.name == "forecast_sigma_price":
                                self.sys_config.forecast_sigma_price = float(kpi.value)
                            elif kpi.name == "forecast_seed":
                                self.sys_config.forecast_seed = int(float(kpi.value))

                            elif kpi.name == "enable_battery":
                                self.sys_config.enable_battery = bool(float(kpi.value))
                            elif kpi.name == "enable_backup_generator":
                                self.sys_config.enable_backup_generator = bool(float(kpi.value))
                            elif kpi.name == "enable_renewable_service":
                                self.sys_config.enable_renewable_service = bool(float(kpi.value))
                            elif kpi.name == "enable_change_management":
                                self.sys_config.enable_change_management = bool(float(kpi.value))
                            elif kpi.name == "enable_goal_management":
                                self.sys_config.enable_goal_management = bool(float(kpi.value))
                            elif kpi.name == "enable_mandate":
                                self.sys_config.enable_mandate = bool(float(kpi.value))

            elif eClass.name == "ElectricityDemand":
                self.dc_base_load_w = float(getattr(obj, "power", 4000000.0))

    def _parse_vector(self, raw_list, default_kw, divide_by_1000=True):
        if not raw_list or not isinstance(raw_list, list) or len(raw_list) == 0:
            return [default_kw] * 96
        
        factor = 1000.0 if divide_by_1000 else 1.0
        return [float(x) / factor for x in raw_list]

    def _heuristic_fallback(self, soc, limit_w, demand_w, pv_kw):
        # Convention: + discharge / - charge
        net_demand_kw = max(0.0, demand_w / 1000.0 - pv_kw)
        limit_kw = limit_w / 1000.0
        
        if limit_kw > net_demand_kw and soc < 95.0:
            # Grid surplus: charge battery (negative)
            return -min(self.sys_config.P_CH_MAX, limit_kw - net_demand_kw)
        elif limit_kw < net_demand_kw and soc > 5.0:
            # Grid deficit: discharge battery (positive)
            return min(self.sys_config.P_DCH_MAX, net_demand_kw - limit_kw)
        return 0.0

if __name__ == "__main__":
    executor = Networkbalancerservice()
    try:
        executor.start_simulation()
    except Exception as e:
        LOGGER.error(f"Crashed: {e}")
        raise
    finally:
        executor.stop_simulation()

