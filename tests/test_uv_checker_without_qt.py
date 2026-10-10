"""UV Checker loads in a host without Qt.

It imported PySide6 at the top for one error popup, so in Blender (no Qt) the module failed
to import and the chunk was missing from the Add menu. Qt is optional now: the popup is
shown when Qt is there, and the message goes to the log when it is not.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender

MODULE = Path(__file__).resolve().parents[1] / "chunks" / "materials" / "uvchecker" / "uv_checker.py"


class _NoStage(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _load_without_qt(monkeypatch):
    # None in sys.modules makes `import PySide6` raise ImportError, as it does in Blender.
    monkeypatch.setitem(sys.modules, "PySide6", None)
    spec = importlib.util.spec_from_file_location("uv_checker_no_qt", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_loads_and_builds_without_qt(monkeypatch):
    chunk_registry.ChunkRegistry().initialize(_NoStage(), "pytest")
    module = _load_without_qt(monkeypatch)
    assert module.QtWidgets is None
    assert module.main() is not None


def test_apply_with_nothing_picked_logs_instead_of_a_popup(monkeypatch, caplog):
    chunk_registry.ChunkRegistry().initialize(_NoStage(), "pytest")
    module = _load_without_qt(monkeypatch)
    built = module.main()
    node = built[0] if isinstance(built, (list, tuple)) else built
    with caplog.at_level(logging.WARNING):
        module.apply(node, None, None)
    assert "Prim not selected" in caplog.text
