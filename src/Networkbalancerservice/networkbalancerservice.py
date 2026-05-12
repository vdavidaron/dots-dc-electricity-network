from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List
import threading
import logging
import json
import math
import time

from esdl import esdl, EnergySystem
import helics as h

from dots_infrastructure.DataClasses import (
    EsdlId, TimeStepInformation, HelicsCalculationInformation, 
    SubscriptionDescription, PublicationDescription
)
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
        from dots_infrastructure.influxdb_connector import InfluxDBConnector
        self.simulator_configuration = CalculationServiceHelperFunctions.get_simulator_configuration_from_environment()
        self.calculations = []
        self.energy_system = None
        self.influx_connector = InfluxDBConnector(
            self.simulator_configuration.influx_host, 
            self.simulator_configuration.influx_port, 
            self.simulator_configuration.influx_username, 
            self.simulator_configuration.influx_password, 
            self.simulator_configuration.influx_database_name
        )
        self.esdl_obj_mapping = {}
        self.current_soc = 50.0

        # ── Forecast error model ───────────────────────────────
        self._forecast_error = ForecastErrorModel()

        # ── Custom Calculation Setup ─────────────────────────────
        da_inputs = [
            SubscriptionDescription(esdl_type="PowerPlant", input_name="power_limit_plan_DA", input_unit="VECTOR", input_type=h.HelicsDataType.VECTOR),
            SubscriptionDescription(esdl_type="PowerPlant", input_name="carbon_intensity_plan_DA", input_unit="VECTOR", input_type=h.HelicsDataType.VECTOR),
            SubscriptionDescription(esdl_type="ElectricityDemand", input_name="demand_power_plan_da", input_unit="VECTOR", input_type=h.HelicsDataType.VECTOR),
            SubscriptionDescription(esdl_type="PVInstallation", input_name="planned_generation_DA", input_unit="VECTOR", input_type=h.HelicsDataType.VECTOR),
        ]
        da_info = HelicsCalculationInformation(
            time_period_in_seconds=86400,
            offset=0,
            uninterruptible=False,
            wait_for_current_time_update=False,
            terminate_on_error=True,
            calculation_name="day_ahead_routing",
            inputs=da_inputs,
            outputs=[],
            calculation_function=self.day_ahead_routing
        )
        self.add_calculation(da_info)

        dispatch_inputs = [
            SubscriptionDescription(esdl_type="ElectricityDemand", input_name="demand_power_w", input_unit="W", input_type=h.HelicsDataType.DOUBLE),
            SubscriptionDescription(esdl_type="Battery", input_name="state_of_charge", input_unit="pct", input_type=h.HelicsDataType.DOUBLE),
            SubscriptionDescription(esdl_type="Battery", input_name="bess_power_w", input_unit="W", input_type=h.HelicsDataType.DOUBLE),
            SubscriptionDescription(esdl_type="PowerPlant", input_name="actual_power_limit_ID", input_unit="W", input_type=h.HelicsDataType.DOUBLE),
            SubscriptionDescription(esdl_type="PowerPlant", input_name="actual_carbon_intensity_ID", input_unit="gCO2/kWh", input_type=h.HelicsDataType.DOUBLE),
            SubscriptionDescription(esdl_type="PVInstallation", input_name="potential_available_generation_ID", input_unit="W", input_type=h.HelicsDataType.DOUBLE),
        ]
        
        dispatch_outputs = [
            PublicationDescription(global_flag=True, esdl_type="ElectricityNetwork", output_name="bess_allocation_w", output_unit="W", data_type=h.HelicsDataType.DOUBLE),
            PublicationDescription(global_flag=True, esdl_type="ElectricityNetwork", output_name="grid_allocation_w", output_unit="W", data_type=h.HelicsDataType.DOUBLE),
            PublicationDescription(global_flag=True, esdl_type="ElectricityNetwork", output_name="current_max_power_limit", output_unit="W", data_type=h.HelicsDataType.DOUBLE),
            PublicationDescription(global_flag=True, esdl_type="ElectricityNetwork", output_name="backup_requested_power", output_unit="W", data_type=h.HelicsDataType.DOUBLE),
        ]
        dispatch_info = HelicsCalculationInformation(
            time_period_in_seconds=900,
            offset=0,
            uninterruptible=False,
            wait_for_current_time_update=False,
            terminate_on_error=True,
            calculation_name="network_dispatch",
            inputs=dispatch_inputs,
            outputs=dispatch_outputs,
            calculation_function=self.network_dispatch
        )
        self.add_calculation(dispatch_info)

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

        self.sys_config = SystemConfig(
            dt=0.25, 
            E_BAT=self.sys_config.E_BAT if hasattr(self, 'sys_config') else 0.0, 
            P_CH_MAX=self.sys_config.P_CH_MAX if hasattr(self, 'sys_config') else 0.0, 
            P_DCH_MAX=self.sys_config.P_DCH_MAX if hasattr(self, 'sys_config') else 0.0, 
            P_GRID_MAX=self.grid_import_limit_w / 1000.0,
            SOC_MIN=0.0,
            SOC_MAX=100.0
        )

        LOGGER.info(
            "SystemConfig: dt=%.2f  E_BAT=%.1f kWh  P_CH=%.1f kW  P_DCH=%.1f kW  "
            "P_GRID=%.1f kW  SOC=[%.0f,%.0f]%%  EFF_CH=%.2f  EFF_DCH=%.2f",
            self.sys_config.dt, self.sys_config.E_BAT,
            self.sys_config.P_CH_MAX, self.sys_config.P_DCH_MAX,
            self.sys_config.P_GRID_MAX,
            self.sys_config.SOC_MIN, self.sys_config.SOC_MAX,
            self.sys_config.EFF_CH, self.sys_config.EFF_DCH,
        )

        # Layers
        self.goal_layer    = GoalManagementLayer(scenario="nonfirm")
        self.change_layer  = ChangeManagementLayer()
        self.control_layer = ComponentControlLayer()

        self.current_day_step_idx = 0
        self._pending_da_result: Optional[Tuple[Goals, SchedulePlan]] = None
        self._mpc_running  = False
        self.current_ci_battery = 250.0  # [gCO2/kWh] Initial assumption
        
        self._state_cache = {
            "actual_power_limit_ID": self.grid_import_limit_w,
            "actual_carbon_intensity_ID": 250.0,
            "available_max_power": 0.0
        }

    # ── Background thread helpers ─────────────────────────────────────────────

    def _run_day_ahead_lp(self, simulation_time: datetime, esdl_id: str, raw_limit: list[float], raw_ci: list[float], raw_demand: list[float], raw_pv: list[float]) -> None:
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

            forecast = {
                "CI_grid":        self._forecast_error.perturb("CI_grid",  pd.Series(ci_actual)),
                "grid_available": pd.Series([(v > 0) for v in grid_limits_kw]),
                "p_DC":           self._forecast_error.perturb("p_DC",     pd.Series(dc_demand_actual)),
                "p_PV":           pd.Series(pv_actual),
            }

            LOGGER.info(
                "[DA Forecast] t=%s  n=%d steps  CI_mean=%.0f gCO2/kWh  p_DC_mean=%.0f kW  p_PV_mean=%.0f kW",
                simulation_time.isoformat(), n,
                float(forecast["CI_grid"].mean()),
                float(forecast["p_DC"].mean()),
                float(forecast["p_PV"].mean())
            )

            goals, plan = self.goal_layer.execute(forecast, self.sys_config, soc_init=self.current_soc)
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
        raw_demand = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "demand_power_plan_da")
        raw_pv = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "planned_generation_DA")
        
        LOGGER.info(f"[{simulation_time}] Dispatching day-ahead LP thread.")
        threading.Thread(
            target=self._run_day_ahead_lp,
            args=(simulation_time, esdl_id, raw_limit, raw_ci, raw_demand, raw_pv),
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

        ci_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "actual_carbon_intensity_ID")
        if ci_val is not None:
            self._state_cache["actual_carbon_intensity_ID"] = ci_val

        pv_val = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "potential_available_generation_ID")
        pv_kw = (pv_val / 1000.0) if pv_val is not None else 0.0

        limit_w      = self._state_cache["actual_power_limit_ID"]
        ci_val       = self._state_cache["actual_carbon_intensity_ID"]
        
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
            event = self.change_layer.analyze(state)
            
            # Forecast extraction for logging
            planned_soc = float(self.change_layer.plan.SOC_plan.iloc[step])
            planned_bess_kw = float(self.change_layer.plan.p_ch_b.iloc[step])
            
            if self.change_layer.goals is not None:
                forecast_p_dc_kw = float(self.change_layer.goals.p_DC.iloc[step])
                forecast_ci = float(self.change_layer.goals.CI_grid.iloc[step])
                forecast_grid_avail = bool(self.change_layer.goals.grid_available.iloc[step])

            # Trigger MPC Replanning if needed
            if event.triggered_replan:
                LOGGER.info(f"[{simulation_time}] Running MPC replan synchronously.")
                self._run_mpc_replan(event, simulation_time, esdl_id)
            
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
        exec_state = self.control_layer.execute_step(setpoint_kw, soc_actual, grid_available, p_dc_kw, ci_val, self.sys_config, CI_battery_prev=self.current_ci_battery, p_PV=pv_kw)
        
        self.current_day_step_idx += 1
        self.current_ci_battery = exec_state.CI_battery

        # BESS Allocation: (+ discharge / - charge)
        bess_w = exec_state.p_ch_b * 1000.0
        grid_w = exec_state.p_grid * 1000.0
        backup_w = exec_state.unserved * 1000.0

        # ── 4. COMPREHENSIVE LOGGING ──
        
        # Real-time Execution States
        self.influx_connector.set_time_step_data_point(esdl_id, "Actual_SOC_from_Battery", simulation_time, soc_actual)
        self.influx_connector.set_time_step_data_point(esdl_id, "Setpoint_from_Layers_kW", simulation_time, setpoint_kw)
        self.influx_connector.set_time_step_data_point(esdl_id, "Grid_Available", simulation_time, 1.0 if grid_available else 0.0)
        self.influx_connector.set_time_step_data_point(esdl_id, "Carbon_Intensity", simulation_time, ci_val)
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_Grid_W", simulation_time, grid_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_BESS_W", simulation_time, bess_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Backup_Requested_Power_W", simulation_time, backup_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Total_Routed_Demand_W", simulation_time, demand_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "PV_Generation_W", simulation_time, pv_kw * 1000.0)
        
        # Carbon Metrics
        self.influx_connector.set_time_step_data_point(esdl_id, "Total_Carbon_g", simulation_time, exec_state.carbon)
        self.influx_connector.set_time_step_data_point(esdl_id, "CI_DC_Consumption_gCO2_kWh", simulation_time, exec_state.CI_DC_consumption)
        self.influx_connector.set_time_step_data_point(esdl_id, "CI_Battery_gCO2_kWh", simulation_time, exec_state.CI_battery)

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

        return NetworkDispatchOutput(
            bess_allocation_w=bess_w,
            grid_allocation_w=grid_w,
            current_max_power_limit=limit_w,
            backup_requested_power=backup_w,
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

