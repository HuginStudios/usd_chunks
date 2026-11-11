"""A recook changes only what changed in the chunk's layer (diff cook).

The layer is never cleared on the stage: the cook writes into a scratch layer and that is
reconciled into the chunk's layer (layer_reconcile). So a recook with the same result
sends no resync, and a moved value is an info-only change -- Hydra updates it in place
instead of building the prim again.
"""

from __future__ import annotations

from pxr import Gf, Sdf, Tf, Usd, UsdGeom

from usd_chunks import Scene, chunk_registry


class _Heard:
    def __init__(self, stage: Usd.Stage):
        self.resynced: list[Sdf.Path] = []
        self.changed: list[Sdf.Path] = []
        self._key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, self._on, stage)

    def _on(self, notice, _sender) -> None:
        self.resynced += notice.GetResyncedPaths()
        self.changed += notice.GetChangedInfoOnlyPaths()

    def clear(self) -> None:
        self.resynced.clear()
        self.changed.clear()


def _own(chunk) -> Sdf.Layer:
    return chunk_registry.ChunkLayerManager().get_layer(chunk)


def test_a_recook_with_the_same_result_resyncs_nothing():
    with Scene() as scene:
        mesh = scene.create.mesh(mesh_type="sphere", prim_path="/World/ball", radius=0.5)
        place = scene.create.transform(mesh, primpath_location="/World/ball", ty=1.0)
        scene.set_output(place)
        stage = scene.get_stage()
        before = _own(mesh).ExportToString()
        heard = _Heard(stage)
        mesh.execute()
        assert heard.resynced == []
        assert _own(mesh).ExportToString() == before


def test_a_moved_value_is_set_in_place():
    with Scene() as scene:
        mesh = scene.create.mesh(mesh_type="sphere", prim_path="/World/ball", radius=0.5)
        place = scene.create.transform(mesh, primpath_location="/World/ball", ty=1.0)
        scene.set_output(place)
        stage = scene.get_stage()
        heard = _Heard(stage)
        place.get_param("ty").value = 3.0
        assert heard.resynced == []
        assert heard.changed, "the move was heard"
        # The param's own value changes under /chunks too; of the scene, only the ball.
        scene_paths = {path.GetPrimPath() for path in heard.changed if not path.HasPrefix("/chunks")}
        assert scene_paths == {Sdf.Path("/World/ball")}
        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/World/ball"))
        assert xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation() == Gf.Vec3d(0, 3, 0)


def test_recooks_leave_the_same_layer():
    """Cooked again and again, the layer is what the first cook made: nothing piles up."""
    with Scene() as scene:
        mesh = scene.create.mesh(mesh_type="sphere", prim_path="/World/ball", radius=0.5)
        scene.set_output(mesh)
        stage = scene.get_stage()
        first = _own(mesh).ExportToString()
        for _ in range(3):
            mesh.execute()
        assert _own(mesh).ExportToString() == first
        assert stage.GetPrimAtPath("/World/ball").IsValid()


def test_the_same_as_clearing(monkeypatch):
    """The layer a diff cook leaves is the one a clear-and-rewrite cook writes."""
    def build():
        with Scene() as scene:
            mesh = scene.create.mesh(mesh_type="tube", prim_path="/World/pipe", radius=0.5)
            place = scene.create.transform(mesh, primpath_location="/World/pipe", tx=2.0)
            scene.set_output(place)
            place.get_param("tx").value = 4.0
            mesh.get_param("radius").value = 0.25
            return _own(mesh).ExportToString(), _own(place).ExportToString()

    diffed = build()
    monkeypatch.setenv("USD_CHUNKS_DIFF_COOK", "0")
    assert build() == diffed
