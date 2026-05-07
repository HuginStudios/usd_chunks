"""
ChunkAdapter — bridges chunks into the node graph.
"""
from __future__ import annotations

from usd_chunks import chunk, chunk_core, chunk_notice, chunk_registry


class ChunkAdapter:
    """Wraps a loaded chunk so it can be driven from the node graph."""

    def __init__(self, name: str, module_name: str = "", auto_create: bool = False, chunk=None):
        # ``chunk`` lets callers bind a specific chunk instance directly, bypassing the
        # name-based scene lookup. This is how node-graph chunk nodes give each node its
        # own distinct chunk instead of all sharing one-per-type (the first hit by name).
        self._name = name
        self._module_name = module_name
        self._chunk = chunk
        if chunk is None:
            self._find_chunk(auto_create)
        else:
            # Keep the adapter's display name in sync with the bound chunk so later
            # lookups/log messages use the real scene-assigned name.
            try:
                self._name = chunk.name
            except Exception:
                pass

    def _find_chunk(self, auto_create: bool = False) -> None:
        scene = chunk_registry.ChunkRegistry().scene
        if not scene:
            return
        # Primary lookup by name via scene API.
        self._chunk = scene.get_chunk(self._name)
        if not self._chunk:
            # Secondary scan: scene.get_chunk() may use a different key than display name.
            for c in scene.get_chunks():
                if c.name == self._name:
                    self._chunk = c
                    break
        if not self._chunk and auto_create and self._module_name:
            created = chunk.create_chunks(self._module_name)
            if created:
                self._chunk = created[0]

    @property
    def chunk(self):
        return self._chunk

    def get_params(self) -> list:
        """Return all drivable params (excludes Button, Separator, Label, Custom)."""
        if not self.chunk:
            return []
        result = []
        for p in self.chunk.get_params():
            if isinstance(p, chunk_core.ChunkVectorBase):
                result.append(p)
            elif isinstance(p, chunk_core.ChunkParamBase):
                if not isinstance(p, (chunk.Button, chunk.Separator, chunk.Label, chunk.Custom)):
                    result.append(p)
        return result

    def set_param(self, name: str, value) -> None:
        """Write *value* into the named chunk param.

        '@...' strings install a pull-only reference binding instead of a concrete
        value — no notice edges are created.  Passing a concrete value clears any
        prior reference.  Type conversion is attempted once if the initial assign
        fails.
        """
        if not self.chunk:
            return
        p = self.chunk.get_param(name)
        if p is None:
            return

        if isinstance(value, str) and value.startswith("@") and hasattr(p, "value_reference"):
            try:
                p.value_reference = value
            except Exception:
                pass
            return

        if hasattr(p, "value_reference") and p.value_reference:
            try:
                p.value_reference = None
            except Exception:
                pass

        if isinstance(p, chunk_core.ChunkVectorBase) and isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                if i < p.length:
                    p[i].value = v
        elif hasattr(p, "value"):
            try:
                p.value = value
            except (ValueError, TypeError):
                try:
                    p.value = type(p.value)(value) if p.value is not None else value
                except Exception:
                    pass

    def press_button(self, button_name: str | None = None) -> None:
        """Fire the named button (or the first Button param if *button_name* is None)."""
        if not self.chunk:
            return
        btn = None
        if button_name:
            btn = self.chunk.get_param(button_name)
        else:
            for p in self.chunk.get_params(chunk.is_param):
                if isinstance(p, chunk.Button):
                    btn = p
                    break
        if btn:
            chunk_notice.send_notice(
                chunk_notice.ChunkNoticeAlias.PARAM_CHANGED,
                sender=btn, data=btn
            )

    @staticmethod
    def list_available() -> list[str]:
        """Return display names of all chunks currently loaded in the scene."""
        scene = chunk_registry.ChunkRegistry().scene
        if not scene:
            return []
        return [c.name for c in scene.get_chunks()]
