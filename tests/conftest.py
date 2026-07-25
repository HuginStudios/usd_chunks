import os

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


@pytest.fixture(autouse=True)
def reset_chunk_animation_manager():
    """Reset ChunkAnimationManager's tracked-param set between tests for isolation.

    Without this, a param object kept alive by a previous test's local variables would
    stay tracked, and on_frame_changed() in a later test would try to touch a param whose
    backing stage no longer matches ChunkRegistry().scene.
    """
    yield
    from usd_chunks.chunk_animation import ChunkAnimationManager
    ChunkAnimationManager().clear()
