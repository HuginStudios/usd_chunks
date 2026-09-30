"""save and export work on any scene, not only on a script's own Scene.

In usdview, Scene.current() is usdview's scene, and `scene.save(...)` raised
AttributeError: the methods were only on the script's Scene class.
"""

from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks import Scene, chunk_naming, chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender


class _Host(SceneSender):
    """A host's scene, like usdview's: a SceneSender that is not a Scene."""

    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def test_save_and_export_on_a_host_scene(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    host = _Host(stage)
    chunk_registry.ChunkRegistry().initialize(host, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, host)
    try:
        scene = Scene.current()
        assert scene is host and not isinstance(scene, Scene)

        box = scene.create.cube(prim_path="/geo/box")
        scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)
        scene.save(str(tmp_path / "network.usda"))
        scene.export(str(tmp_path / "result.usda"))
        scene.commit()

        network = Usd.Stage.Open(str(tmp_path / "network.usda"))
        chunks = [p.GetName() for p in network.TraverseAll()
                  if p.GetTypeName() == chunk_naming.CHUNK_PRIM_TYPENAME]
        assert "Transform" in chunks

        result = Usd.Stage.Open(str(tmp_path / "result.usda"))
        box_prim = result.GetPrimAtPath("/geo/box")
        moved = UsdGeom.Xformable(box_prim).ComputeLocalToWorldTransform(0).ExtractTranslation()
        assert moved[1] == 3.0
        assert not any(p.GetTypeName() == chunk_naming.CHUNK_PRIM_TYPENAME for p in result.TraverseAll())
    finally:
        for c in list(host.get_chunks()):
            c.request_delete()
        chunk_registry.ChunkLayerManager().reset()
