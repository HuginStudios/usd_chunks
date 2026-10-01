from __future__ import annotations
import os
import pathlib
import sys

import pytest
from usd_chunks.chunk_registry import ChunkLayerManager, ChunkRegistry

import chunk_stubs

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:  # the ui extra is optional; only Qt tests ask for qapp
    QtCore = QtWidgets = None

# Qt widget tests (test_chunk_pin.py, test_filter_bar.py, ...) instantiate real
# ChunkWidget/QMainWindow objects via the qapp fixture below -- without forcing Qt's
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
def _opt_out_of_power_throttling() -> None:
    """Keep Windows from throttling this test process.

    On a laptop's Balanced plan, Windows throttles a busy process that is not in the
    foreground (EcoQoS) a few seconds in. Measured on an i7-13620H: plain Python went from
    60ms to 420ms per loop after 3s, which is why the first test of a run was fast and every
    later one several times slower. Opting out needs no admin rights and only affects this
    process. A no-op elsewhere, or if Windows refuses.
    """
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class _State(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        # ProcessPowerThrottling (4): control EXECUTION_SPEED (1), state 0 = never throttle.
        state = _State(1, 1, 0)
        kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state))
    except (OSError, AttributeError):
        pass


_opt_out_of_power_throttling()

_SCRIPTS = str(pathlib.Path(__file__).resolve().parent.parent / "chunks")
if _SCRIPTS not in os.environ.get("USD_CHUNK_DIRS", "").split(os.pathsep):
    _existing = os.environ.get("USD_CHUNK_DIRS", "")
    os.environ["USD_CHUNK_DIRS"] = (
        f"{_existing}{os.pathsep}{_SCRIPTS}" if _existing else _SCRIPTS
    )


@pytest.fixture(scope="session")
def qapp():
    """Session-wide QApplication required by any test that creates Qt widgets."""
    if QtWidgets is None:
        pytest.skip("PySide6 is not installed")
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


@pytest.fixture(autouse=True)
def release_app_scene():
    """Let go of an app's scene a test left registered.

    Opening a graph frontend (Flet, ImGui) registers its scene as an app's -- is_app, the
    way usdview's is -- and nothing unregisters it when the test ends. A script Scene()
    refuses to start while an app's scene is live, so every Scene-based test that happened
    to run after one on the same xdist worker failed with "is already running a scene":
    all of test_module_hash, depending on how the run was split.

    Demoted rather than dropped, the way test_scene_new_current does it by hand: tests that
    register a scene once at import (test_new_chunks) run on whatever scene is registered
    by then, and framework code reaches registry.scene.get_stage() without a None check.
    """
    yield
    scene = getattr(ChunkRegistry(), "scene", None)
    if getattr(scene, "is_app", False):
        scene.is_app = False


@pytest.fixture(autouse=True)
def unregister_stub_modules():
    """Drop the stub chunk modules a test registered (chunk_stubs), so none outlives it."""
    yield
    chunk_stubs.unregister_stub_modules()


@pytest.fixture(scope="module", autouse=True)
def delete_widgets_left_by_the_file():
    """Delete every widget a test file leaves behind, once the file is done.

    Many Qt tests only close() their panel, and a deleteLater() never runs without an event
    loop. The widgets then stay alive for the rest of the worker, and every Qt call in every
    later file gets slower: after test_copy_paste.py left 1300 widgets, building the same 200
    widgets took 235ms instead of 17ms. Per file, not per test: fixtures shared across a
    file's tests still need their widgets until the file ends.
    """
    yield
    app = QtWidgets.QApplication.instance() if QtWidgets is not None else None
    if app is None:
        return
    from usd_chunks.frontends.qt import nodegraph

    sink = nodegraph._proxy_sink  # shared by every graph for the whole process; keep it
    for widget in app.topLevelWidgets():
        if widget is sink:
            continue
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
