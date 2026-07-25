#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspaces.entrypoint import run


if __name__ == "__main__":
    run(sandcastle=False, prog="workspace.py")
