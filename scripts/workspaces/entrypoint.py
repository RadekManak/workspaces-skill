"""Shared bootstrap for the workspace CLI entrypoint scripts.

The entrypoints differ only in which CLI variant they select, so everything
else -- the PyYAML precondition check and the handoff into `cli.main` -- lives
here. The dependency check must run before `workspaces.cli` is imported, which
is why the import is function-local.
"""
import sys


def run(*, sandcastle, prog):
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("PyYAML is required: python3 -m pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    from workspaces.cli import main

    main(sandcastle=sandcastle, prog=prog)
