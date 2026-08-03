from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from plugins.chunk_usd import _author_prim, _CHUNKS_ROOT, _INPUTS_PREFIX
from usd_chunks import chunk, chunk_animation, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender

import scripts.transform as transform_mod


class _DummyScene(SceneSender):
    def __init__(self, stage=None):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


@pytest.fixture(autouse=True)
def reset_animation_manager():
    yield
    chunk_animation.ChunkAnimationManager().clear()


def _build_transform_chunk(stage, monkeypatch, target_path: str):
    """Build a real Transform chunk (via transform.main()), bind its params to `stage`,
    point it at target_path, and keep every relevant object alive for the test."""
    node = transform_mod.main()
    node._config = ChunkConfig(module="transform")
    node.get_param("primpath_location").value = target_path

    _author_prim(stage, node)
    prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Transform")
    for name in ("primpath_location", "translate_grp:tx", "translate_grp:ty", "translate_grp:tz",
                 "rotate_grp:rx", "rotate_grp:ry", "rotate_grp:rz",
                 "scale_grp:sx", "scale_grp:sy", "scale_grp:sz"):
        attr = prim.GetAttribute(f"{_INPUTS_PREFIX}{name}")
        if attr.IsValid():
            node.get_param(name.split(":")[-1] if ":" in name else name)._connect_param_attr(attr)

    scene = _DummyScene(stage)
    scene._node = node  # anchor a strong reference -- get_chunk() caches its owner as a weakref
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene)
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "initialized", True)
    return node


def test_execute_skips_entirely_once_animated_to_protect_the_bake(monkeypatch) -> None:
    """Regression test for the real live bug: execute() re-runs reactively on every
    scrub tick (ChunkAnimationManager.on_frame_changed pushes a new value into each
    animated slider, bubbling up through the DAG to here), and its own
    chunk_layer.Clear() would wipe bake()'s time samples on the very next scrub after
    baking if execute() didn't know to skip itself once the node is animated -- by the
    time the user scrolled to a later frame and clicked "Run Simulation", the bake would
    already be gone, replaced by a single plain Set() reflecting whichever frame the
    timeline happened to be sitting on."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/bowl")
    node = _build_transform_chunk(stage, monkeypatch, "/bowl")

    # Simulate bake() having already run: real per-frame time samples on the target's
    # xformOp:transform.
    xformable = UsdGeom.Xformable(stage.GetPrimAtPath("/bowl"))
    transform_op = xformable.AddTransformOp()
    transform_op.Set(Gf.Matrix4d(1), Usd.TimeCode(1))
    transform_op.Set(Gf.Matrix4d(1), Usd.TimeCode(24))
    assert transform_op.GetAttr().GetNumTimeSamples() == 2

    monkeypatch.setattr(chunk_animation, "chunk_has_animated_params", lambda n: True)

    transform_mod.execute(node)

    assert transform_op.GetAttr().GetNumTimeSamples() == 2, (
        "execute() must skip entirely once animated -- it must not Clear() the layer "
        "holding the real baked time samples"
    )


def test_execute_runs_normally_when_not_animated(monkeypatch) -> None:
    """Unchanged behavior for the common (non-animated) case: execute() still clears
    and re-authors a plain default-value xformOp:transform every reactive run."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/bowl")
    node = _build_transform_chunk(stage, monkeypatch, "/bowl")
    node.get_param("tx").value = 5.0

    monkeypatch.setattr(chunk_animation, "chunk_has_animated_params", lambda n: False)

    # execute()'s own chunk_layer.Clear() targets stage.GetEditTarget().GetLayer() -- in
    # real usage chunk_notice.py's callback dispatch always wraps execute() in
    # Usd.EditContext(stage, chunk.get_edit_context()) first, so Clear() only touches the
    # chunk's own isolated layer. Without that here, GetEditTarget() would fall back to
    # the stage's root layer and Clear() would wipe /bowl itself, not just this chunk's
    # own authored opinions.
    chunk_registry.ChunkLayerManager().initialize(stage)
    with Usd.EditContext(stage, chunk_registry.ChunkLayerManager().get_or_create_layer(node)):
        transform_mod.execute(node)

    attr = stage.GetPrimAtPath("/bowl").GetAttribute("xformOp:transform")
    assert attr.IsValid()
    assert attr.GetNumTimeSamples() == 0
    matrix = attr.Get()
    assert matrix is not None
    assert matrix.ExtractTranslation()[0] == pytest.approx(5.0)
