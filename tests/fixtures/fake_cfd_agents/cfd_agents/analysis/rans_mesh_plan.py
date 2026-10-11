import json
import os
from dataclasses import dataclass


@dataclass
class RansMeshPolicy:
    diameter_m: float
    velocity_jump_m_s: float
    max_cells: int = 2_000_000


def build_rans_plan(case_dir, time_name, output_dir, policy, export_gmsh_size_field=False):
    case_dir, output_dir = os.path.abspath(case_dir), os.path.abspath(output_dir)
    if os.path.exists(output_dir) or output_dir.startswith(case_dir):
        raise ValueError("output_dir must be new and outside the source case")
    if not os.path.isfile(os.path.join(case_dir, "constant", "polyMesh", "owner")):
        raise ValueError("topology: no native polyMesh (all-tet ASCII mesh required)")
    os.makedirs(output_dir)
    plan = {"status": "PLAN_ONLY", "n_cells": 400, "active_shear_cells": 12,
            "scenarios": [{"level_cap": 1, "equivalent_cells": 500, "estimated_wall_hours": 0.1}],
            "policy": {"diameter_m": policy.diameter_m, "max_cells": policy.max_cells}}
    with open(os.path.join(output_dir, "refinement_plan.json"), "w") as fh:
        json.dump(plan, fh)
    return plan
