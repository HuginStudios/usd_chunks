"""Old module names that still import, for code written before a module moved.

Each old name is an alias for its new home, not a copy: importing either gives the same
module object, so patching an attribute through one name patches both. Running an old
name with ``python -m`` runs the new module as ``__main__``.

One import hook instead of a stub file per old name, so the package tree shows only where
things live now. New code imports the new name; ``tests/test_package_layout.py`` fails if
anything in the repository still uses an old one.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys

#: Old dotted name -> new dotted name.
MOVED: dict[str, str] = {
    # plugins/ split into frontends/, hosts/ and cli
    "usd_chunks.plugins.chunk_qt": "usd_chunks.frontends.qt.factory",
    "usd_chunks.plugins.chunk_qt_widgets": "usd_chunks.frontends.qt.widgets",
    "usd_chunks.plugins.chunk_qt_nodegraph": "usd_chunks.frontends.qt.nodegraph",
    "usd_chunks.plugins.chunk_qt_layer_stack": "usd_chunks.frontends.qt.layer_stack",
    "usd_chunks.plugins.chunk_window_state": "usd_chunks.frontends.qt.window_state",
    "usd_chunks.plugins.chunk_flet": "usd_chunks.frontends.flet.factory",
    "usd_chunks.plugins.chunk_flet_nodegraph": "usd_chunks.frontends.flet.nodegraph",
    "usd_chunks.plugins.chunk_imgui": "usd_chunks.frontends.imgui.factory",
    "usd_chunks.plugins.chunk_imgui_nodegraph": "usd_chunks.frontends.imgui.nodegraph",
    "usd_chunks.plugins.chunk_blender": "usd_chunks.frontends.blender.factory",
    "usd_chunks.plugins.chunk_blender_nodegraph": "usd_chunks.frontends.blender.nodegraph",
    "usd_chunks.plugins.usdchunks_cli": "usd_chunks.cli",
    "usd_chunks.plugins.chunk_usd": "usd_chunks.chunk_stage",
    # the core's graph, gizmo and store modules grouped into subpackages
    "usd_chunks.chunk_graph": "usd_chunks.graph.engine",
    "usd_chunks.chunk_graph_layout": "usd_chunks.graph.layout",
    "usd_chunks.chunk_gizmo": "usd_chunks.gizmo.runtime",
    "usd_chunks.chunk_gizmo_types": "usd_chunks.gizmo.types",
    "usd_chunks.pin": "usd_chunks.store.pin",
    "usd_chunks.stores": "usd_chunks.store.providers",
}

#: Old packages that no longer exist on disk but still hold moved names.
_EMPTY_PACKAGES = {"usd_chunks.plugins"}


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str | None):
        self._target = target
        self._real_spec = None

    def create_module(self, spec):
        if self._target is None:
            return None
        module = importlib.import_module(self._target)
        self._real_spec = module.__spec__
        return module

    def exec_module(self, module):
        if self._target is None:
            module.__doc__ = "Removed; its modules moved. See usd_chunks._moved."
            return
        # The import machinery stamps the alias spec onto the module it was handed; put
        # the real one back so reload() and __spec__.name still name the new module.
        module.__spec__ = self._real_spec

    def get_code(self, fullname):
        """For ``python -m <old name>``: runpy runs the new module's code."""
        loader = importlib.util.find_spec(self._target).loader
        return loader.get_code(self._target)


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in _EMPTY_PACKAGES:
            return importlib.machinery.ModuleSpec(fullname, _AliasLoader(None), is_package=True)
        if fullname in MOVED:
            return importlib.util.spec_from_loader(fullname, _AliasLoader(MOVED[fullname]))
        return None


def install() -> None:
    """Put the alias finder first, so a stale __pycache__ at an old path cannot win."""
    if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _AliasFinder())
