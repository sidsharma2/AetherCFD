"""Set up the PyTorch environment that AetherCFD's PINN training runs in. One step on a new computer:

    python scripts/setup_ml_env.py

It creates a virtual environment (default: %LOCALAPPDATA%/AetherCFD/ml-env on Windows), installs PyTorch
(the CUDA build when an NVIDIA GPU is present, the CPU build otherwise) and numpy, checks it with a tiny
training step and saves its path as the ml_python setting. Any Python 3.10 or newer can run this script;
uv is used when it is on PATH. Options: --help. Details: docs/pinn.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aethercfd.ml.envsetup import main

if __name__ == "__main__":
    sys.exit(main())
