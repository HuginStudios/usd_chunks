"""
GraphHost — thin abstraction over the host application (usdview, Kraken, etc.).

Nodes and widgets call GraphHost.current() instead of importing usdview
internals directly, so the node graph runs in any host.
"""
from __future__ import annotations
from typing import Callable
from PySide6 import QtCore, QtGui


class GraphHost:
    """Abstract host interface. Subclass and call set_current() to register."""

    _current = None

    @classmethod
    def current(cls) -> GraphHost:
        if cls._current is None:
            cls._current = UsdviewGraphHost()
        return cls._current

    @classmethod
    def set_current(cls, host: GraphHost) -> None:
        cls._current = host

    # ── Stage ──────────────────────────────────────────────────────────────

    def get_stage(self) -> object | None:
        """Return the active Usd.Stage, or None."""
        return None

    # ── Selection ──────────────────────────────────────────────────────────

    def get_selection(self) -> list:
        """Return list of currently selected Usd.Prim objects."""
        return []

    def set_selection(self, prim: object) -> None:
        """Set the host selection to a single Usd.Prim."""
        pass

    # ── Viewport ───────────────────────────────────────────────────────────

    def get_viewport_screenshot(self) -> QtGui.QPixmap | None:
        """Return a QPixmap of the current viewport, or None."""
        return None

    def frame_prim(self, prim: object, on_ready: Callable | None = None) -> None:
        """
        Frame prim in the viewport.  When the frame is ready, call
        on_ready(pixmap) where pixmap is a QPixmap (may be None on failure).
        Camera state is restored after the grab.
        """
        if on_ready:
            on_ready(None)

    def update_viewport(self) -> None:
        """Force a viewport redraw."""
        pass


class UsdviewGraphHost(GraphHost):
    """Usdview implementation of GraphHost."""

    def _get_scene_ctrl(self) -> tuple[object, object]:
        try:
            from usd_chunks import chunk_registry
            scene = chunk_registry.ChunkRegistry().scene
            if not scene:
                return None, None
            ctrl = scene.usdviewAPI._UsdviewApi__appController
            return scene, ctrl
        except Exception:
            return None, None

    def get_stage(self) -> object | None:
        scene, _ctrl = self._get_scene_ctrl()
        if scene:
            try:
                return scene.get_stage()
            except Exception:
                pass
        return None

    def get_selection(self) -> list:
        scene, _ctrl = self._get_scene_ctrl()
        if scene:
            try:
                return scene.get_selected_prims() or []
            except Exception:
                pass
        return []

    def set_selection(self, prim: object) -> None:
        _scene, ctrl = self._get_scene_ctrl()
        if ctrl and prim:
            try:
                ctrl._dataModel.selection.setPrim(prim)
            except Exception:
                pass

    def get_viewport_screenshot(self) -> QtGui.QPixmap | None:
        _scene, ctrl = self._get_scene_ctrl()
        if not ctrl:
            return None
        try:
            sv = ctrl._stageView
            try:
                return sv.grabFramebuffer()
            except Exception:
                return sv.grab()
        except Exception:
            return None

    def frame_prim(self, prim: object, on_ready: Callable | None = None) -> None:
        _scene, ctrl = self._get_scene_ctrl()
        if not ctrl or not prim:
            if on_ready:
                on_ready(None)
            return
        try:
            sv = ctrl._stageView
            old_state = sv.copyViewState()
            ctrl._dataModel.selection.setPrim(prim)
            ctrl._frameSelection()

            def _do_grab():
                px = None
                try:
                    px = sv.grab()
                except Exception:
                    pass
                try:
                    sv.restoreViewState(old_state)
                    sv.updateView()
                except Exception:
                    pass
                if on_ready:
                    on_ready(px)

            QtCore.QTimer.singleShot(250, _do_grab)
        except Exception:
            if on_ready:
                on_ready(None)

    def update_viewport(self) -> None:
        _scene, ctrl = self._get_scene_ctrl()
        if ctrl:
            try:
                ctrl._stageView.updateView(resetCam=False, forceComputeBBox=True)
            except Exception:
                pass
