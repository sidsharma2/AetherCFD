import glob
import os

from ..contracts import AutoFOAMRunResult  # noqa: F401  (relative import, as in the real module)


class Connie:
    def watch_health(self, postprocessing_dir, *, policies):
        files = glob.glob(os.path.join(str(postprocessing_dir), "*", "*", "fieldMinMax.dat"))
        bad = []
        for f in files:
            with open(f) as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) > 2 and parts[1] == "k" and float(parts[2]) < 0:
                        bad.append({"quantity": "k", "status": "failed", "reason": "negative k"})
                        break
        return {"status": "failed" if bad else "healthy", "quantities": bad, "samples_evaluated": len(files),
                "policy_growth": policies.growth}
