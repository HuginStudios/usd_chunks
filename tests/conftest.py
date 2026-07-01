import pytest
from usd_chunks.chunk_registry import ChunkLayerManager


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
    lm = ChunkLayerManager()
    lm.initialized = False
    lm.stage = None
    lm.chunk_layers = {}
    lm.graph_layers = {}
    lm._root_layer = None
