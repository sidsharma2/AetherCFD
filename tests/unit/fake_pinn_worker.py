"""A stand-in for ml/pinn_worker.py that needs no torch: it speaks the same file protocol, so the app's
plumbing (start, progress, cancel, kill, load, held-out errors, messages) is tested on any computer.

    python fake_pinn_worker.py <job_dir>     mode from job.json "_fake":

ok     a few history rows, then outputs = the CFD values (truth_eval.npy) except +5 at the training cells,
       and a held-out error of 0.1 per output
slow   history rows until the "cancel" file appears, then a cancelled result (a cooperative worker)
hang   ignores everything and sleeps (cancel has to kill it)
crash  prints a traceback and exits 1 without a result
oom    prints a CUDA out-of-memory line and exits 1 without a result
nan    a diverged result, as the real worker writes it
"""

import json
import sys
import time
from pathlib import Path

import numpy as np

jd = Path(sys.argv[1])
job = json.loads((jd / "job.json").read_text(encoding="utf-8"))
mode = job.get("_fake", "ok")
hist = (jd / "history.jsonl").open("w", encoding="utf-8")


def result(res):
    tmp = jd / "result.json.tmp"
    tmp.write_text(json.dumps(res), encoding="utf-8")
    tmp.replace(jd / "result.json")


def row(i, val=None):
    hist.write(json.dumps({"stage": "adam", "step": i, "data": 1.0 / (i + 1), "pde": 0.5 / (i + 1), "t": 0.01 * i,
                           **({"val": val} if val is not None else {})}) + "\n")
    hist.flush()


common = {"device": "cpu", "gpu": None, "seconds": 0.1, "seed": job.get("seed", 0), "torch": "fake",
          "hyper": {"lr": 1e-3, "layers": [8]}, "notes": []}
if mode == "ok":
    k = len(job.get("names") or [1])
    for i in range(3):
        row(i, [0.1] * k)
    x = np.load(jd / "inputs.npz")
    if (jd / "truth_eval.npy").is_file():
        y = np.load(jd / "truth_eval.npy").astype(float).copy()
        if (jd / "train_cells.npy").is_file():
            y[np.isin(np.load(jd / "eval_cells.npy"), np.load(jd / "train_cells.npy"))] += 5.0
    else:
        y = np.zeros((len(x["x_eval"]), k))
    np.savez(jd / "outputs.npz", y_eval=y, y_fit=np.zeros_like(x["y_data"]))
    res = {"ok": True, "final_data_loss": 0.01, "final_pde_loss": 0.02, "fit_rmse_over_std": [0.05] * k,
           "n_data": len(x["x_data"]), "n_colloc": len(x["x_colloc"]), "n_eval": len(x["x_eval"]), **common}
    if "x_val" in x:
        res["heldout"] = {"rmse_over_std": [0.1] * k, "n": len(x["x_val"]), "what": job.get("val_source", "?")}
    result(res)
elif mode == "slow":
    i = 0
    while not (jd / "cancel").exists():
        row(i)
        i += 1
        time.sleep(0.05)
    result({"ok": False, "reason": "cancelled", "error": "stopped by the user before it finished", **common})
elif mode == "hang":
    time.sleep(120)
elif mode == "crash":
    print("Traceback (most recent call last):\nRuntimeError: boom in the fake worker", flush=True)
    sys.exit(1)
elif mode == "oom":
    print("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", flush=True)
    sys.exit(1)
elif mode == "nan":
    row(0)
    result({"ok": False, "reason": "diverged", "error": "training diverged: the loss became NaN or infinite at "
                                                        "adam step 40", **common})
hist.close()
