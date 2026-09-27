"""The Light chunk's config() lists one searchable menu entry per light type.

Creating "Dome Light" must yield a DomeLight prim. It did not while MenuEntry.create
applied its preset with a cook: the chunk cooked before it was in any context, into a layer
the stage had not adopted, and the real cook on add was then skipped by light.py's own
'nothing changed' guard -- the right menu value, and no prim at all.
"""
from __future__ import annotations

from pxr import Usd, UsdLux

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks import chunk_graph


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False



def test_a_light_type_menu_entry_creates_that_light(qapp):
    """config() lists one searchable entry per light type. Creating "Dome Light" must
    yield a DomeLight prim -- which it did not while MenuEntry.create applied its preset
    with a cook: the chunk cooked before it was in any context, and the real cook on add
    was then skipped by light.py's own 'nothing changed' guard, leaving no prim at all."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    entries = registry.get_menu_entries()
    labels = {e.label for e in entries}
    assert {"Dome Light", "Rect Light", "Disk Light", "Distant Light", "Sphere Light", "Cylinder Light"} <= labels
    hits = chunk_registry.search_menu_entries(entries, "dome")
    entry = next(e for e in hits if e.label == "Dome Light")

    created, error = chunk_graph.create_from_entry(scene, entry, (0.0, 0.0))
    qapp.processEvents()
    assert not error
    node = created[0]
    assert node.get_param("light_type").value == "DomeLight"
    prim = stage.GetPrimAtPath(node.get_param("primpath_location").value)
    assert prim and prim.IsA(UsdLux.DomeLight), "the entry's preset must reach the prim, not just the menu"
