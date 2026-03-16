from dataclasses import dataclass
from typing import List

@dataclass
class NetworkDispatchOutput:
    bess_allocation_w : float | None = None
    grid_allocation_w : float | None = None

