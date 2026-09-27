from __future__ import annotations
import os
import pathlib

import pytest
from usd_chunks.chunk_registry import ChunkLayerManager

# Qt widget tests (test_chunk_pin.py, test_filter_bar.py, ...) instantiate real
# ChunkWidget/QMainWindow objects via the qapp fixture below — without forcing Qt's
# built-in offscreen platform plugin first, each one flashes a real, visible window on
# screen for the duration of the test run. Must be set before QApplication is ever
# constructed (Qt reads QT_QPA_PLATFORM at that point), so set it at import time here
# rather than inside the fixture. Only takes effect if the environment hasn't already
# picked a platform (e.g. a developer explicitly wanting on-screen widgets to debug).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The suite loads real chunk modules by name (get_chunk_module), and the registry only
# looks in USD_CHUNK_DIRS. With it unset the suite reports 250 failures and 50 errors
# (measured, not estimated), most of them a NoneType 'loaded' -- which reads like broken
# code rather than a missing path, so it is set here rather than left to whoever runs
# pytest. Note test_chunk_core.py is NOT among them and passes either way; it is a poor
# test to check this with.
#
# It belongs here rather than in pytest.ini because pytest cannot set environment
# variables without the pytest-env plugin, and adding a dependency for one variable is a
# worse trade than the two lines below. Same reason and same mechanism as QT_QPA_PLATFORM
# above; both must be set before the code that reads them is imported.
#
# Appended rather than assigned: a developer pointing the var at their own chunk
# directory keeps it, and keeps precedence, since entries earlier in the list win on a
# duplicate module name (ChunkEnvVar._get). Absolute, so the suite does not depend on
# being run from the repo root.
_SCRIPTS = str(pathlib.Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS not in os.environ.get("USD_CHUNK_DIRS", "").split(os.pathsep):
    _existing = os.environ.get("USD_CHUNK_DIRS", "")
    os.environ["USD_CHUNK_DIRS"] = (
        f"{_existing}{os.pathsep}{_SCRIPTS}" if _existing else _SCRIPTS
    )


@pytest.fixture(scope="session")
def qapp():
    """Session-wide QApplication required by any test that creates Qt widgets."""
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def reset_chunk_layer_manager():
    """Reset ChunkLayerManager singleton between tests for isolation.

    Without this, tests that call ChunkLayerManager().initialize(stage) leave
    the singleton pointing at a stale stage, which can corrupt layer-stack checks
    in subsequent tests that never initialize the manager themselves.
    """
    yield
    ChunkLayerManager().reset()
