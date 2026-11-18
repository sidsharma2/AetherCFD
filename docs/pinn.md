# PINN field reconstruction

AetherCFD can train a physics-informed neural network (PINN) on a solved case. The network learns one
field from a small set of data points. The flow physics acts as a penalty that pulls the field toward a
solution of the equations between the points. You find it in the Analysis panel, PINN tab, and in the
copilot as the `pinn_train` tool.

## What it is for

- Rebuilding a field from sparse probes. For example, a temperature or mixture fraction from 20 to 100
  thermocouple or sampling points, with the CFD velocity carrying the scalar (advection-diffusion).
- Rebuilding a laminar incompressible flow from a few velocity probes (Navier-Stokes).
- A smooth, differentiable fit of a CFD field over a box ("Data fit only"), for example to take
  derivatives or to compare with the physics-informed fit.

## What it is not for

- It is not a solver. A PINN result is a reconstruction. It does not replace the CFD solution and it is
  not checked for convergence the way OpenFOAM checks its residuals. Every result says so.
- It is not reliable on turbulent 3-D flows. On a turbulent 3-D jet-mixing case the temperature
  reconstruction reached only RMSE/std 0.78 to 0.81. A value near 1 means the network does about as well
  as predicting the mean everywhere.
- It does not extrapolate. Away from the data and outside the box of the training points the error grows,
  and only the held-out error tells you by how much.

## Setup on a new computer

PINNs train in a separate Python environment with PyTorch. The app itself does not contain PyTorch, so a
CUDA or driver problem cannot crash the GUI. Create the environment once:

```
python scripts/setup_ml_env.py
```

Run it from the AetherCFD source folder with any Python 3.10 or newer. The installed app has no `scripts`
folder. There, run `aethercfd-cli ml-setup` from the install folder; it does the same and takes the same
options. The script:

1. Looks for an NVIDIA driver with `nvidia-smi`. With a driver for CUDA 12.6 or newer it installs the
   CUDA build of PyTorch (about 2.5 GB). Without one it installs the CPU build (a few hundred MB).
2. Creates the environment in `%LOCALAPPDATA%\AetherCFD\ml-env` on Windows
   (`~/.local/share/AetherCFD/ml-env` on Linux, `~/Library/Application Support/AetherCFD/ml-env` on
   macOS). It uses `uv` when uv is on PATH, otherwise `python -m venv` and pip.
3. Runs a small forward and backward pass in the new environment and prints the torch version and the GPU.
4. Saves the path as the `ml_python` setting. AetherCFD also finds the default folder without the setting.

Useful options: `--cpu` (CPU build even with a GPU), `--cuda cu128` (a specific CUDA build), `--dir PATH`
(another folder), `--check` (only test the environment), `--dry-run` (print the commands), `--force`
(install PyTorch again into an existing folder). An existing environment that works is left alone.

AetherCFD looks for the environment in this order: the `ml_python` setting, the `AETHERCFD_ML_PYTHON`
environment variable, `.ml-env` next to the source checkout, the default folder above, and `ml-env` next
to the installed exe. Each of these can be the python executable or the environment folder.

When no environment is found, or torch does not load, the PINN tab says so, disables "Train PINN" and
shows these steps. Press "Check again" after you run the script. The copilot tool gives the same message.

## Worked example

The test fixture `tests/fixtures/cases/esi_multiRegionHeater` is a small solved conjugate heat transfer
case (ESI tutorial multiRegionHeater, saved time 0.4). Copy it somewhere first, because "Write fields"
writes into the case. The example has two parts. The first works. The second shows what a failure looks
like and how the reported numbers reveal it.

### A data fit in the heater

1. Open the copy. In the Analysis panel, choose the region `heater` and the PINN tab.
2. Physics: "Data fit only (no physics)". Field: `T`. The heater is a solid with 80 cells, and its
   temperature runs from 300 K to 345 K.
3. Data points: 40. Adam steps: 3000. L-BFGS steps: 400. Seed: 0.
4. Press "Train PINN". The plot shows the data loss and the held-out error while it trains. "Stop" ends
   the run at any time.
5. Read the result in the box below the buttons.

The same run from Python:

```python
from aethercfd.ml import pinn
job = pinn.build_job("heater-copy", physics="data", field="T", region="heater", n_data=40,
                     epochs_adam=3000, lbfgs_steps=400, seed=0)
res = pinn.wait(pinn.start(job))
print("\n".join(pinn.summary(res)))
```

On the development laptop (RTX 3050 Ti, 4 GB, shared with another training job) it took 16 s and
reported:

```
Error against the CFD field on 40 cells the network did not train on (RMSE / std): T 0.0213.
Error on held-out points (40 random CFD cells, held out of training): T 0.0213.
Fit on the training points: T 0.0011.
```

The held-out error is about 2 % of the spread of T, so the fit is good between the points. With 20 data
points instead of 40 the held-out error rose to 0.348, while the training fit stayed below 0.001. The loss
alone would have hidden that.

6. "Write fields" saves `TPinn` (the reconstruction) and `TPinnErr` (reconstruction minus CFD) at the
   result time, and a record of the run in `.aethercfd/pinn/`. "Compare in 3D" shows CFD, PINN and the
   difference side by side.

### Advection-diffusion in the water, where it fails

Choose the region `bottomWater`, physics "Advection-diffusion of a scalar (CFD velocity)", field `T`, D
`1.43e-7` (water, m²/s), 40 data points, and the same steps. It took 84 s and reported RMSE/std 1.00 on
the 1420 cells it did not train on and 1.00 on the held-out points, with a training fit of 0. The note
under the result says why: the training values spread over only 5 % of the field's spread. At this time the
water has warmed only in a thin layer next to the heater, by at most 0.06 K, and the 40 random cells
almost all lie outside it. The network learned "300 K everywhere". The physics cannot add a hot layer that
no data point shows. Put the data points, or the box, where the field varies.

## What the reported errors mean

All errors are RMSE divided by the standard deviation of the reference values, per output. 0 is perfect.
1 is no better than the mean value everywhere.

- Error against the CFD field on cells the network did not train on. This is the main number. The
  training cells are left out. When the data are measurements, the CFD field is only a reference, with
  its own model error.
- Error on held-out points. Before training, up to 500 random CFD cells (or 20 % of your measurement
  points, when at least 5 stay for training) are set aside. The network never sees them. The value is also
  logged during training, so you can see when more steps stop helping.
- Notes under the result. They appear when the training values spread over less than 10 % of the field's
  spread, when they are all the same, or when the field hardly varies at all.
- Fit on the training points. This only shows that the optimiser worked. A small fit error with a large
  held-out error means the network memorised the points.

## Reproducibility

Each run saves `record.json` in its job folder, and "Write fields" copies it to
`.aethercfd/pinn/<field>Pinn_<time>.json`. It holds the seed, the torch, CUDA, numpy and Python versions,
the device and GPU, all hyper-parameters with their defaults filled in, a SHA-256 hash of the training
inputs, the case, region and time, and the errors above. The same seed gives the same data, held-out and
collocation points and the same initial network. On a GPU the result can still differ in the last digits
between runs, because some CUDA operations are not deterministic.

## Failures and what you see

- No environment, or torch missing or broken: a message that names the Python it tried and the setup steps.
- The loss becomes NaN: training stops with "training diverged" and the step. Use a smaller learning rate
  or check D or ν and the units.
- The GPU runs out of memory (it is shared with other programs): the run starts again on the CPU and the
  result says so. On the CPU an out-of-memory error asks for fewer points or a smaller network.
- The worker crashes or is killed: the tab reports that it stopped without a result, with the end of its
  log. It never waits forever. A crash inside PyTorch itself is named as one, with its exit code and the
  last lines of the stack from `worker.log`.
- `CUDA_VISIBLE_DEVICES=-1` (or `none`) is set to hide the GPU: the CUDA build of PyTorch 2.14.1 crashed
  during CPU training with that value, in 10 of 11 test runs. AetherCFD passes the worker an empty value,
  which hides the GPU without the crash, and the result notes it. Your own environment is not changed.
- Stop, closing the panel, opening another case or closing the app ends the worker. If the app crashes, the
  worker notices within a few steps and exits.
- The copilot tool waits `wait_min` minutes (default 20) and then stops the run.

## Hard boundary values

For jobs built from Python there is an option `hard_bc`. With `hard_bc="auto"`, every face of the bounding
box that holds data points (at least 3, and 5 % of them) gets its values by construction: the output is
`g(x) + phi(x) N(x)`, where `g` is a small network fitted to the face data first and `phi` is zero on
those faces. You can also name faces, for example `hard_bc=["x-", "x+"]`. This helps when the data lie on
the domain boundary, as in a channel with measured inlet and outlet profiles. It does not help with random
interior probes, so the panel does not offer it.

## Known limits

- Steady problems only: incompressible Navier-Stokes, advection-diffusion of one scalar, or a data fit.
- The domain is the bounding box of the points. Walls inside the box are not known to the network.
- Soft physics constraints balance badly on stiff or turbulent problems. Expect large errors on 3-D
  turbulent jets (RMSE/std 0.78 to 0.81 on a 3-D jet case), and check the held-out error before you use a result.
- The best case is the Kovasznay flow test: Re 20, exact velocity data on the boundary only, 4000 Adam and
  800 L-BFGS steps. On the development laptop it reached a relative L2 error of 0.06 % in u and 0.42 % in v,
  and RMSE/std 0.001 (u) and 0.004 (v) on 300 held-out interior points, in 196 s on the GPU.
- A 4 GB laptop GPU is enough at the default sizes (8000 collocation points, 4 layers of 64).

## For developers

- `src/aethercfd/ml/pinn.py` builds jobs and runs the worker. `src/aethercfd/ml/pinn_worker.py` trains in
  the PyTorch environment. `src/aethercfd/ml/envsetup.py` is the setup logic behind the script.
- Tests: `tests/unit/test_pinn_plumbing.py` and `tests/studio/test_pinn_panel.py` use a fake worker and need
  no torch. `tests/unit/test_pinn.py` trains for real and is skipped without an environment. Point it at
  one with `AETHERCFD_ML_PYTHON=<python or environment folder>`.
