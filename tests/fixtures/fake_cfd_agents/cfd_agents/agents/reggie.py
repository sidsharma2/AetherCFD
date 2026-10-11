class Reggie:
    def design_transient_numerics(self, *, max_non_orthogonality, initial_delta_t, target_courant_max=0.5,
                                  relax_if_unstable=False):
        return {"solver_name": "pimpleFoam", "ddt_scheme": "backward", "ddt_scheme_rationale": "2nd order",
                "pimple": {"nOuterCorrectors": "2", "nNonOrthogonalCorrectors": "1" if max_non_orthogonality > 65
                           else "0", "adjustTimeStep": "yes", "maxCo": str(target_courant_max)},
                "pimple_rationale": "from the mesh", "relaxation_factors": {"p": 0.3, "U": 0.7, "k": 0.7},
                "initial_delta_t": initial_delta_t, "target_courant_max": target_courant_max}
