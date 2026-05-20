import uuid
import base64
from esdl.esdl_handler import EnergySystemHandler
from esdl import esdl

# 1. Create empty EnergySystem
esh = EnergySystemHandler()
es = esh.create_empty_energy_system(
    name="Datacenter_BESS_Scenario",
    es_description="Datacenter with BESS and grid connection",
    inst_title="scenario_instance",
    area_title="Datacenter_Site"
)

area = es.instance[0].area

# -----------------------------------------------------------------------------
# 2. Define assets
# -----------------------------------------------------------------------------
# REQUIRED: The Balancer uses this to identify the network it is balancing.
# All controller-tuning KPIs live on this asset (matching the production
# datacenter_bess_scenario.py at dots-services/dots-simulation/).
lv_network = esdl.ElectricityNetwork(id=str(uuid.uuid4()), name="Site Electricity Management System")
lv_network.KPIs = esdl.KPIs(id=str(uuid.uuid4()))
# Lexicographic LP weights
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="w_unserved", value=1e9))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="w_carbon", value=1.0))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="w_price", value=1.0))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="w_effort", value=0.01))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="w_soc_low", value=1e6))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="soc_baseline", value=50.0))
# Asset-level enablement toggles
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="enable_battery", value=1.0))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="enable_backup_generator", value=1.0))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="enable_renewable_service", value=1.0))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="enable_change_management", value=1.0))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="enable_goal_management", value=1.0))
# Mandate toggle — when False, the grid is treated as a one-way import sink
# (no surplus-absorption obligation, no PV curtailment forced by grid surplus).
# Default off in this test fixture so tests don't exercise the mandate path.
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="enable_mandate", value=0.0))
# MPC deviation thresholds
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="mpc_soc_drift_threshold", value=5.0))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="mpc_demand_spike_threshold", value=0.10))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="mpc_horizon_steps", value=24.0))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="mpc_replan_cooldown", value=4.0))
# Forecast-error model parameters
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="forecast_sigma_ci", value=0.12))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="forecast_sigma_p_dc", value=0.05))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="forecast_sigma_price", value=0.15))
lv_network.KPIs.kpi.append(esdl.DoubleKPI(id=str(uuid.uuid4()), name="forecast_seed", value=42.0))

# SERVICE 1: DatacenterDemandService - Realistic 4MW load
# power is typically EDouble but minLoad is EInt in some ESDL versions
datacenter = esdl.ElectricityDemand(id=str(uuid.uuid4()), name="Datacenter Load", power=4000000.0)
datacenter.powerFactor = 0.95

# SERVICE 2: BatteryService - Realistic 10MWh / 4MW BESS
bess = esdl.Battery(id=str(uuid.uuid4()), name="Datacenter BESS", capacity=4000000.0)
bess.chargeEfficiency = 0.95
bess.dischargeEfficiency = 0.95
bess.maxChargeRate = 4000000.0
bess.maxDischargeRate = 4000000.0

# SERVICE 3: PowerPlantService - Realistic Grid connection.
# minLoad=0 here because the test fixture has enable_mandate=0; making the
# constraint match the toggle keeps the test consistent with what the
# powerplant calc will compute at runtime. To exercise the mandate path
# explicitly in a test, set enable_mandate=1 and minLoad=-75000000.
grid_connection = esdl.PowerPlant(id=str(uuid.uuid4()), name="Grid Connection", power=75000000.0)
grid_connection.efficiency = 1.0
grid_connection.minLoad = 0              # Use int for minLoad

# SERVICE 4: LocalGenerator - Realistic 5MW Backup
backup_generator = esdl.GasProducer(id=str(uuid.uuid4()), name="Backup Generator", power=5000000.0)

# SERVICE 5: LocalRenewable - Realistic 1MW Solar array
local_renewable = esdl.PVInstallation(id=str(uuid.uuid4()), name="Local RES", power=1000000.0)
local_renewable.panelEfficiency = 0.20       
local_renewable.inverterEfficiency = 0.98    
local_renewable.surfaceArea = 5000           # 1MW / (1kW/m2 * 0.2) = 5000 m2
local_renewable.angle = 35                 
local_renewable.orientation = 180          

# 3. Add assets to area
area.asset.extend([lv_network, datacenter, bess, grid_connection, backup_generator, local_renewable])

# -----------------------------------------------------------------------------
# 4. Create ports
# -----------------------------------------------------------------------------
net_to_dc = esdl.OutPort(id=str(uuid.uuid4()), name="net_to_datacenter")
net_to_bess = esdl.OutPort(id=str(uuid.uuid4()), name="net_to_bess")
net_from_bess = esdl.InPort(id=str(uuid.uuid4()), name="net_from_bess")
net_to_grid = esdl.OutPort(id=str(uuid.uuid4()), name="net_to_grid")
net_from_grid = esdl.InPort(id=str(uuid.uuid4()), name="net_from_grid")
net_from_gen = esdl.InPort(id=str(uuid.uuid4()), name="net_from_backup_generator")
net_from_res = esdl.InPort(id=str(uuid.uuid4()), name="net_from_local_res")

lv_network.port.extend([
    net_to_dc, net_to_bess, net_from_bess, net_to_grid, net_from_grid, net_from_gen, net_from_res
])

dc_in = esdl.InPort(id=str(uuid.uuid4()), name="dc_in")

# Attach InfluxDBProfile so the demand service reads config from ESDL
# instead of hard-coding measurement/field/database in the service code.
dc_demand_profile = esdl.InfluxDBProfile(
    id=str(uuid.uuid4()),
    host="influxdb",
    port=8086,
    database="GO-e",
    measurement="historical_datacenter_demand",
    field="Demand_W",
    filters="name='Datacenter Load'",
    multiplier=1.0,
)
dc_in.profile.append(dc_demand_profile)

datacenter.port.append(dc_in)

bess_in = esdl.InPort(id=str(uuid.uuid4()), name="bess_in")
bess_out = esdl.OutPort(id=str(uuid.uuid4()), name="bess_out")
bess.port.extend([bess_in, bess_out])

grid_in = esdl.InPort(id=str(uuid.uuid4()), name="grid_in")
grid_bg_profile = esdl.InfluxDBProfile(
    id=str(uuid.uuid4()), host="influxdb", port=8086, database="GO-e",
    measurement="transformer_background", field="background_w",
    filters="name='Grid Connection'", multiplier=1.0,
)
grid_carbon_profile = esdl.InfluxDBProfile(
    id=str(uuid.uuid4()), host="influxdb", port=8086, database="GO-e",
    measurement="carbon_intensity", field="carbon_intensity",
    filters="name='Grid Connection'", multiplier=1.0,
)
grid_in.profile.extend([grid_bg_profile, grid_carbon_profile])
grid_out = esdl.OutPort(id=str(uuid.uuid4()), name="grid_out")
grid_connection.port.extend([grid_in, grid_out])

gen_out = esdl.OutPort(id=str(uuid.uuid4()), name="gen_out")
backup_generator.port.append(gen_out)

res_out = esdl.OutPort(id=str(uuid.uuid4()), name="res_out")
res_profile = esdl.InfluxDBProfile(
    id=str(uuid.uuid4()), host="influxdb", port=8086, database="GO-e",
    measurement="historical_solar_irradiance", field="Irradiance_W_m2",
    filters="name='Local RES'", multiplier=1.0,
)
res_out.profile.append(res_profile)
local_renewable.port.append(res_out)

# -----------------------------------------------------------------------------
# 5. Create connections
# -----------------------------------------------------------------------------
net_to_dc.connectedTo.append(dc_in)
net_to_bess.connectedTo.append(bess_in)
bess_out.connectedTo.append(net_from_bess)
net_to_grid.connectedTo.append(grid_in)
grid_out.connectedTo.append(net_from_grid)
net_from_gen.connectedTo.append(gen_out)
net_from_res.connectedTo.append(res_out)

# -----------------------------------------------------------------------------
# 6. Save and Encode
# -----------------------------------------------------------------------------
file_name = "datacenter_bess_scenario.esdl"
esh.save(filename=file_name)

with open(file_name, "rb") as esdl_file:
    esdl_base64string = base64.b64encode(esdl_file.read()).decode('utf-8')

with open("esdl_base64_payload.txt", "w") as b64_file:
    b64_file.write(esdl_base64string)

print(f"[OK] ESDL file generated.")
