"""A network brought in by composition is tweaked, never rewritten.

The workflow: a sequence file holds a chunk network (a room layout, say), and a shot file
composes it in -- sublayered, so the chunks appear as the shot's own graph, or referenced
under a chunkgroup, so they appear as a diveable node. The shot then nudges a param. What
must happen is what USD does everywhere else: the sequence file is read-only, the nudge is
a sparse `over` in the shot, and saving the shot keeps the arc so the sequence stays live
underneath.

Three places wanted otherwise, and each is pinned here:

- opening the shot re-homed the sequence's chunk prims out of the sublayered file and into
  the session layer (_migrate_chunk_bookkeeping deletes the source spec), gutting the
  sequence in the Sdf layer cache;
- editing a param wrote back into the layer holding the strongest existing spec
  (get_attr_home_layer), i.e. the sequence file, or an invalid target across a reference;
- saving inlined every composed chunk as a full `def` and dropped the root layer's
  subLayers -- and crashed outright on a referenced chunk, whose spec exists in no local
  layer to copy from.

The lock the node graph draws on an external-layer node is not touched: it still pins the
node's position, but its params are meant to be edited, and this is where those edits go.
"""
from __future__ import annotations

import os

import pytest
from pxr import Sdf, Usd

from graph_fixtures import _Scene, make_network
from usd_chunks import chunk_registry
from usd_chunks.chunk_stage import (
    export_chunks, initialize, param_override_source, revert_param_override,
)

_TX = "inputs:translate_grp:tx"
_SEQ_VALUE = 1.0
_SHOT_VALUE = 3.5


def _write_sequence(tmp_path) -> str:
    """A two-transform network with a non-default tx on the first, so the shot's edit
    overrides an opinion the sequence actually holds rather than filling in a default."""
    seq, context = make_network(tmp_path / "seq.usda")
    stage = Usd.Stage.Open(seq)
    prim = stage.GetPrimAtPath(f"{context}/Transform")
    prim.CreateAttribute(_TX, Sdf.ValueTypeNames.Float).Set(_SEQ_VALUE)
    stage.GetRootLayer().Save()
    return seq


def _write_shot(tmp_path, seq: str, arc: str) -> tuple[str, str]:
    """The shot file and the path the sequence's first Transform composes at in it."""
    shot = str(tmp_path / "shot.usda")
    if arc == "sublayer":
        layer = Sdf.Layer.CreateNew(shot)
        layer.subLayerPaths.append("./seq.usda")
        layer.Save()
        return shot, "/chunks/ChunkGroup/Transform"
    with open(shot, "w") as fh:
        fh.write(
            "#usda 1.0\n"
            'def Scope "chunks"\n{\n'
            '    def Chunk "Bedroom" (references = @./seq.usda@</chunks/ChunkGroup>)\n'
            "    {\n    }\n}\n"
        )
    return shot, "/chunks/Bedroom/Transform"


def _open(shot: str):
    """What every frontend does to open a file. initialize() runs load_chunks itself."""
    stage = Usd.Stage.Open(shot)
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    initialize(stage, scene)
    return stage, scene


def _chunk_defs(layer: Sdf.Layer) -> list[str]:
    """Paths of every Chunk `def` authored in this one layer."""
    found = []

    def visit(spec):
        if spec.typeName == "Chunk" and spec.specifier == Sdf.SpecifierDef:
            found.append(str(spec.path))
        for child in spec.nameChildren.values():
            visit(child)

    for root in layer.rootPrims.values():
        visit(root)
    return found


@pytest.fixture(params=["sublayer", "reference"])
def composed_shot(request, tmp_path):
    seq = _write_sequence(tmp_path)
    shot, transform_path = _write_shot(tmp_path, seq, request.param)
    stage, scene = _open(shot)
    seq_layer = Sdf.Layer.Find(seq)
    assert seq_layer is not None, "the sequence should be open as part of the shot"
    try:
        yield request.param, stage, scene, seq_layer, shot, transform_path
    finally:
        for chunk in list(scene.get_chunks()):
            chunk.request_delete()


class TestOpeningLeavesTheSequenceAlone:
    def test_the_sequence_layer_is_not_dirtied_by_the_open(self, composed_shot):
        _, _, _, seq_layer, _, _ = composed_shot
        assert not seq_layer.dirty

    def test_the_sequence_keeps_its_chunk_defs(self, composed_shot):
        """The bug: a load consolidated bookkeeping into the session layer by deleting the
        original spec, so the sublayered sequence lost every chunk it contributed."""
        _, _, _, seq_layer, _, _ = composed_shot
        assert sorted(_chunk_defs(seq_layer)) == [
            "/chunks/ChunkGroup",
            "/chunks/ChunkGroup/Transform",
            "/chunks/ChunkGroup/Transform1",
        ]

    def test_the_shot_sees_the_sequence_value(self, composed_shot):
        _, stage, _, _, _, transform_path = composed_shot
        assert stage.GetAttributeAtPath(f"{transform_path}.{_TX}").Get() == _SEQ_VALUE


def _tx_param(scene):
    transform = next(c for c in scene.get_chunks() if c.name == "Transform")
    return transform.get_param("tx")


class TestEditingWritesAnOverIntoTheShot:
    def test_the_value_composes(self, composed_shot):
        _, stage, scene, _, _, transform_path = composed_shot
        _tx_param(scene).value = _SHOT_VALUE
        assert stage.GetAttributeAtPath(f"{transform_path}.{_TX}").Get() == _SHOT_VALUE

    def test_the_sequence_layer_is_untouched(self, composed_shot):
        """The bug: get_attr_home_layer answered with the layer holding the strongest
        existing spec, which for a composed-in chunk is the sequence file."""
        _, _, scene, seq_layer, _, _ = composed_shot
        _tx_param(scene).value = _SHOT_VALUE
        assert not seq_layer.dirty
        spec = seq_layer.GetAttributeAtPath(f"/chunks/ChunkGroup/Transform.{_TX}")
        assert spec.default == _SEQ_VALUE

    def test_the_override_lives_in_a_layer_the_shot_owns(self, composed_shot):
        _, stage, scene, seq_layer, _, transform_path = composed_shot
        _tx_param(scene).value = _SHOT_VALUE
        attr = stage.GetAttributeAtPath(f"{transform_path}.{_TX}")
        strongest = attr.GetPropertyStack(Usd.TimeCode.Default())[0]
        assert strongest.layer != seq_layer
        assert strongest.layer.anonymous or strongest.layer == stage.GetRootLayer()
        assert strongest.default == _SHOT_VALUE


def _add(scene, module_name):
    module = chunk_registry.ChunkRegistry().get_chunk_module(module_name)
    assert module is not None, f"chunk module {module_name!r} not found"
    chunk = (module.create() if module.loaded else module.load())[0]
    scene.add_chunk(chunk)
    return chunk


class TestTheOverrideFollowsTheHostEditTarget:
    """Where the override goes: the stage's edit target -- the layer the host has open for
    editing, the shot file itself by default -- and not the chunk's own Edit Target, which
    for a sequence chunk is the sequence's, locked at that level."""

    def test_by_default_the_override_is_in_the_shot_file_itself(self, composed_shot):
        _, stage, scene, _, _, transform_path = composed_shot
        assert stage.GetEditTarget().GetLayer() == stage.GetRootLayer()
        _tx_param(scene).value = _SHOT_VALUE
        spec = stage.GetRootLayer().GetAttributeAtPath(f"{transform_path}.{_TX}")
        assert spec is not None and spec.default == _SHOT_VALUE

    def test_a_layout_layer_the_host_targets_takes_the_override(self, composed_shot, tmp_path):
        """The user is editing their layout layer: a file sublayered into the shot and
        made the stage's edit target. The nudge lands there and nowhere else."""
        _, stage, scene, seq_layer, _, transform_path = composed_shot
        layout = Sdf.Layer.CreateNew(str(tmp_path / "layout.usda"))
        stage.GetRootLayer().subLayerPaths.insert(0, "./layout.usda")
        stage.SetEditTarget(Usd.EditTarget(layout))

        _tx_param(scene).value = _SHOT_VALUE

        assert not seq_layer.dirty
        spec = layout.GetAttributeAtPath(f"{transform_path}.{_TX}")
        assert spec is not None and spec.default == _SHOT_VALUE
        assert stage.GetSessionLayer().GetAttributeAtPath(f"{transform_path}.{_TX}") is None
        assert stage.GetAttributeAtPath(f"{transform_path}.{_TX}").Get() == _SHOT_VALUE

    def test_a_shot_edit_target_chunk_upstream_does_not_capture_it(self, composed_shot):
        """Pinned on purpose: wiring a shot-made Layer -> Edit Target above the
        sequence's Transform does not redirect its overrides into that layer. The
        sequence chunk's Edit Target chain is the sequence's; shot overrides follow the
        host. And the wire's reconcile pass must not carry the override out of the shot
        file afterwards either."""
        _, stage, scene, seq_layer, _, transform_path = composed_shot
        create = _add(scene, "layer")
        create.get_param("layer_name").value = "layout"
        create.recook()
        layout = Sdf.Layer.Find(create.get_param("_new_layer_id").value)
        edit_target = _add(scene, "edit_target")
        edit_target.add_dependency(create)
        edit_target.get_param("edit_target").value = "layout"
        edit_target.recook()
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")

        transform.get_param("tx").value = _SHOT_VALUE
        transform.add_dependency(edit_target)

        assert not seq_layer.dirty
        assert layout.GetAttributeAtPath(f"{transform_path}.{_TX}") is None
        spec = stage.GetRootLayer().GetAttributeAtPath(f"{transform_path}.{_TX}")
        assert spec is not None and spec.default == _SHOT_VALUE


class TestSavingKeepsTheArc:
    def test_the_saved_shot_still_composes_the_sequence(self, composed_shot):
        arc, _, scene, _, shot, _ = composed_shot
        _tx_param(scene).value = _SHOT_VALUE
        export_chunks(scene, shot)
        text = open(shot).read()
        assert ("subLayers" if arc == "sublayer" else "references") in text
        assert "seq.usda" in text

    def test_the_saved_shot_holds_the_override_and_nothing_of_the_sequence(self, composed_shot):
        """The bug: export flattened the whole layer stack and copied every composed chunk
        as a full def -- the sequence inlined, its arc dropped, and a crash for a
        referenced chunk because no local layer had a spec to copy."""
        _, _, scene, _, shot, transform_path = composed_shot
        _tx_param(scene).value = _SHOT_VALUE
        export_chunks(scene, shot)
        saved = Sdf.Layer.FindOrOpen(shot)
        defs = _chunk_defs(saved)
        assert transform_path not in defs, "the sequence's chunk was inlined into the shot"
        assert not [d for d in defs if d.endswith("/Transform1")]
        assert saved.GetAttributeAtPath(f"{transform_path}.{_TX}").default == _SHOT_VALUE

    def test_reopening_the_saved_shot_composes_the_override_over_the_sequence(self, composed_shot):
        _, _, scene, _, shot, transform_path = composed_shot
        _tx_param(scene).value = _SHOT_VALUE
        export_chunks(scene, shot)
        reopened = Usd.Stage.Open(shot)
        prim = reopened.GetPrimAtPath(transform_path)
        assert prim.GetTypeName() == "Chunk", "the chunk should still come from the sequence"
        assert prim.GetAttribute(_TX).Get() == _SHOT_VALUE
        sibling = reopened.GetPrimAtPath(os.path.dirname(transform_path) + "/Transform1")
        assert sibling.GetTypeName() == "Chunk"


class TestKnowingAndRevertingAnOverride:
    def test_an_untouched_param_is_not_an_override(self, composed_shot):
        _, _, scene, _, _, _ = composed_shot
        assert param_override_source(_tx_param(scene)) is None

    def test_a_nudged_param_names_the_file_it_overrides(self, composed_shot):
        _, _, scene, seq_layer, _, _ = composed_shot
        _tx_param(scene).value = _SHOT_VALUE
        assert param_override_source(_tx_param(scene)) == seq_layer

    def test_revert_puts_the_sequence_value_back_and_authors_nothing(self, composed_shot):
        _, stage, scene, seq_layer, _, transform_path = composed_shot
        tx = _tx_param(scene)
        tx.value = _SHOT_VALUE
        revert_param_override(tx)
        assert tx.value == _SEQ_VALUE
        assert param_override_source(tx) is None
        attr = stage.GetAttributeAtPath(f"{transform_path}.{_TX}")
        assert attr.Get() == _SEQ_VALUE
        assert [s.layer for s in attr.GetPropertyStack(Usd.TimeCode.Default())] == [seq_layer]
        assert not seq_layer.dirty

    def test_a_shot_made_chunks_value_is_never_an_override(self, composed_shot):
        _, _, scene, _, _, _ = composed_shot
        create = _add(scene, "layer")
        create.get_param("layer_name").value = "mine"
        assert param_override_source(create.get_param("layer_name")) is None
