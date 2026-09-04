"""Duplicating a Duplicate Prim + Transform pair makes a second prim, not a fight over one.

The end-to-end shape behind ParamFlags.PRIM_TARGET, and the reason a wire matters here.

Duplicate Prim's dest_path is a write target, so a copy is given a free path. A Transform's
primpath_location is a READ target and is deliberately left alone -- moving it would aim the
copy at a prim that does not exist. So a Transform whose path was TYPED still points at the
original's prim after the group is duplicated, and the copy transforms the wrong thing.

Wiring the Transform to the Duplicate Prim's destination instead of typing it is what makes
the pair travel together: the expression resolves against the copy's own Duplicate Prim, so
the Transform follows the bumped path with nothing to update by hand.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from test_copy_paste import _DummyScene, _mk, _pump
from usd_chunks import chunk_registry
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init


@pytest.fixture()
def graph(qapp):
    """Function-scoped, unlike the one in test_copy_paste this borrows its helpers from.

    Each test here duplicates a pair and inspects the result, so a scene carrying the
    previous test's chunks makes the selection ambiguous. Shared scope was tried and gave
    exactly that: every test passing alone and two failing together.
    """
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()
    yield qapp, reg, scene, docker
    docker.close()

def _build(graph, transform_path, root):
    SRC = f"/Kitchen_set/{root}"
    TAKEN = SRC + "_copy1"
    qapp, reg, scene, docker = graph
    dup = _mk(reg.get_chunk_module("core_usd.duplicate_prim"), scene)
    tr = _mk(reg.get_chunk_module("transform"), scene)
    _pump(qapp, 0.6)

    gv = docker.tab_widget.get_current_graph_view()
    stage = gv.scene()._stage
    UsdGeom.Xform.Define(stage, SRC)
    UsdGeom.Xform.Define(stage, TAKEN)      # the prim this pair already made

    dup.get_param("source_prim").set_prims([stage.GetPrimAtPath(SRC)])
    dup.get_param("dest_path").value = TAKEN
    if transform_path == "WIRE":
        # The chunk name of the Duplicate Prim in this scene, so the expression resolves.
        tr.get_param("primpath_location").value = f"@{dup.name}.dest_path@"
    else:
        tr.get_param("primpath_location").value = TAKEN
    _pump(qapp, 0.3)

    scene.set_selected_chunks([dup, tr])
    gv._duplicate_selected()
    _pump(qapp, 0.6)

    copy_dest = copy_tr = None
    for c in scene.get_selected_chunks():
        if c.get_param("dest_path") is not None:
            copy_dest = c.get_param("dest_path").value
        elif c.get_param("primpath_location") is not None:
            copy_tr = c.get_param("primpath_location").value
    return copy_dest, copy_tr, TAKEN


def test_the_copy_duplicates_to_a_free_path(graph) -> None:
    copy_dest, _, TAKEN = _build(graph, None, "PotA")

    assert copy_dest and copy_dest != TAKEN, (
        f"the copy still writes to {TAKEN!r}; it would override the first pair's prim "
        "instead of making a second one"
    )


def test_a_typed_transform_path_does_not_follow(graph) -> None:
    """Pinned, because it is the reason to wire rather than type -- not a bug to fix here.

    Moving a read target is how a Transform ends up pointing at a prim that does not exist.
    """
    copy_dest, copy_tr, TAKEN = _build(graph, None, "PotB")

    assert copy_tr == TAKEN, "a typed path is left exactly as typed"
    assert copy_tr != copy_dest, (
        "if a typed path tracked the copy's destination, every read target would have to, "
        "and duplicating a Transform would silently retarget it"
    )


def test_a_wired_transform_follows_the_copy(graph) -> None:
    """Wire it and the pair travels together, which is the workflow to use."""
    copy_dest, copy_tr, TAKEN = _build(graph, "WIRE", "PotC")

    assert copy_tr == copy_dest, (
        f"the copy's Transform resolved to {copy_tr!r} but its Duplicate Prim writes to "
        f"{copy_dest!r}; the wire should make it follow"
    )
    assert copy_tr != TAKEN, "and it should not still be on the original's prim"
