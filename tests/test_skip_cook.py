"""Skipping cooks no upstream change can reach (skip_cook, USD_CHUNKS_SKIP_COOKS=1).

A chunk cooks on a stage populated only at its inputs and its last outputs, so what it
read is known without anyone declaring it; what each cook changed is handed to the chunks
below. A chunk dirtied only by chunks above it skips its cook when none of those changes
touch what it reads -- and must end up exactly as if it had cooked.
"""

from __future__ import annotations

import pytest
from chunk_stubs import register_stub_module
from pxr import Sdf

from usd_chunks import Scene, chunk, skip_cook


@pytest.fixture(autouse=True)
def _skipping_on(monkeypatch):
    monkeypatch.setenv(skip_cook.ENV, "1")


def _add(scene, module):
    (c,) = module.load()
    scene.add_chunk(c)
    return c


def _generator(cooks: list, name: str):
    """Writes <path>.size -- reads nothing from the stage."""

    def main():
        c = chunk.Chunk(name)
        c.add_param(chunk.LineEdit("path", default_value=f"/{name}"))
        c.add_param(chunk.Slider("size", default_value=1.0))
        return c

    def execute(node):
        cooks.append(node.name)
        prim = node.get_stage().DefinePrim(node.get_param("path").value, "Xform")
        prim.CreateAttribute("size", Sdf.ValueTypeNames.Double).Set(node.get_param("size").value)

    return register_stub_module(main=main, execute=execute)


def _reader(cooks: list, name: str, *, declared: bool = True):
    """Writes /<name>.size as twice the size at its target.

    declared: the target is a PRIM_SELECTION param. Otherwise it reads /Gen by a path of
    its own -- something its inputs do not name.
    """

    def main():
        c = chunk.Chunk(name)
        flags = chunk.ChunkFlags.ParamFlags.PRIM_SELECTION if declared else chunk.ChunkFlags.ParamFlags(0)
        c.add_param(chunk.LineEdit("target", default_value="/Gen", flags=flags))
        return c

    def execute(node):
        cooks.append(node.name)
        stage = node.get_stage()
        source = stage.GetPrimAtPath(node.get_param("target").value)
        size = source.GetAttribute("size").Get() if source else None
        prim = stage.DefinePrim(f"/{name}", "Xform")
        prim.CreateAttribute("size", Sdf.ValueTypeNames.Double).Set((size or 0.0) * 2)

    return register_stub_module(main=main, execute=execute)


def _size(scene, path):
    return scene.get_stage().GetPrimAtPath(path).GetAttribute("size").Get()


def _chain(scene, *chunks):
    for c in chunks:
        # A PRIM_SELECTION param starts out as the host's selection, not its default.
        if (target := c.get_param("target")) is not None and not target.value:
            target.value = "/Gen"
    for upstream, downstream in zip(chunks, chunks[1:]):
        downstream.add_dependency(upstream)
    # The first cook after wiring runs masked and records what each chunk reads.
    chunks[0].get_param("size").value = 0.5


def test_a_chunk_below_that_reads_nothing_that_changed_is_skipped():
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        other = _add(scene, _generator(cooks, "Other"))
        _chain(scene, gen, other)
        cooks.clear()

        gen.get_param("size").value = 2.0

        assert cooks == ["Gen"]
        assert _size(scene, "/Gen") == 2.0
        assert _size(scene, "/Other") == 1.0


def test_a_chunk_that_reads_what_changed_cooks():
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        reader = _add(scene, _reader(cooks, "Reader"))
        _chain(scene, gen, reader)
        cooks.clear()

        gen.get_param("size").value = 3.0

        assert cooks == ["Gen", "Reader"]
        assert _size(scene, "/Reader") == 6.0


def test_a_skip_is_decided_on_everything_above_not_just_the_chunk_before():
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        other = _add(scene, _generator(cooks, "Other"))
        reader = _add(scene, _reader(cooks, "Reader"))
        _chain(scene, gen, other, reader)
        cooks.clear()

        gen.get_param("size").value = 4.0

        # Other is skipped; Reader, two below Gen, still sees Gen's change.
        assert cooks == ["Gen", "Reader"]
        assert _size(scene, "/Reader") == 8.0


def test_a_chunk_reading_outside_its_inputs_cooks_on_the_whole_stage():
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        reader = _add(scene, _reader(cooks, "Reader", declared=False))
        _chain(scene, gen, reader)

        gen.get_param("size").value = 5.0

        assert _size(scene, "/Reader") == 10.0
        assert reader._skip_mask is None
        cooks.clear()
        gen.get_param("size").value = 6.0
        assert cooks == ["Gen", "Reader"]
        assert _size(scene, "/Reader") == 12.0


def test_a_cook_that_writes_somewhere_new_is_run_again_where_it_can():
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        _chain(scene, gen)

        gen.get_param("path").value = "/Moved"

        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/Moved").GetAttribute("size").Get() == 0.5
        assert not stage.GetPrimAtPath("/Gen")
        assert Sdf.Path("/Moved") in gen._skip_mask


def test_disabling_a_chunk_above_reaches_what_reads_it():
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        reader = _add(scene, _reader(cooks, "Reader"))
        _chain(scene, gen, reader)
        assert _size(scene, "/Reader") == 1.0

        gen.set_state(chunk.ChunkFlags.State.DISABLED)

        assert _size(scene, "/Reader") == 0.0


def test_a_scene_change_outside_any_cook_is_not_skipped_past():
    """A Stage Expression writes its variables from a param callback, before any cook; a user
    mutes a layer. No cook above says so, and nothing below may skip on the old scene."""
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        other = _add(scene, _generator(cooks, "Other"))
        _chain(scene, gen, other)
        stage = scene.get_stage()
        stage.GetSessionLayer().expressionVariables = {"SHOT": "sh020"}
        cooks.clear()

        gen.get_param("size").value = 2.0

        assert cooks == ["Gen", "Other"]
        cooks.clear()
        gen.get_param("size").value = 3.0
        assert cooks == ["Gen"]


def test_a_value_written_before_the_cook_still_reaches_the_chunks_below():
    """A param bound to a prim attribute writes it the moment it changes, before any cook --
    a Light's intensity. Against the layer as the cook starts it is no change at all; against
    what the chunks below last saw, it is."""
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        reader = _add(scene, _reader(cooks, "Reader"))
        _chain(scene, gen, reader)
        layer = gen.get_edit_context()
        layer.GetAttributeAtPath("/Gen.size").default = 9.0

        gen.get_param("size").value = 9.0

        assert "Reader" in cooks
        assert _size(scene, "/Reader") == 18.0


def test_changed_paths_ignore_a_parent_only_gaining_a_child():
    before = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(before, "/World/A").specifier = Sdf.SpecifierDef
    after = skip_cook.snapshot(before)
    Sdf.CreatePrimInLayer(after, "/World/B").specifier = Sdf.SpecifierDef

    assert skip_cook.changed_paths(before, after) == {Sdf.Path("/World/B")}


def test_a_glob_covers_the_prims_under_its_fixed_part():
    assert set(skip_cook._paths_in("/World/*/Mesh, /Other")) == {Sdf.Path("/World"), Sdf.Path("/Other")}
    assert set(skip_cook._paths_in("a_collection")) == {Sdf.Path.absoluteRootPath}
