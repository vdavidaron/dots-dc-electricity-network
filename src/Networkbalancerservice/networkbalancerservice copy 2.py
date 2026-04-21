from datetime import datetime
from esdl import esdl
import helics as h
from dots_infrastructure.DataClasses import EsdlId, TimeStepInformation
from dots_infrastructure.CalculationServiceHelperFunctions import get_single_param_with_name, get_vector_param_with_name
from dots_infrastructure.Logger import LOGGER
from esdl import EnergySystem
import logging
import random

from networkbalancerservice_base import NetworkbalancerserviceBase
from networkbalancerservice_dataclasses import NetworkDispatchOutput
from dots_infrastructure import CalculationServiceHelperFunctions

LOGGER = logging.getLogger(__name__)

class Networkbalancerservice(NetworkbalancerserviceBase):

    def init_calculation_service(self, energy_system: esdl.EnergySystem):
        super().init_calculation_service(energy_system)
        LOGGER.info("Initializing Network Balancer Service...")
        
        # In a more advanced setup, you could read the peak shaving limit from an ESDL ControlStrategy.
        # Here, we will hardcode a 4 MW (4,000,000 W) grid limit for the datacenter.
        self.grid_import_limit_w = 4000000.0 

    def day_ahead_routing(self, param_dict : dict, simulation_time : datetime, time_step_number : TimeStepInformation, esdl_id : EsdlId, energy_system : EnergySystem):
        pass
    
    def network_dispatch(self, param_dict: dict, simulation_time: datetime, time_step_number: TimeStepInformation, esdl_id: EsdlId, energy_system: esdl.EnergySystem):
        # 1. Read the input from the Datacenter Demand service
        total_demand_w = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "demand_power_w")
        if total_demand_w is None:
            total_demand_w = 0.0

        # 2. Read additional network inputs that affect dispatch decisions
        grid_limit_w = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "actual_power_limit_ID")
        if grid_limit_w is None:
            grid_limit_w = self.grid_import_limit_w

        battery_soc_pct = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "state_of_charge")
        max_bess_charge_w = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "max_available_charge")
        max_bess_discharge_w = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "max_available_discharge")
        backup_available_w = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "available_max_power")

        # 3. Handle routing securely, checking for component availability
        current_max_power_limit = max(0.0, grid_limit_w)
        grid_allocation_w = min(total_demand_w, current_max_power_limit)

        # Use battery first for peaks when possible.
        peak_shortfall_w = max(0.0, total_demand_w - grid_allocation_w)
        
        if max_bess_discharge_w is not None:
            # We have a battery, limit to expected shortfall bounds
            bess_allocation_w = min(peak_shortfall_w, max_bess_discharge_w)
        else:
            # Battery is omitted from ESDL, safely allocate 0 to it
            bess_allocation_w = 0.0

        # If the battery can't cover all the remaining demand, request backup generation.
        remaining_w = max(0.0, peak_shortfall_w - bess_allocation_w)
        
        if backup_available_w is not None:
            # Backup generator present, request up to exactly its available capacity
            backup_requested_power = min(remaining_w, backup_available_w)
        else:
            # No backup available
            backup_requested_power = 0.0

        # 4. Write routing metrics to InfluxDB
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_Grid_W", simulation_time, grid_allocation_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_BESS_W", simulation_time, bess_allocation_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Backup_Requested_Power_W", simulation_time, backup_requested_power)
        self.influx_connector.set_time_step_data_point(esdl_id, "Total_Routed_Demand_W", simulation_time, total_demand_w)

        # 5. Output the specific allocations to the connected federates
        return NetworkDispatchOutput(
            bess_allocation_w=bess_allocation_w,
            grid_allocation_w=grid_allocation_w,
            current_max_power_limit=current_max_power_limit,
            backup_requested_power=backup_requested_power
        ) 
if __name__ == "__main__":

    helics_simulation_executor = Networkbalancerservice()
    try:
        LOGGER.info("Starting simulation...")
        helics_simulation_executor.start_simulation()
    except Exception as e:
        LOGGER.error(f"Simulation crashed due to an error: {e}")
        # Raising the error is optional, but good for debugging so it doesn't fail silently
        raise 
    finally:
        LOGGER.info("Ensuring HELICS federate is finalized and simulation is stopped.")
        # This block executes NO MATTER WHAT, guaranteeing the broker is freed.
        helics_simulation_executor.stop_simulation()
