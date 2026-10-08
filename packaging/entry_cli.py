"""PyInstaller entry point for aethercfd-cli.exe (console): doctor, open, run, runs, control, set."""

import sys

from aethercfd.scripting.cli import main

sys.exit(main())
