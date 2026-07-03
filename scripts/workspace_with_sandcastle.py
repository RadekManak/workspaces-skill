#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import yaml  # noqa: F401
except ImportError:
    print("PyYAML is required: python3 -m pip install pyyaml", file=sys.stderr)
    sys.exit(2)

from workspaces.cli import main


if __name__ == "__main__":
    main(sandcastle=True, prog="workspace_with_sandcastle.py")
