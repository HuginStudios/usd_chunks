"""One transform chunk, several target prims, one handle.

`chunk_utils.author_in_variant_contexts` lets a chunk drive many prims while `anchor` is
singular in the declaration, so the resolved anchor has to carry all of them. The decision is **one
handle at the averaged pivot**, not one handle per target -- `usdviewTools/gizmo` already
does bbox-centroid offsets for multi-prim selection, so the pivot itself is the viewport
layer's job. What this repo owes it is the full target list and a single set of params.

Not an edge case: a transform chunk pointed at two prims is ordinary use, so anchoring to
the first target would be visibly wrong rather than merely incomplete.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_registry
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import _CHUNKS_ROOT, _author_prim


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

transform_mod = importlib.import_module("chunks.transforms.transform")


def _stage_with_cubes(*paths: str) -> Usd.Stage:
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        for path in paths:
            UsdGeom.Cube.Define(stage, path)

    class _StagedScene(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_StagedScene(), "pytest")
    return stage


def _transform_chunk(target: str):
    c = transform_mod.main()
    c._config = ChunkConfig(module="transform", gizmos=transform_mod.config().gizmos)
    c.get_param("primpath_location").value = target
    return c


def _moved_by(x, y, z) -> Gf.Matrix4d:
    """A transform handle's payload is a whole matrix -- the host ran its own manipulator."""
    xform = Gf.Transform()
    xform.SetTranslation(Gf.Vec3d(x, y, z))
    return xform.GetMatrix()


def _authored(stage, c):
    _author_prim(stage, c)
    return stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/{c.name}")


# ---------------------------------------------------------------------------
# the anchor carries every target
# ---------------------------------------------------------------------------

def test_anchor_carries_all_targets() -> None:
    stage = _stage_with_cubes("/World/A", "/World/B", "/World/C")
    prim = _authored(stage, _transform_chunk("/World/A,/World/B,/World/C"))

    targets = prim.GetRelationship("ui:gizmo:transform:anchor").GetTargets()
    assert [str(t) for t in targets] == ["/World/A", "/World/B", "/World/C"]


def test_a_glob_expands_to_every_match() -> None:
    """The path param takes Houdini-style globs, so the anchor has to survive one."""
    stage = _stage_with_cubes("/World/A", "/World/B")
    prim = _authored(stage, _transform_chunk("/World/*"))

    targets = prim.GetRelationship("ui:gizmo:transform:anchor").GetTargets()
    assert sorted(str(t) for t in targets) == ["/World/A", "/World/B"]


def test_target_count_does_not_multiply_the_handles() -> None:
    """Three targets is still one handle per group at the averaged pivot -- not one handle
    per target fighting for the same drag. The chunk declares three (translate, rotate,
    scale); the number of prims it points at must not change that."""
    stage = _stage_with_cubes("/World/A", "/World/B", "/World/C")
    prim = _authored(stage, _transform_chunk("/World/A,/World/B,/World/C"))

    descs = chunk_gizmo.resolve_gizmos(prim)
    assert [d.name for d in descs] == ["transform"]
    assert len(descs[0].anchor) == 3


def test_desc_exposes_the_full_target_list() -> None:
    """The host computes the pivot, so it needs all of them -- and a plain tuple of Sdf.Path
    is all it should need."""
    stage = _stage_with_cubes("/World/A", "/World/B")
    prim = _authored(stage, _transform_chunk("/World/A,/World/B"))

    desc = chunk_gizmo.resolve_gizmos(prim)[0]
    assert isinstance(desc.anchor, tuple)
    assert all(isinstance(p, Sdf.Path) for p in desc.anchor)


def test_single_target_is_unchanged() -> None:
    stage = _stage_with_cubes("/World/A")
    prim = _authored(stage, _transform_chunk("/World/A"))

    desc = chunk_gizmo.resolve_gizmos(prim)[0]
    assert [str(p) for p in desc.anchor] == ["/World/A"]


def test_duplicate_targets_collapse() -> None:
    """Two entries naming one prim is one place to draw, not two."""
    stage = _stage_with_cubes("/World/A")
    prim = _authored(stage, _transform_chunk("/World/A,/World/A"))

    assert len(chunk_gizmo.resolve_gizmos(prim)[0].anchor) == 1


# ---------------------------------------------------------------------------
# targets that do not resolve
# ---------------------------------------------------------------------------

def test_a_missing_target_does_not_kill_the_gizmo() -> None:
    """Targets come and go as upstream chunks cook. Losing one must cost that one target,
    not the whole handle."""
    stage = _stage_with_cubes("/World/A")
    prim = _authored(stage, _transform_chunk("/World/A,/World/Ghost"))

    descs = chunk_gizmo.resolve_gizmos(prim)
    assert descs, "losing one target must not lose the handles"
    assert all("/World/A" in [str(p) for p in d.anchor] for d in descs)


def test_a_target_that_does_not_exist_yet_is_still_authored() -> None:
    """A chunk prim is authored before the upstream chunk that creates its target has cooked.
    A rel to a not-yet-existing prim heals when it appears; an anchor we declined to author
    never comes back on its own."""
    stage = _stage_with_cubes()
    prim = _authored(stage, _transform_chunk("/World/NotYet"))

    targets = prim.GetRelationship("ui:gizmo:transform:anchor").GetTargets()
    assert [str(t) for t in targets] == ["/World/NotYet"]


def test_garbage_in_the_path_param_does_not_raise() -> None:
    """The anchor trusts whatever the path param resolved, exactly as the chunk's own cook
    does -- we are not a second, stricter validator of the same field. Nonsense in it yields
    nonsense targets that resolve to no prim, which is the same thing the cook already
    silently skips. What must not happen is an exception taking the handle down."""
    stage = _stage_with_cubes("/World/A")
    prim = _authored(stage, _transform_chunk("/World/A,not a path,,,"))

    targets = [str(t) for t in prim.GetRelationship("ui:gizmo:transform:anchor").GetTargets()]
    assert "/World/A" in targets
    assert all(Sdf.Path(t).IsAbsolutePath() and Sdf.Path(t).IsPrimPath() for t in targets)


def test_a_relative_path_never_reaches_the_anchor() -> None:
    """A rel target has to be an absolute prim path; a relative one would be resolved against
    the *chunk* prim under /chunks, pointing the handle at something meaningless."""
    stage = _stage_with_cubes("/World/A")
    c = _transform_chunk("/World/A")
    prim = _authored(stage, c)

    c.get_param("primpath_location").value = "some/relative/path"
    chunk_gizmo.refresh_gizmo_anchors(stage, prim, c)

    for target in prim.GetRelationship("ui:gizmo:transform:anchor").GetTargets():
        assert target.IsAbsolutePath()


# ---------------------------------------------------------------------------
# write-back is per chunk, not per target
# ---------------------------------------------------------------------------

def test_write_back_sets_the_params_once_regardless_of_target_count() -> None:
    """The params belong to the chunk, not to each prim. The pivot only decides where the
    handle is drawn and what the delta is measured against."""
    _stage_with_cubes("/World/A", "/World/B", "/World/C")
    c = _transform_chunk("/World/A,/World/B,/World/C")

    chunk_gizmo.apply_bindings(c, "transform", _moved_by(4.0, 0.0, 0.0))

    assert c.get_param("tx").value == pytest.approx(4.0)


def test_every_target_moves_on_the_cook() -> None:
    stage = _stage_with_cubes("/World/A", "/World/B")
    c = _transform_chunk("/World/A,/World/B")

    chunk_gizmo.apply_bindings(c, "transform", _moved_by(0.0, 6.0, 0.0))

    class _FakeNode:
        def get_stage(self):
            return stage

        def get_param(self, name):
            return c.get_param(name)

    transform_mod.execute(_FakeNode())

    for path in ("/World/A", "/World/B"):
        matrix = UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetLocalTransformation()
        assert matrix.ExtractTranslation() == Gf.Vec3d(0.0, 6.0, 0.0), path


# ---------------------------------------------------------------------------
# keeping the anchor fresh
# ---------------------------------------------------------------------------

def test_repointing_the_param_re_resolves_the_anchor() -> None:
    stage = _stage_with_cubes("/World/A", "/World/B")
    c = _transform_chunk("/World/A")
    prim = _authored(stage, c)

    c.get_param("primpath_location").value = "/World/B"
    chunk_gizmo.refresh_gizmo_anchors(stage, prim, c)

    assert [str(t) for t in prim.GetRelationship("ui:gizmo:transform:anchor").GetTargets()] == ["/World/B"]


def test_widening_the_param_adds_targets() -> None:
    stage = _stage_with_cubes("/World/A", "/World/B")
    c = _transform_chunk("/World/A")
    prim = _authored(stage, c)

    c.get_param("primpath_location").value = "/World/A,/World/B"
    chunk_gizmo.refresh_gizmo_anchors(stage, prim, c)

    assert len(prim.GetRelationship("ui:gizmo:transform:anchor").GetTargets()) == 2


def test_refresh_is_a_no_op_for_a_chunk_without_gizmos() -> None:
    """It hangs off the generic param-changed path, so the overwhelmingly common case --
    a chunk with no gizmos at all -- has to cost nothing and touch nothing."""
    stage = _stage_with_cubes("/World/A")
    c = chunk.Chunk("Plain")
    c._config = ChunkConfig(module="prim")
    prim = _authored(stage, c)

    chunk_gizmo.refresh_gizmo_anchors(stage, prim, c)

    assert not [a for a in prim.GetAttributes() if a.GetName().startswith("ui:gizmo:")]
