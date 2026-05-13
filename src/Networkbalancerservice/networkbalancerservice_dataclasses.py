from dataclasses import dataclass
from typing import List


@dataclass
class NetworkDispatchOutput:
    bess_allocation_w : float | None = None
    grid_allocation_w : float | None = None
    current_max_power_limit : float | None = None
    backup_requested_power : float | None = None
    served_datacenter_power_w : float | None = None

