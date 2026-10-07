# -*- coding: utf-8 -*-
"""Shared bootstrap for the local test scripts.

These tests drive a real ISAT ``MainWindow``, so they need a checkout of
ISAT_with_segment_anything on the machine. ISAT is not on PyPI, so it cannot
be installed as a dependency and the path has to be supplied by whoever runs
the tests.

Resolution order:

1. ``$ISAT_ROOT`` -- set this explicitly.
2. A sibling checkout: ``../ISAT_with_segment_anything`` relative to the
   repository root, which is the usual local layout.
3. An ``ISAT`` package that is already importable.

No machine-specific path is baked in, so the tests are portable and nothing
about the host leaks into the repository.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _looks_like_isat(path):
    return bool(path) and os.path.isdir(
        os.path.join(path, "ISAT", "widgets")
    )


def setup_sys_path():
    """Make ISAT and the plugin under test importable. Returns the ISAT root."""
    candidates = []
    env = os.environ.get("ISAT_ROOT")
    if env:
        candidates.append(env)
    candidates.append(
        os.path.join(os.path.dirname(REPO_ROOT), "ISAT_with_segment_anything")
    )

    for path in candidates:
        if _looks_like_isat(path):
            sys.path.insert(0, os.path.abspath(path))
            return os.path.abspath(path)

    # Fall back to an already-importable ISAT (e.g. pip install isat).
    try:
        import ISAT  # noqa: F401
        return os.path.dirname(os.path.dirname(ISAT.__file__))
    except Exception:
        pass

    raise SystemExit(
        "Could not find ISAT_with_segment_anything.\n"
        "Set $ISAT_ROOT to a checkout, e.g.\n"
        "    ISAT_ROOT=/path/to/ISAT_with_segment_anything python tests/test_eraser.py\n"
        "Tried:\n  " + "\n  ".join(os.path.abspath(c) for c in candidates)
    )


def add_plugin_to_path(relative_package_dir):
    """Put the package under test on sys.path (for uninstalled local runs)."""
    path = os.path.join(REPO_ROOT, relative_package_dir)
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)


def headless():
    """Qt offscreen + no bytecode, so the tests can run unattended."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("MPLBACKEND", "Agg")
    sys.dont_write_bytecode = True
