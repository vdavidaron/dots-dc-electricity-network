import pandas as pd
import matplotlib.pyplot as plt

import config

def plot_week(results: pd.DataFrame, scenario: str, week_start: str = "2024-01-7") -> None:
    """Plot one representative week of simulation output."""
    week = results.loc[week_start: pd.Timestamp(week_start) + pd.Timedelta(days=6)]

    fig, axes = plt.subplots(4, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(f"Scenario: {scenario.upper()} — sample week ({week_start})", fontsize=13)

    axes[0].plot(week.index, week["SOC"],      color="steelblue", label="SOC [%]")
    axes[0].axhline(config.SOC_MIN, color="red",   linestyle="--", linewidth=0.8, label="SOC min/max")
    axes[0].axhline(config.SOC_MAX, color="red",   linestyle="--", linewidth=0.8)
    axes[0].set_ylabel("SOC [%]")
    axes[0].legend(fontsize=8)

    axes[1].plot(week.index, week["p_ch_b"],   color="darkorange", label="p_ch_b [kW]")
    axes[1].axhline(0, color="grey", linewidth=0.5)
    axes[1].set_ylabel("Charge power [kW]")
    axes[1].legend(fontsize=8)

    axes[2].plot(week.index, week["price_E"],  color="green",  label="Price [€/kWh]")
    ax2b = axes[2].twinx()
    ax2b.plot(week.index, week["CI_grid"],     color="brown",  linestyle="--", alpha=0.6, label="CI [gCO2/kWh]")
    axes[2].set_ylabel("Price [€/kWh]")
    ax2b.set_ylabel("CI [gCO2/kWh]")
    axes[2].legend(loc="upper left",  fontsize=8)
    ax2b.legend(loc="upper right", fontsize=8)

    # Grid availability as shaded background
    for i in range(len(week)):
        if not week["grid_avail"].iloc[i]:
            axes[3].axvspan(week.index[i], week.index[i] + pd.Timedelta(hours=1),
                            color="red", alpha=0.3)
    axes[3].plot(week.index, week["p_grid"],   color="navy",  label="p_grid [kW]")
    axes[3].plot(week.index, week["p_DC"],     color="grey",  linestyle="--", label="p_DC [kW]")
    axes[3].set_ylabel("Power [kW]")
    axes[3].legend(fontsize=8)
    axes[3].set_xlabel("Time")

    plt.tight_layout()
    plt.savefig(f"results_{scenario}.png", dpi=150)
    print(f"[Plot] Saved results_{scenario}.png")
    plt.show()