def diagnose(rr):
    if "Floating point exception" in rr.log:
        return {"failure_type": "fp_exception", "category": "numerical", "message": "Floating point exception",
                "hints": {"reduce_relaxation": True, "use_upwind": True, "reduce_dt": False}, "repair_applied": False}
    if rr.success and rr.converged:
        return {"failure_type": "unknown", "category": "unknown", "message": "no residuals", "hints": {}}
    return {"failure_type": "no_convergence", "category": "physical_model_selection",
            "message": "Simulation did not converge", "hints": {"increase_correctors": True}}
