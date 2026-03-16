from datetime import datetime
from esdl import esdl
import helics as h
from dots_infrastructure.DataClasses import EsdlId, TimeStepInformation
from dots_infrastructure.CalculationServiceHelperFunctions import get_single_param_with_name, get_vector_param_with_name
from dots_infrastructure.Logger import LOGGER
from esdl import EnergySystem

class CalculationServiceTest(CalculationServiceTestBase):

    def init_calculation_service(self, energy_system: esdl.EnergySystem):
        LOGGER.info("init calculation service")
        for esdl_id in self.simulator_configuration.esdl_ids:
            LOGGER.info(f"Example of iterating over esdl ids: {esdl_id}")

    def test_calculation(self, param_dict : dict, simulation_time : datetime, time_step_number : TimeStepInformation, esdl_id : EsdlId, energy_system : EnergySystem):
        ret_val = {}
        single_input1_value = get_single_param_with_name(param_dict, "input1") # returns the first value in param dict with "PV_Dispatch" in the key name
        all_input1_values = get_vector_param_with_name(param_dict, "input1") # returns all the values as a list in param_dict with "PV_Dispatch" in the key name
        ret_val["output1"] = single_input1_value
        ret_val["output2"] = sum(all_input1_values)
        self.influx_connector.set_time_step_data_point(esdl_id, "EConnectionDispatch", simulation_time, ret_val["EConnectionDispatch"])
        return ret_val
    
    def test_calculation_2(self, param_dict : dict, simulation_time : datetime, time_step_number : TimeStepInformation, esdl_id : EsdlId, energy_system : EnergySystem):
        ret_val = {}
        ret_val["output3"] = 3.0
        return ret_val
    
if __name__ == "__main__":

    helics_simulation_executor = CalculationServiceTest()
    helics_simulation_executor.start_simulation()
    helics_simulation_executor.stop_simulation()
