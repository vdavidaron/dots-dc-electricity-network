# config.py — All simulation constants and parameters

# ── Timeseries ─────────────────────────────────────────────────────────────
YEAR        = 2024
FREQ        = "h"           # hourly resolution
N_HOURS     = 336          # full leap year

# ── Battery (BESS) ─────────────────────────────────────────────────────────
E_BAT       = 1000.0         # Capacity [kWh]
P_CH_MAX    = 200.0         # Max charge power [kW]
P_DCH_MAX   = 200.0         # Max discharge power [kW]
SOC_MIN     = 10.0          # Min SOC [%]
SOC_MAX     = 90.0          # Max SOC [%]
SOC_INIT    = 50.0          # Initial SOC [%]
EFF_CH      = 0.95          # Charge efficiency
EFF_DCH     = 0.95          # Discharge efficiency

# ── Data Center Load ───────────────────────────────────────────────────────
P_DC_BASE   = 50.0         # Constant DC load [kW]

# ── Grid (Non-Firm ATO) ────────────────────────────────────────────────────
GRID_AVAILABILITY = 0.85    # 85% of hours grid is available
P_GRID_MAX  = 200.0         # Max grid draw when available [kW]

# ── Carbon Intensity ───────────────────────────────────────────────────────
CI_MEAN     = 250.0         # gCO2/kWh
CI_STD      = 80.0
CI_MIN      = 50.0
CI_MAX      = 600.0

# ── Scenarios ──────────────────────────────────────────────────────────────
# "cost"    → Scenario A: minimise electricity cost
# "carbon"  → Scenario B: minimise Scope 2 CO2
# "nonfirm" → Scenario C: survive non-firm grid outages
SCENARIOS   = ["cost", "carbon", "nonfirm"]
