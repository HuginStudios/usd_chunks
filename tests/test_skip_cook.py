"""Skipping cooks no upstream change can reach (skip_cook).

A chunk cooks on a stage populated only at its inputs and its last outputs, so what it
read is known without anyone declaring it; what each cook changed is handed to the chunks
below. A chunk dirtied only by chunks above it skips its cook when none of those changes
touch what it reads -- and must end up exactly as if it had cooked.
"""

from __future__ import annotations

import pytest
from chunk_stubs import register_stub_module
from pxr import Sdf, UsdGeom

from usd_chunks import Scene, chunk, chunk_cook, chunk_registry, skip_cook
from usd_chunks.chunk_flags import ChunkFlags


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


def _rebuilder(cooks: list, name: str):
    """Writes /<name>.size, and adds a param to its own group on every cook, as a Light does
    when it shows its prim's attributes."""

    def main():
        c = chunk.Chunk(name)
        c.add_param(chunk.Group("shown"))
        return c

    def execute(node):
        cooks.append(node.name)
        prim = node.get_stage().DefinePrim(f"/{name}", "Xform")
        prim.CreateAttribute("size", Sdf.ValueTypeNames.Double).Set(1.0)
        shown = node.get_param("shown", chunk.is_group)
        shown.add_param(chunk.Slider(f"attr{len(cooks)}", default_value=0.0))

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


def test_the_first_edit_after_opening_skips_like_any_other(tmp_path):
    """Opening must leave nothing behind that makes the first edit cook more.

    The load restored Other's saved size -- a change of its own -- and cooked with solves
    held back, so the change stayed pending; and cooking the scene's chunkgroup, which has
    nothing to run, made its layer after Other had recorded the layer stack. Either one made
    the first edit after opening cook every chunk below the one edited, once.
    """
    cooks: list = []
    network = tmp_path / "network.usda"
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        other = _add(scene, _generator(cooks, "Other"))
        other.get_param("size").value = 3.0
        _chain(scene, gen, other)
        scene.set_output(other)
        scene.save(str(network))

    with Scene.open(str(network)) as scene:
        cooks.clear()

        scene.get_chunk("Gen").get_param("size").value = 2.0

        assert cooks == ["Gen"]
        assert _size(scene, "/Gen") == 2.0
        assert _size(scene, "/Other") == 3.0


def test_params_a_cook_adds_are_no_reason_to_cook_again():
    """A Light's first cook after a load rebuilds its params, and their GROUP_CHANGED stayed.

    The cook cleared its own node's dirty flag but not the reasons. Inside an edit's solve
    the solve cleared them; a cook outside one -- a View's recook, which usdview runs after
    every open -- left them, and the next edit handed them to skip_cook as a change of the
    chunk's own. In the kitchen scene every Light below an edited aim cooked on each step.
    """
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        shower = _add(scene, _rebuilder(cooks, "Shower"))
        _chain(scene, gen, shower)
        shower._cook()  # as recook_for_view cooks it: outside any edit
        assert not list(shower.get_dag_node().iter_changes()), "a cook takes in what it set on itself"
        cooks.clear()

        gen.get_param("size").value = 2.0

        assert cooks == ["Gen"], "Shower reads /Shower, which the edit did not touch"


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


def test_a_chunk_reading_outside_its_inputs_widens_its_mask_to_what_it_read():
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        reader = _add(scene, _reader(cooks, "Reader", declared=False))
        _chain(scene, gen, reader)

        gen.get_param("size").value = 5.0

        assert _size(scene, "/Reader") == 10.0
        assert Sdf.Path("/Gen") in skip_cook.record(reader).mask
        cooks.clear()
        gen.get_param("size").value = 6.0
        assert cooks == ["Gen", "Reader"]
        assert _size(scene, "/Reader") == 12.0


def test_a_variant_selection_names_the_prim_under_it_not_the_whole_stage():
    # As a chunk writes it -- a slash after the brace -- which Sdf.Path does not parse.
    assert list(skip_cook._paths_in("/Car{color=Red}/_materials/Paint")) == [
        Sdf.Path("/Car/_materials/Paint")]
    assert list(skip_cook._paths_in("/Car{color=Red}Body")) == [Sdf.Path("/Car/Body")]


def test_a_cook_that_writes_somewhere_new_is_run_again_where_it_can():
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        _chain(scene, gen)

        gen.get_param("path").value = "/Moved"

        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/Moved").GetAttribute("size").Get() == 0.5
        assert not stage.GetPrimAtPath("/Gen")
        assert Sdf.Path("/Moved") in skip_cook.record(gen).mask


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


def test_a_cook_sees_only_the_layers_above_it():
    """Not a sibling's, and not what is below it: a Transform wired above a Cube doesn't move it."""
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        sibling = _add(scene, _generator(cooks, "Sibling"))
        reader = _add(scene, _reader(cooks, "Reader"))
        below = _add(scene, _generator(cooks, "Below"))
        _chain(scene, gen, reader, below)
        layer_of = chunk_registry.ChunkLayerManager().get_layer

        hidden = skip_cook.hidden_layers(reader, layer_of(reader), layer_of)

        assert set(hidden) == {layer_of(sibling).identifier, layer_of(below).identifier}


def test_a_cook_reading_what_a_chunk_below_it_edits_stays_upstream_only():
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        _add(scene, _generator(cooks, "Sibling"))
        reader = _add(scene, _reader(cooks, "Reader"))
        below = _add(scene, _generator(cooks, "Below"))
        below.get_param("path").value = "/Gen"
        _chain(scene, gen, reader, below)

        gen.get_param("size").value = 3.0

        assert _size(scene, "/Reader") == 6.0
        assert not skip_cook.record(reader).warned_siblings


def test_a_chunk_naming_a_prim_another_branch_makes_is_blocked(caplog):
    """What a chunk sees is what is wired into it: live as after reopening, where the
    sibling may well cook after it. One that names a prim only a sibling makes is blocked,
    and says what to wire."""
    cooks: list = []
    with Scene() as scene:
        gen = _add(scene, _generator(cooks, "Gen"))
        sibling = _add(scene, _generator(cooks, "Sibling"))
        reader = _add(scene, _reader(cooks, "Reader"))
        _chain(scene, gen, reader)

        with caplog.at_level("WARNING", logger="usd_chunks.skip_cook"):
            reader.get_param("target").value = "/Sibling"

        assert not scene.get_stage().GetPrimAtPath("/Reader"), "a blocked chunk shows nothing"
        assert reader.has_state(ChunkFlags.State.FAILED)
        assert chunk_cook.cook_problem(reader) == (
            "/Sibling is made by Sibling, which isn't wired above Reader. Wire Sibling above Reader.")
        assert "Reader is blocked" in caplog.text

        # Wired to it, it sees it.
        reader.add_dependency(sibling)
        sibling.get_param("size").value = 3.0
        assert _size(scene, "/Reader") == 6.0
        assert not reader.has_state(ChunkFlags.State.FAILED)


def test_a_first_cook_does_not_see_a_sibling_that_cooked_before_it():
    """A first cook runs on the whole stage, where a sibling shows only if it cooked first."""
    cooks: list = []
    with Scene() as scene:
        sibling = _add(scene, _generator(cooks, "Sibling"))
        sibling.get_param("size").value = 3.0
        assert _size(scene, "/Sibling") == 3.0
        reader = _add(scene, _reader(cooks, "Reader"))
        reader.get_param("target").value = "/Sibling"

        assert not scene.get_stage().GetPrimAtPath("/Reader"), "a blocked chunk shows nothing"
        assert skip_cook.record(reader).mask is not None


def _moved(scene, path):
    prim = scene.get_stage().GetPrimAtPath(path)
    return tuple(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation())


def test_a_chunk_in_a_group_sees_what_is_wired_into_the_group(caplog):
    """No wire links a group's inner chunks to the group's input, yet they read what comes
    in through it: a Bind Material in a material group binding the room's floor."""
    with Scene() as scene:
        box = scene.create.cube(name="Box", prim_path="/geo/box")
        group = scene.create.chunkgroup(box, name="Group")
        with group:
            move = scene.create.transform(name="Move", primpath_location="/geo/box", tz=3.0)
        nested = scene.create.chunkgroup(group, name="Outer")
        with nested:
            inner = scene.create.chunkgroup(name="Inner")
            with inner:
                lift = scene.create.transform(name="Lift", primpath_location="/geo/box", tx=1.0)

        with caplog.at_level("WARNING", logger="usd_chunks.skip_cook"):
            scene.set(move, tz=5.0)
            scene.set(lift, tx=2.0)

        assert _moved(scene, "/geo/box") == (2.0, 0.0, 5.0)
        assert "isn't wired above it" not in caplog.text


def _red(scene) -> set[str]:
    return {c.name for c in scene.get_chunks() if c.has_state(ChunkFlags.State.FAILED)}


def _box_z(scene) -> float:
    return _moved(scene, "/geo/box")[2]


def test_a_transform_wired_above_the_cube_it_names_never_moves_it(tmp_path):
    """Transform -> Cube: the Cube is below it, so the Transform never sees the box. An
    edit saw it, and moved the box live but not after reopening."""
    path = str(tmp_path / "chain.usda")
    with Scene() as scene:
        move = scene.create.transform(name="Move", primpath_location="/geo/box", tz=5.0)
        scene.set_output(scene.create.cube(move, name="Box", prim_path="/geo/box"))
        assert _box_z(scene) == 0.0
        scene.set(move, tz=6.0)
        assert _box_z(scene) == 0.0, "an edit let the Transform see the Cube below it"
        assert _red(scene) == {"Move", "Box", "Output"}
        scene.save(path)

    with Scene.open(path) as scene:
        assert _box_z(scene) == 0.0
        assert _red(scene) == {"Move", "Box", "Output"}, "opened, the Transform was not blocked"


def test_a_merge_reads_like_a_chain(tmp_path):
    """The Merge's bottom input comes after the ones above it: a Transform there moves the
    Cube on the top input, as Cube -> Transform would."""
    path = str(tmp_path / "merge.usda")
    with Scene() as scene:
        move = scene.create.transform(name="Move", primpath_location="/geo/box", tz=5.0)
        box = scene.create.cube(name="Box", prim_path="/geo/box")
        scene.set_output(scene.create.merge(move, box, name="Merge"))
        assert _box_z(scene) == 5.0
        scene.set(move, tz=6.0)
        assert _box_z(scene) == 6.0
        assert not _red(scene)
        scene.save(path)

    with Scene.open(path) as scene:
        assert _box_z(scene) == 6.0
        assert not _red(scene)


def test_a_transform_above_the_cube_in_a_merge_never_moves_it(tmp_path):
    """A Merge input does not see the inputs below it, as Transform -> Cube does not."""
    path = str(tmp_path / "merge.usda")
    with Scene() as scene:
        move = scene.create.transform(name="Move", primpath_location="/geo/box", tz=5.0)
        box = scene.create.cube(name="Box", prim_path="/geo/box")
        scene.set_output(scene.create.merge(box, move, name="Merge"))
        scene.set(move, tz=6.0)
        assert _box_z(scene) == 0.0, "an edit let the Transform see the input below it"
        assert _red(scene) == {"Move", "Merge", "Output"}
        scene.save(path)

    with Scene.open(path) as scene:
        assert _box_z(scene) == 0.0
        assert _red(scene) == {"Move", "Merge", "Output"}, "opened, the Transform was not blocked"


def test_a_chunk_wired_after_its_first_cook_cooks_again():
    """A script wires a chunk after its first cook, which could not see what is now above it."""
    with Scene() as scene:
        box = scene.create.cube(name="Box", prim_path="/geo/box")
        scene.set_output(scene.create.transform(box, name="Move", primpath_location="/geo/box", tz=5.0))
        assert _box_z(scene) == 5.0


def test_changed_paths_ignore_a_parent_only_gaining_a_child():
    before = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(before, "/World/A").specifier = Sdf.SpecifierDef
    after = skip_cook.snapshot(before)
    Sdf.CreatePrimInLayer(after, "/World/B").specifier = Sdf.SpecifierDef

    assert skip_cook.changed_paths(before, after) == {Sdf.Path("/World/B")}


def test_a_glob_covers_the_prims_under_its_fixed_part():
    assert set(skip_cook._paths_in("/World/*/Mesh, /Other")) == {Sdf.Path("/World"), Sdf.Path("/Other")}
    assert set(skip_cook._paths_in("a_collection")) == {Sdf.Path.absoluteRootPath}


@pytest.mark.parametrize(
    ("path", "covered"),
    [
        ("/Rocks", True),
        ("/Rocks/Prototypes/Rock00_0", True),
        ("/Rocks/Prototypes/Rock00_0.points", True),
        ("/Rocks2", False),
        ("/World", False),
        ("/World/Machine/Mantle", True),
        ("/World/Machine", False),
    ],
)
def test_covered_is_at_or_below_a_masked_path(path, covered):
    mask = {Sdf.Path("/Rocks"), Sdf.Path("/World/Machine/Mantle")}
    assert skip_cook._covered(Sdf.Path(path), mask) is covered
    assert skip_cook._covered(Sdf.Path(path), mask | {Sdf.Path.absoluteRootPath}) is True
