"""
Test infrastructure for the node graph.

Session-scoped fixtures initialize ChunkRegistry and ChunkLayerManager once
against a real Usd.Stage. Per-test fixtures create fresh NodeGraph instances.

Injection seam: GraphHost.set_current(TestGraphHost(stage)) lets the node
execute() path get the test stage without usdview.
"""
import sys
from pathlib import Path

# scripts/node_graph/tests/ → scripts/node_graph/ → scripts/ → <repo root>
ROOT = Path(__file__).parent.parent.parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest
from pxr import Usd

# Apply the same GroupNode monkey-patch as graph_widget.py so custom_properties
# ('promoted_params', 'promoted_outputs') are always initialised without importing
# the full graph_widget module.
from NodeGraphQt.nodes.group_node import GroupNode as _GroupNode
_orig_gn_init = _GroupNode.__init__


def _patched_gn_init(self, *args, **kwargs):
    _orig_gn_init(self, *args, **kwargs)
    self.model.custom_properties.setdefault('promoted_params', '{}')
    self.model.custom_properties.setdefault('promoted_outputs', '{}')


_GroupNode.__init__ = _patched_gn_init


# ── Test doubles ─────────────────────────────────────────────────────────────
# Defined as factories to avoid circular-import issues at module load time.

def _make_test_scene_sender(stage):
    from usd_chunks.chunk_core import SceneSender

    class TestSceneSender(SceneSender):
        def __init__(self):
            super().__init__()
            self._stage = stage

        def get_stage(self):
            return self._stage

        def get_selected_prims(self):
            return []

        def get_selected_property(self):
            return None

        def request_save(self):
            pass

    return TestSceneSender()


def _make_test_graph_host(stage):
    from node_graph.graph_host import GraphHost

    class TestGraphHost(GraphHost):
        def get_stage(self):
            return stage

    return TestGraphHost()


# ── Session-scoped fixtures ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="session")
def stage(tmp_path_factory, qapp):
    """One real Usd.Stage for the entire test session."""
    from node_graph.graph_host import GraphHost
    p = tmp_path_factory.mktemp("usd") / "test.usda"
    s = Usd.Stage.CreateNew(str(p))
    GraphHost.set_current(_make_test_graph_host(s))
    return s


@pytest.fixture(scope="session", autouse=True)
def setup_registry(stage):
    """Initialize ChunkRegistry and ChunkLayerManager exactly once.

    Manually injects only the test chunk module so production scripts/
    directories are never scanned during tests.
    """
    from usd_chunks.chunk_registry import ChunkRegistry, ChunkLayerManager, ChunkModule

    reg = ChunkRegistry()
    reg.scene = _make_test_scene_sender(stage)
    reg.plugin = "test"
    reg.initialized = True

    ChunkLayerManager().initialize(stage)

    test_chunk_path = Path(__file__).parent / "chunks" / "test_cube.py"
    mods = ChunkModule.from_chunk_definition(test_chunk_path, "test_cube")
    if mods:
        reg.registered_modules["test_cube"] = mods[0]


# ── Per-test fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def node_graph(qapp, setup_registry):
    """Fresh NodeGraph with GroupNode and TestCube chunk node registered.

    ng.widget is accessed to initialise the Qt widget layer; this is required
    by expand_group_node (which opens sub-graph tabs).
    """
    from NodeGraphQt import NodeGraph
    from NodeGraphQt.nodes.group_node import GroupNode
    from node_graph.nodes import make_chunk_node

    ng = NodeGraph()
    ng.register_node(GroupNode)
    CubeNodeCls = make_chunk_node("test_cube", "TestCube")
    ng.register_node(CubeNodeCls)
    _ = ng.widget  # initialise Qt widget so expand_group_node works
    yield ng, CubeNodeCls
    try:
        ng.clear_session()
    except Exception:
        pass
