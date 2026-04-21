from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List
import threading
import logging
import json

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

        # ── Forecast error model (calibrated Gaussian perturbation) ───────────
        # Instantiated once so the RNG state advances consistently across the
        # full simulation horizon — each call to day_ahead_routing() draws the
        # next independent noise sample from the same seeded generator.
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
            SubscriptionDescription(esdl_type="ElectricityDemand", input_name="demand_power_w", input_unit="W", input_type=h.HelicsDataType.DOUBLE),
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
        
        LOGGER.info("Initializing Network Balancer Service (Dynamic mode)...")

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
            P_GRID_MAX=self.grid_import_limit_w / 1000.0
        )

        # Layers
        self.goal_layer    = GoalManagementLayer(scenario="cost")
        self.change_layer  = ChangeManagementLayer()
        self.control_layer = ComponentControlLayer(initial_soc=50.0)

        self.current_day_step_idx = 0
        self._pending_da_result: Optional[Tuple[Goals, SchedulePlan]] = None
        self._mpc_running  = False
        
        self._state_cache = {
            "actual_power_limit_ID": self.grid_import_limit_w,
            "state_of_charge": 50.0,
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

        # ── Extract grid limit plan from PowerplantService ────────────────────
        raw_limit = None
        for k, v in param_dict.items():
            if "power_limit_plan_DA" in k:
                raw_limit = v; break

        grid_limits_kw = self._parse_json_list(raw_limit, self.grid_import_limit_w / 1000.0)
        n = len(grid_limits_kw)

        # ── Extract carbon intensity plan (previously ignored) ────────────────
        raw_ci = None
        for k, v in param_dict.items():
            if "carbon_intensity_plan_DA" in k:
                raw_ci = v; break

        if raw_ci:
            ci_actual = self._parse_json_list(raw_ci, 250.0)
        else:
            # Fallback: physically-plausible synthetic CI with solar midday dip
            # CI_MEAN=250, solar dip peak at step 52 (13:00), seasonal offset
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

        # ── Price signal: use data_generator pattern centred on PRICE_MEAN ────
        # Realistic daily price shape: cheap at night, peak morning + evening.
        price_actual = [
            float(np.clip(
                0.12
                + 0.04 * np.sin(2 * np.pi * (i / (n / 24) - 14 / 24))   # daily peak at 14:00
                + 0.03 * np.exp(-((i / (n / 24) - 8) ** 2) / 8),         # morning ramp
                0.01, 0.40
            ))
            for i in range(n)
        ]

        # ── DC load: ESDL base load with realistic ±3% hourly texture ─────────
        dc_base_kw = self.dc_base_load_w / 1000.0
        hour_texture = [
            dc_base_kw * (1.0 + 0.03 * np.sin(2 * np.pi * i / n))
            for i in range(n)
        ]

        # ── Apply calibrated Gaussian forecast error ──────────────────────────
        # Each signal is perturbed independently using literature-calibrated σ:
        #   CI_grid : σ=12%  (ENTSO-E forecast accuracy; Staffell & Pfenninger 2016)
        #   price_E : σ=15%  (Weron 2014, EPEX SPOT NL day-ahead)
        #   p_DC    : σ=5%   (Pelley et al. 2009, large DC load variability)
        # The actual (unperturbed) values are used by the intra-day MPC layer,
        # creating realistic deviation events that test RQ4.
        forecast = {
            "price_E":        self._forecast_error.perturb("price_E",  pd.Series(price_actual)),
            "CI_grid":        self._forecast_error.perturb("CI_grid",  pd.Series(ci_actual)),
            "grid_available": pd.Series([(v > 0) for v in grid_limits_kw]),
            "p_DC":           self._forecast_error.perturb("p_DC",     pd.Series(hour_texture)),
        }

        LOGGER.info(
            "[DA Forecast] t=%s  n=%d steps  "
            "CI_mean=%.0f→%.0f gCO2/kWh  price_mean=%.3f→%.3f €/kWh  "
            "p_DC_mean=%.0f→%.0f kW",
            simulation_time.isoformat(), n,
            float(pd.Series(ci_actual).mean()),    float(forecast["CI_grid"].mean()),
            float(pd.Series(price_actual).mean()), float(forecast["price_E"].mean()),
            float(pd.Series(hour_texture).mean()), float(forecast["p_DC"].mean()),
        )

        self.current_day_step_idx = 0
        threading.Thread(target=self._run_day_ahead_lp, args=(forecast, self.control_layer.SOC), daemon=True).start()
        return {}

    def network_dispatch(self, param_dict, simulation_time, time_step_number, esdl_id, energy_system):
        if self._pending_da_result:
            goals, plan = self._pending_da_result
            self._pending_da_result = None
            self.change_layer.load_day_ahead_plan(goals, plan)

        demand_w = 0.0
        for k, v in param_dict.items():
            if "demand_power_w" in k:
                demand_w = v; break
        
        # In absence of direct input, use ESDL-defined limit
        limit_w      = self._state_cache["actual_power_limit_ID"]
        actual_soc   = self._state_cache["state_of_charge"]
        ci_val       = self._state_cache["actual_carbon_intensity_ID"]
        
        grid_available = limit_w > 0.0
        p_dc_kw = demand_w / 1_000.0
        self.control_layer.SOC = actual_soc

        setpoint_kw = 0.0
        if self.change_layer.plan is not None:
            step = min(self.current_day_step_idx, len(self.change_layer.plan.p_ch_b) - 1)
            # Pass actual demand so demand spikes can trigger replanning
            state = self.change_layer.monitor(step, actual_soc, grid_available, p_dc_actual_kw=p_dc_kw)
            event = self.change_layer.analyze(state)
            if event.triggered_replan and not self._mpc_running:
                self._mpc_running = True
                threading.Thread(target=self._run_mpc_replan, args=(event,), daemon=True).start()
            setpoint_kw = float(self.change_layer.plan.p_ch_b.iloc[step])
        else:
            setpoint_kw = self._heuristic_fallback(actual_soc, limit_w, demand_w)

        exec_state = self.control_layer.execute_step(setpoint_kw, grid_available, p_dc_kw, 0.12, ci_val, self.sys_config)
        
        self.current_day_step_idx += 1

        bess_w = exec_state.p_ch_b * 1000.0
        grid_w = exec_state.p_grid * 1000.0
        backup_w = exec_state.unserved * 1000.0

        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_Grid_W", simulation_time, grid_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_BESS_W", simulation_time, bess_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Backup_Requested_Power_W", simulation_time, backup_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Total_Routed_Demand_W", simulation_time, demand_w)
        # Demand delta vs day-ahead forecast (thesis SQ3 — MPC replan analysis)
        if self.change_layer.goals is not None:
            step_idx = max(0, self.current_day_step_idx - 1)
            da_step  = min(step_idx, len(self.change_layer.goals.p_DC) - 1)
            forecast_w = float(self.change_layer.goals.p_DC.iloc[da_step]) * 1_000.0
            self.influx_connector.set_time_step_data_point(esdl_id, "Demand_Delta_W", simulation_time, demand_w - forecast_w)

        return NetworkDispatchOutput(
            bess_allocation_w=bess_w,
            grid_allocation_w=grid_w,
            current_max_power_limit=limit_w,
            backup_requested_power=backup_w
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _refresh_system_params(self, energy_system):
        """Extract all relevant parameters from ESDL assets."""
        if not hasattr(self, 'sys_config'):
            self.sys_config = SystemConfig()

        for obj in energy_system.eAllContents():
            eClass = getattr(obj, "eClass", None)
            if not eClass: continue
            
            # Battery
            if eClass.name == "Battery":
                self.sys_config.E_BAT = float(getattr(obj, "capacity", 0.0))
                self.sys_config.P_CH_MAX = float(getattr(obj, "maxChargeRate", 0.0)) / 1000.0
                self.sys_config.P_DCH_MAX = float(getattr(obj, "maxDischargeRate", 0.0)) / 1000.0
                self.sys_config.EFF_CH = float(getattr(obj, "chargeEfficiency", 0.95))
                self.sys_config.EFF_DCH = float(getattr(obj, "dischargeEfficiency", 0.95))
                LOGGER.info(f"[ESDL] Battery: {self.sys_config.E_BAT}kWh, {self.sys_config.P_CH_MAX*1000}W")

            # Grid Connection (PowerPlant in this scenario)
            elif eClass.name == "PowerPlant":
                self.grid_import_limit_w = float(getattr(obj, "power", 4000000.0))
                LOGGER.info(f"[ESDL] Grid Limit: {self.grid_import_limit_w}W")

            # Datacenter Load (ElectricityDemand)
            elif eClass.name == "ElectricityDemand":
                self.dc_base_load_w = float(getattr(obj, "power", 4000000.0))
                LOGGER.info(f"[ESDL] DC Base Load: {self.dc_base_load_w}W")

    def _parse_json_list(self, raw, default_kw):
        if not raw: return [default_kw] * 96
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                # If values are in Watts, convert to kW for the solver
                return [float(x.get("value", x))/1000.0 if isinstance(x, dict) else float(x)/1000.0 for x in data]
            return [float(data)/1000.0] * 96
        except: return [default_kw] * 96

    def _heuristic_fallback(self, soc, limit_w, demand_w):
        if limit_w > demand_w and soc < 80.0:
            return min(self.sys_config.P_CH_MAX, (limit_w - demand_w)/1000.0)
        elif limit_w < demand_w and soc > 20.0:
            return -min(self.sys_config.P_DCH_MAX, (demand_w - limit_w)/1000.0)
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
