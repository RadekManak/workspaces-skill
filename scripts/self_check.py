#!/usr/bin/env python3
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.support import run_plain, run_dual_entrypoint, run_sandcastle_only, run_cli_split
from tests.test_workspace_model import WORKSPACE_MODEL_TESTS
from tests.test_base_cli import BASE_TESTS
from tests.test_sandcastle_cli import SANDCASTLE_TESTS
from tests.test_cli_split import CLI_SPLIT_TESTS


def main():
    passed = 0
    passed += run_plain(WORKSPACE_MODEL_TESTS)
    passed += run_dual_entrypoint(BASE_TESTS)
    passed += run_sandcastle_only(SANDCASTLE_TESTS)
    passed += run_cli_split(CLI_SPLIT_TESTS)
    print(f"{passed} self-check(s) passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        traceback.print_exc()
        print(f"self-check failed: {error}", file=sys.stderr)
        sys.exit(1)
