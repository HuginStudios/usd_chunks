"""
GraphHost — thin abstraction over the host application (usdview, Kraken, etc.).

Nodes and widgets call GraphHost.current() instead of importing usdview
internals directly, so the node graph runs in any host.
"""

from __future__ import annotations
from typing import Callable
from PySide6 import QtGui


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

    # ── Camera ─────────────────────────────────────────────────────────────

    def set_camera_prim(self, prim: object) -> None:
        """Set the active camera to the given prim."""
        pass

    # ── Timeline ───────────────────────────────────────────────────────────

    def get_current_frame(self) -> float:
        """Return the current frame/time code."""
        return 0.0

    def refresh_timeline(self, start: float, end: float) -> None:
        """Update the host's timeline range and enable playback."""
        pass

    # ── Subscriptions ──────────────────────────────────────────────────────

    def subscribe_selection_changed(self, callback: Callable) -> Callable | None:
        """Subscribe to selection changes. Returns an unsubscribe callable, or None."""
        return None

    def subscribe_frame_changed(self, callback: Callable) -> Callable | None:
        """Subscribe to frame/time changes. Returns an unsubscribe callable, or None."""
        return None


class UsdviewGraphHost(GraphHost):
    """Usdview implementation of GraphHost — delegates all calls to UsdviewScene.

    UsdviewScene (a SceneSender subclass in plugins/chunk_usdview) owns all
    DCC-specific logic. This class is a thin bridge so the node graph can call
    GraphHost.current() without importing usdview internals directly.
    """

    def _scene(self):
        try:
            from usd_chunks import chunk_registry

            return chunk_registry.ChunkRegistry().scene
        except Exception:
            return None

    def get_stage(self) -> object | None:
        s = self._scene()
        try:
            return s.get_stage() if s else None
        except Exception:
            return None

    def get_selection(self) -> list:
        s = self._scene()
        try:
            return s.get_selected_prims() or [] if s else []
        except Exception:
            return []

    def set_selection(self, prim: object) -> None:
        if s := self._scene():
            s.set_selection(prim)

    def get_viewport_screenshot(self) -> QtGui.QPixmap | None:
        if s := self._scene():
            return s.get_viewport_screenshot()
        return None

    def frame_prim(self, prim: object, on_ready: Callable | None = None) -> None:
        if s := self._scene():
            s.frame_prim(prim, on_ready)
        elif on_ready:
            on_ready(None)

    def update_viewport(self) -> None:
        if s := self._scene():
            s.update_viewport()

    def set_camera_prim(self, prim: object) -> None:
        if s := self._scene():
            s.set_camera_prim(prim)

    def get_current_frame(self) -> float:
        if s := self._scene():
            return s.get_current_frame()
        return 0.0

    def refresh_timeline(self, start: float, end: float) -> None:
        if s := self._scene():
            s.refresh_timeline(start, end)

    def subscribe_selection_changed(self, callback: Callable) -> Callable | None:
        if s := self._scene():
            return s.subscribe_selection_changed(callback)
        return None

    def subscribe_frame_changed(self, callback: Callable) -> Callable | None:
        if s := self._scene():
            return s.subscribe_frame_changed(callback)
        return None
