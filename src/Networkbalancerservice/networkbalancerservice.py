from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List
import threading
import logging
import json
import math

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
        self._shadow_subs = {}  # HELICS inputs registered outside framework tracking


        # ── Forecast error model (calibrated Gaussian perturbation) ───────────
        self._forecast_error = ForecastErrorModel()

        # ── Custom Calculation Setup ──────────────────────────────────────────
        da_inputs = [
            SubscriptionDescription(esdl_type="PowerPlant", input_name="power_limit_plan_DA", input_unit="JSON", input_type=h.HelicsDataType.STRING),
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
            # ONLY demand_power_w as formal input — Battery/PowerPlant are read
            # via shadow subscriptions to avoid circular-dependency deadlock
            # (Battery waits for bess_allocation_w from us, we'd wait for state_of_charge).
            SubscriptionDescription(esdl_type="ElectricityDemand", input_name="demand_power_w", input_unit="W", input_type=h.HelicsDataType.DOUBLE),
        ]
        # Shadow subscription keys — registered during init, read non-blockingly
        self._shadow_sub_keys = {
            "soc":    ("Battery",     "state_of_charge",         "pct"),
            "bess":   ("Battery",     "bess_power_w",            "W"),
            "limit":  ("PowerPlant",  "actual_power_limit_ID",   "W"),
            "ci":     ("PowerPlant",  "actual_carbon_intensity_ID", "gCO2/kWh"),
        }
        dispatch_outputs = [
            PublicationDescription(global_flag=True, esdl_type="ElectricityNetwork", output_name="bess_allocation_w", output_unit="W", data_type=h.HelicsDataType.DOUBLE),
            PublicationDescription(global_flag=True, esdl_type="ElectricityNetwork", output_name="grid_allocation_w", output_unit="W", data_type=h.HelicsDataType.DOUBLE),
            PublicationDescription(global_flag=True, esdl_type="ElectricityNetwork", output_name="current_max_power_limit", output_unit="W", data_type=h.HelicsDataType.DOUBLE),
            PublicationDescription(global_flag=True, esdl_type="ElectricityNetwork", output_name="backup_requested_power", output_unit="W", data_type=h.HelicsDataType.DOUBLE),
        ]
        dispatch_info = HelicsCalculationInformation(
            time_period_in_seconds=900,
            offset=10,
            uninterruptible=False,
            wait_for_current_time_update=False,
            terminate_on_error=True,
            calculation_name="network_dispatch",
            inputs=dispatch_inputs,
            outputs=dispatch_outputs,
            calculation_function=self.network_dispatch
        )
        self.add_calculation(dispatch_info)

    # ── Shadow subscription registration ──────────────────────────────────────

    def start_simulation(self):
        """Override to inject shadow HELICS subscriptions before execution mode."""
        self._assert_that_periods_of_calculation_are_smaller_than_simulation_duration()
        esdl_helper = self.init_simulation()

        from concurrent.futures import ThreadPoolExecutor
        self.exe = ThreadPoolExecutor(len(self.calculations))
        for calc_executor in self.calculations:
            calc_name = calc_executor.helics_value_federate_info.calculation_name
            if calc_name == "network_dispatch":
                # Wrap to inject shadow subs between init and exec mode
                self.exe.submit(self._init_dispatch_with_shadow_subs, calc_executor, esdl_helper)
            else:
                self.exe.submit(calc_executor.initialize_and_start_federate, esdl_helper)

    def _init_dispatch_with_shadow_subs(self, calc_executor, esdl_helper):
        """init_federate → register shadow subs → start loop."""
        calc_executor.init_federate(esdl_helper)
        fed = calc_executor.value_federate

        # Resolve connected Battery/PowerPlant asset IDs from ESDL
        for esdl_id in self.simulator_configuration.esdl_ids:
            network = self.esdl_obj_mapping.get(esdl_id)
            if not network:
                continue
            for port in network.port:
                for cp in port.connectedTo:
                    asset = cp.eContainer()
                    atype = type(asset).__name__
                    for alias, (esdl_type, name, unit) in self._shadow_sub_keys.items():
                        if atype == esdl_type and alias not in self._shadow_subs:
                            key = f"{esdl_type}/{name}/{asset.id}"
                            sub = h.helicsFederateRegisterSubscription(fed, key, unit)
                            self._shadow_subs[alias] = sub
                            LOGGER.info(f"Shadow subscription registered: {key}")

        calc_executor.energy_system = esdl_helper.energy_system
        calc_executor.start_value_federate()

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
        self.goal_layer    = GoalManagementLayer(scenario="carbon")
        self.change_layer  = ChangeManagementLayer()
        self.control_layer = ComponentControlLayer()

        self.current_day_step_idx = 0
        self._pending_da_result: Optional[Tuple[Goals, SchedulePlan]] = None
        self._mpc_running  = False
        
        self._state_cache = {
            "actual_power_limit_ID": self.grid_import_limit_w,
            "actual_carbon_intensity_ID": 250.0,
            "available_max_power": 0.0
        }

    # ── Background thread helpers ─────────────────────────────────────────────

    def _run_day_ahead_lp(self, forecast: dict, soc_init: float) -> None:
        try:
            goals, plan = self.goal_layer.execute(forecast, self.sys_config, soc_init=soc_init)
            self._pending_da_result = (goals, plan)
            LOGGER.info(f"[DA thread] Plan generated.")
        except Exception as exc:
            LOGGER.error(f"[DA thread] Failed: {exc}")

    def _run_mpc_replan(self, event: DeviationEvent) -> None:
        try:
            self.change_layer.replan_mpc(event, self.sys_config)
        except Exception as exc:
            LOGGER.error(f"[MPC thread] Failed: {exc}")
        finally:
            self._mpc_running = False

    # ── Calculation Callbacks ─────────────────────────────────────────────────


    def day_ahead_routing(self, param_dict, simulation_time, time_step_number, esdl_id, energy_system):
        self._refresh_system_params(energy_system)

        raw_limit = None
        for k, v in param_dict.items():
            if "power_limit_plan_DA" in k:
                raw_limit = v; break

        grid_limits_kw = self._parse_json_list(raw_limit, self.grid_import_limit_w / 1000.0)
        n = len(grid_limits_kw)

        raw_ci = None
        for k, v in param_dict.items():
            if "carbon_intensity_plan_DA" in k:
                raw_ci = v; break

        if raw_ci:
            ci_actual = self._parse_json_list(raw_ci, 250.0)
        else:
            month = simulation_time.month
            seasonal_offset = 40.0 * np.cos(2 * np.pi * (month - 1) / 12)
            ci_actual = [
                float(np.clip(
                    250.0 + seasonal_offset
                    - 60.0 * np.exp(-((i - 52) ** 2) / 40.0),   # midday solar dip
                    50.0, 600.0
                ))
                for i in range(n)
            ]

        dc_base_kw = self.dc_base_load_w / 1000.0
        hour_texture = [
            dc_base_kw * (1.0 + 0.03 * np.sin(2 * np.pi * i / n))
            for i in range(n)
        ]

        forecast = {
            "CI_grid":        self._forecast_error.perturb("CI_grid",  pd.Series(ci_actual)),
            "grid_available": pd.Series([(v > 0) for v in grid_limits_kw]),
            "p_DC":           self._forecast_error.perturb("p_DC",     pd.Series(hour_texture)),
        }

        LOGGER.info(
            "[DA Forecast] t=%s  n=%d steps  "
            "CI_mean=%.0f→%.0f gCO2/kWh  "
            "p_DC_mean=%.0f→%.0f kW",
            simulation_time.isoformat(), n,
            float(pd.Series(ci_actual).mean()),    float(forecast["CI_grid"].mean()),
            float(pd.Series(hour_texture).mean()), float(forecast["p_DC"].mean()),
        )

        self.current_day_step_idx = 0
        threading.Thread(target=self._run_day_ahead_lp, args=(forecast, self.current_soc), daemon=True).start()
        return {}

    def network_dispatch(self, param_dict, simulation_time, time_step_number, esdl_id, energy_system):
        if self._pending_da_result:
            goals, plan = self._pending_da_result
            self._pending_da_result = None
            self.change_layer.load_day_ahead_plan(goals, plan)

        try:
            return self._do_network_dispatch(param_dict, simulation_time, time_step_number, esdl_id, energy_system)
        except Exception as exc:
            LOGGER.error(f"network_dispatch CRASHED at t={simulation_time}, step={self.current_day_step_idx}: {exc}", exc_info=True)
            raise

    def _read_shadow_sub(self, alias: str):
        """Non-blocking read of a shadow HELICS subscription. Returns value or None."""
        sub = self._shadow_subs.get(alias)
        if sub is None:
            return None
        try:
            val = h.helicsInputGetDouble(sub)
            return val if not math.isnan(val) else None
        except Exception:
            return None

    def _do_network_dispatch(self, param_dict, simulation_time, time_step_number, esdl_id, energy_system):
        # ── 1. READ inputs ──
        # demand_power_w: formal framework input (blocking wait handled by framework)
        demand_w = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "demand_power_w")
        if demand_w is None: demand_w = 0.0

        # Battery SOC: shadow subscription (non-blocking)
        soc_actual = self.current_soc
        soc_val = self._read_shadow_sub("soc")
        if soc_val is not None:
            soc_actual = soc_val
            self.current_soc = soc_actual

        # PowerPlant grid limit: shadow subscription (non-blocking)
        lim_val = self._read_shadow_sub("limit")
        if lim_val is not None:
            self._state_cache["actual_power_limit_ID"] = lim_val

        # PowerPlant carbon intensity: shadow subscription (non-blocking)
        ci_val = self._read_shadow_sub("ci")
        if ci_val is not None:
            self._state_cache["actual_carbon_intensity_ID"] = ci_val

        limit_w      = self._state_cache["actual_power_limit_ID"]
        ci_val       = self._state_cache["actual_carbon_intensity_ID"]
        
        grid_available = limit_w > 0.0
        p_dc_kw = demand_w / 1_000.0

        setpoint_kw = 0.0
        if self.change_layer.plan is not None:
            step = min(self.current_day_step_idx, len(self.change_layer.plan.p_ch_b) - 1)
            state = self.change_layer.monitor(step, soc_actual, grid_available, p_dc_actual_kw=p_dc_kw)
            event = self.change_layer.analyze(state)
            if event.triggered_replan and not self._mpc_running:
                self._mpc_running = True
                threading.Thread(target=self._run_mpc_replan, args=(event,), daemon=True).start()
            raw_setpoint = self.change_layer.plan.p_ch_b.iloc[step]
            if raw_setpoint is None or (isinstance(raw_setpoint, float) and math.isnan(raw_setpoint)):
                LOGGER.warning("Plan setpoint is NaN/None at step %d, using heuristic", step)
                setpoint_kw = self._heuristic_fallback(soc_actual, limit_w, demand_w)
            else:
                setpoint_kw = float(raw_setpoint)
        else:
            # Heuristic fallback uses (+ discharge / - charge) convention
            setpoint_kw = self._heuristic_fallback(soc_actual, limit_w, demand_w)

        exec_state = self.control_layer.execute_step(setpoint_kw, soc_actual, grid_available, p_dc_kw, ci_val, self.sys_config)
        
        self.current_day_step_idx += 1

        # BESS Allocation: (+ discharge / - charge)
        bess_w = exec_state.p_ch_b * 1000.0
        grid_w = exec_state.p_grid * 1000.0
        backup_w = exec_state.unserved * 1000.0

        # Detailed Logging
        self.influx_connector.set_time_step_data_point(esdl_id, "Actual_SOC_from_Battery", simulation_time, soc_actual)
        self.influx_connector.set_time_step_data_point(esdl_id, "Setpoint_from_Layers_kW", simulation_time, setpoint_kw)
        self.influx_connector.set_time_step_data_point(esdl_id, "Grid_Available", simulation_time, 1.0 if grid_available else 0.0)
        self.influx_connector.set_time_step_data_point(esdl_id, "Carbon_Intensity", simulation_time, ci_val)

        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_Grid_W", simulation_time, grid_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_BESS_W", simulation_time, bess_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Backup_Requested_Power_W", simulation_time, backup_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Total_Routed_Demand_W", simulation_time, demand_w)
        
        if self.change_layer.goals is not None:
            step_idx = max(0, self.current_day_step_idx - 1)
            da_step  = min(step_idx, len(self.change_layer.goals.p_DC) - 1)
            forecast_w = float(self.change_layer.goals.p_DC.iloc[da_step]) * 1_000.0
            self.influx_connector.set_time_step_data_point(esdl_id, "Demand_Delta_W", simulation_time, demand_w - forecast_w)

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

    def _parse_json_list(self, raw, default_kw):
        if not raw: return [default_kw] * 96
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [float(x.get("value", x))/1000.0 if isinstance(x, dict) else float(x)/1000.0 for x in data]
            return [float(data)/1000.0] * 96
        except: return [default_kw] * 96

    def _heuristic_fallback(self, soc, limit_w, demand_w):
        # Convention: + discharge / - charge
        if limit_w > demand_w and soc < 95.0:
            # Grid surplus: charge battery (negative)
            return -min(self.sys_config.P_CH_MAX, (limit_w - demand_w)/1000.0)
        elif limit_w < demand_w and soc > 5.0:
            # Grid deficit: discharge battery (positive)
            return min(self.sys_config.P_DCH_MAX, (demand_w - limit_w)/1000.0)
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
