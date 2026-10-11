from dataclasses import dataclass


@dataclass
class AutoFOAMRunResult:
    success: bool
    converged: bool
    runtime: float
    log: str
