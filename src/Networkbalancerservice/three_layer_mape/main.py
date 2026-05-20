# main.py
# Entry point — runs all three scenarios and prints KPI comparison
#
# Install dependencies:
#   pip install pulp pandas numpy matplotlib

import pandas as pd
import matplotlib.pyplot as plt

import config
from data.data_generator import generate_full_year
from simulation.orchestrator import BESSOrchestrator
from plottting import plot_week


def run_scenario(scenario: str, year_data: pd.DataFrame) -> pd.DataFrame:
    print(f"\n{'═'*60}")
    print(f"  SCENARIO: {scenario.upper()}")
    print(f"{'═'*60}")
    orch    = BESSOrchestrator(scenario=scenario)
    results = orch.run_year(year_data)
    results["scenario"] = scenario
    return results


def print_kpis(all_results: dict[str, pd.DataFrame]) -> None:
    print(f"\n{'═'*60}")
    print(f"  KPI SUMMARY")
    print(f"{'═'*60}")
    print(f"{'Scenario':<12} {'Cost (€)':>12} {'CO2 (tCO2)':>12} "
          f"{'Unserved (kWh)':>16} {'Replans':>8}")
    print("-" * 62)
    for name, df in all_results.items():
        cost     = df["cost_eur"].sum()
        co2      = df["carbon_gco2"].sum() / 1e6
        unserved = df["unserved_kw"].sum()
        # Count MPC replans from alarm messages
        replans  = df["alarm_msg"].str.contains("outage").sum()
        print(f"{name:<12} {cost:>12.2f} {co2:>12.3f} {unserved:>16.1f} {replans:>8}")



if __name__ == "__main__":

    # Generate one full year of synthetic data
    print("Generating full-year simulation data...")
    year_data = generate_full_year()
    print(f"  {len(year_data)} hours  |  "
          f"grid availability: {year_data['grid_available'].mean()*100:.1f}%")

    # Run all scenarios
    all_results = {}
    for scenario in config.SCENARIOS:
        all_results[scenario] = run_scenario(scenario, year_data)

    # KPI table
    print_kpis(all_results)

    # Plot sample week for each scenario
    for scenario, results in all_results.items():
        plot_week(results, scenario)
