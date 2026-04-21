from datetime import datetime
import unittest
import time
import json
import os
import sys
from unittest.mock import MagicMock
import pandas as pd

# Add the source directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../src/Networkbalancerservice')))

from networkbalancerservice import Networkbalancerservice
from dots_infrastructure.DataClasses import SimulatorConfiguration, TimeStepInformation, EsdlId
from esdl.esdl_handler import EnergySystemHandler
import helics as h

BROKER_TEST_PORT = 23404
START_DATE_TIME = datetime(2024, 1, 1, 0, 0, 0)
SIMULATION_DURATION_IN_SECONDS = 960
TEST_ID = "test-id"

def simulator_environment_e_connection():
    return SimulatorConfiguration("EConnection", [TEST_ID], "Mock-Econnection", "127.0.0.1", BROKER_TEST_PORT, "test-id", SIMULATION_DURATION_IN_SECONDS, START_DATE_TIME, "test-host", "test-port", "test-username", "test-password", "test-database-name", h.HelicsLogLevel.DEBUG, ["PVInstallation", "EConnection"])

class TestNetworkBalancer(unittest.TestCase):

    def setUp(self):
        from dots_infrastructure import CalculationServiceHelperFunctions
        CalculationServiceHelperFunctions.get_simulator_configuration_from_environment = simulator_environment_e_connection
        
        esh = EnergySystemHandler()
        esdl_path = os.path.join(os.path.dirname(__file__), "datacenter_bess_scenario.esdl")
        esh.load_file(esdl_path)
        self.energy_system = esh.get_energy_system()

    def test_ems_v2_realtime_management(self):
        """
        Verifies that the service correctly handles real-time management 
        with minimal blocking inputs.
        """
        service = Networkbalancerservice()
        service.influx_connector = MagicMock()
        service.init_calculation_service(self.energy_system)
        
        # 1. Test Day Ahead Routing (Only one input expected)
        # --------------------------------------------------
        complex_payload = [{"time": f"2024-01-01T{i:02}:00:00", "value": 4000.0} for i in range(96)]
        # Key format must match framework's expectation: AssetType/InputName/AssetId
        param_dict_da = {
            "PowerPlant/power_limit_plan_DA/None": json.dumps(complex_payload)
        }
        
        esdl_id = EsdlId("test-id")
        time_info = TimeStepInformation(0, 96)
        
        service.day_ahead_routing(param_dict_da, START_DATE_TIME, time_info, esdl_id, self.energy_system)
        
        # Wait for LP
        timeout = 5
        start_wait = time.time()
        while service._pending_da_result is None:
            if time.time() - start_wait > timeout:
                self.fail("Day-ahead LP failed.")
            time.sleep(0.1)
            
        self.assertIsNotNone(service._pending_da_result)

        # 2. Test Network Dispatch (Only demand_power_w expected)
        # ------------------------------------------------------
        param_dict_dispatch = {
            "ElectricityDemand/demand_power_w/None": 3500000.0
        }
        
        output = service.network_dispatch(param_dict_dispatch, START_DATE_TIME, time_info, esdl_id, self.energy_system)
        
        self.assertIsNotNone(output)
        self.assertEqual(output.grid_allocation_w, 3500000.0) # Should cover load
        self.assertEqual(service.current_day_step_idx, 1)

if __name__ == '__main__':
    unittest.main()
