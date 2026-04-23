from datetime import datetime
from esdl import esdl
import helics as h
from dots_infrastructure.DataClasses import EsdlId, TimeStepInformation
from dots_infrastructure.CalculationServiceHelperFunctions import get_single_param_with_name, get_vector_param_with_name
from dots_infrastructure.Logger import LOGGER
from esdl import EnergySystem
import logging

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

    def network_dispatch(self, param_dict: dict, simulation_time: datetime, time_step_number: TimeStepInformation, esdl_id: EsdlId, energy_system: esdl.EnergySystem):
        
        # 1. Read the input from the Datacenter Demand service
        # If the datacenter hasn't output anything yet, default to 0.
        total_demand_w = CalculationServiceHelperFunctions.get_single_param_with_name(param_dict, "demand_power_w")
        if total_demand_w is None:
            total_demand_w = 0.0

        # 2. Peak Shaving Logic
        # The grid handles the base load up to our defined limit
        grid_allocation_w = min(total_demand_w, self.grid_import_limit_w)
        
        # The battery handles any peak demand that exceeds the grid limit
        bess_allocation_w = max(0.0, total_demand_w - self.grid_import_limit_w)

        # 3. Write routing metrics to InfluxDB
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_Grid_W", simulation_time, grid_allocation_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Routed_to_BESS_W", simulation_time, bess_allocation_w)
        self.influx_connector.set_time_step_data_point(esdl_id, "Total_Routed_Demand_W", simulation_time, total_demand_w)

        # 4. Output the specific allocations to the connected federates
        return NetworkDispatchOutput(
            bess_allocation_w=bess_allocation_w,
            grid_allocation_w=grid_allocation_w
        ) 
if __name__ == "__main__":

    helics_simulation_executor = Networkbalancerservice()
    helics_simulation_executor.start_simulation()
    helics_simulation_executor.stop_simulation()
