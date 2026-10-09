"""A shot of department layers runs as one network, headless.

shot.usda sublayers lighting.usda over layout.usda. Each department file holds a chain of
its own ending in its Output: layout A1 -> A2 -> Output, lighting B1 -> B2 -> Output.
`usdchunks run shot.usda` has to cook all four, weakest layer first, and an override on a
layout chunk authored in lighting.usda has to reach that chunk's cook.

Each department's Output ends its turn. The turns run weakest layer first, so lighting
cooks on layout's result, and a lighting `over` on a layout chunk is a param that chunk
cooks with in layout's turn.

The department files are saved without cooking, so they hold no bake: a transform's value
reaches its cube only if the run cooked it.
"""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from network_files import read_z, run, run_and_read, save_cubes, save_network
from usd_chunks import chunk_layers, chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_script import Scene

_TZ = "inputs:translate_grp:tz"


def _link_cook_sublayers(shot_dir, *departments):
    """Publish each department with its cook sublayer, as its owner would."""
    for department in departments:
        home = Sdf.Layer.FindOrOpen(str(shot_dir / f"{department}.usda"))
        chunk_layers.add_cook_sublayer(home)
        home.Save()


@pytest.fixture
def shot(tmp_path):
    """shot.usda: lighting.usda over layout.usda over set.usda (four cubes at the origin)."""
    save_cubes(tmp_path / "set.usda", "abcd")
    layout = save_network(
        tmp_path / "layout.usda", {"A1": ("/a", 1.0), "A2": ("/b", 2.0)}, ends=("output",)
    )
    lighting = save_network(
        tmp_path / "lighting.usda", {"B1": ("/c", 3.0), "B2": ("/d", 4.0)}, ends=("output",)
    )
    shot_layer = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    shot_layer.subLayerPaths[:] = ["./lighting.usda", "./layout.usda", "./set.usda"]
    shot_layer.Save()
    return tmp_path, {**layout, **lighting}


def test_the_department_files_hold_no_bake(shot):
    """The fixture's premise: before any run, no cube has moved."""
    shot_dir, _ = shot
    stage = Usd.Stage.Open(str(shot_dir / "shot.usda"))
    for name in "abcd":
        xform = UsdGeom.Xformable(stage.GetPrimAtPath(f"/{name}"))
        assert xform.ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation() == Gf.Vec3d(0)


def test_every_departments_chunks_cook(shot):
    shot_dir, _ = shot
    z = run_and_read(shot_dir / "shot.usda", "abcd")
    assert z == pytest.approx({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0})


def test_an_override_in_lighting_reaches_a_layout_chunk(shot):
    """An `over` on A1's tz authored in lighting.usda, as a lighting artist editing a layout
    chunk with lighting as the edit target would write it."""
    shot_dir, paths = shot
    lighting = Sdf.Layer.FindOrOpen(str(shot_dir / "lighting.usda"))
    spec = Sdf.CreatePrimInLayer(lighting, paths["A1"])
    spec.specifier = Sdf.SpecifierOver
    attr = Sdf.AttributeSpec(spec, _TZ.split(".")[-1], Sdf.ValueTypeNames.Double)
    attr.default = 10.0
    lighting.Save()
    assert run_and_read(shot_dir / "shot.usda", "abcd")["a"] == pytest.approx(10.0)


def test_the_graph_delivers_the_strongest_layers_output(shot):
    shot_dir, _ = shot
    scene = Scene.open(str(shot_dir / "shot.usda"), "pytest")
    try:
        output = chunk_stage.strongest_output(scene.get_stage(), scene, None)
        assert chunk_stage.get_chunk_prim_path(output).endswith("/lighting_output")
    finally:
        scene.close()


def test_the_stack_implies_layout_feeds_lighting_and_writes_nothing(shot):
    """B1 has no wire in, so it starts where layout's chain ends: it depends on A2, the
    chunk feeding layout's Output. The edge is read from the stack, never authored."""
    shot_dir, paths = shot
    scene = Scene.open(str(shot_dir / "shot.usda"), "pytest")
    try:
        stage = scene.get_stage()
        by_path = {chunk_stage.get_chunk_prim_path(c): c for c in scene.get_chunks()}
        b1, a2 = by_path[paths["B1"]], by_path[paths["A2"]]
        assert any(dep is a2 for _, dep in b1.get_dependencies())
        assert chunk_stage.implied_sources(stage.GetPrimAtPath(paths["B1"])) == [paths["A2"]]
        assert chunk_stage.implied_sources(stage.GetPrimAtPath(paths["A1"])) == []
        b1_prim = stage.GetPrimAtPath(paths["B1"])
        assert not chunk_stage.exec_input_connections(b1_prim), "the implied edge was authored"
    finally:
        scene.close()


def test_a_stronger_layers_hand_authored_value_beats_a_weaker_layers_cook(shot):
    """A cook sits at the strength of the layer that defines its chunk: layout's cook goes
    in layout's cook sublayer, just under layout.usda, so lighting.usda's hand-authored op
    order on the same prim wins -- a layout procedural no longer beats what lighting
    authors. Results used to sit in the session layer, over everything."""
    shot_dir, _ = shot
    _link_cook_sublayers(shot_dir, "layout", "lighting")
    lighting = Sdf.Layer.FindOrOpen(str(shot_dir / "lighting.usda"))
    stage = Usd.Stage.Open(lighting)
    over = stage.OverridePrim("/a")
    UsdGeom.Xformable(over).CreateXformOpOrderAttr().Set(["xformOp:translate"])
    over.CreateAttribute("xformOp:translate", Sdf.ValueTypeNames.Double3).Set(Gf.Vec3d(0, 0, 7))
    lighting.Save()
    z = run_and_read(shot_dir / "shot.usda", "ab")
    assert z == pytest.approx({"a": 7.0, "b": 2.0})


def test_a_departments_cook_nests_under_its_own_layer(shot):
    """Each department's chunks cook into a cook layer sublayered under that department's
    file, in memory, and shot.usda is never given one."""
    shot_dir, paths = shot
    _link_cook_sublayers(shot_dir, "layout", "lighting")
    scene = Scene.open(str(shot_dir / "shot.usda"), "pytest")
    try:
        stage = scene.get_stage()
        lm = chunk_registry.ChunkLayerManager()
        by_path = {chunk_stage.get_chunk_prim_path(c): c for c in scene.get_chunks()}
        for chunk_path, department in ((paths["A1"], "layout"), (paths["B1"], "lighting")):
            cook = lm.get_chunk_target(by_path[chunk_path].get_hash())
            assert cook == Sdf.Layer.Find(str(shot_dir / f"{department}.cook.usda"))
        assert stage.GetRootLayer().subLayerPaths == ["./lighting.usda", "./layout.usda", "./set.usda"]
    finally:
        scene.close()


def test_a_listed_cook_sublayer_is_used_and_never_written(shot):
    """A department published with its cook sublayer -- layout.usda lists layout.cook.usda --
    has its chunks cook into that layer in memory; neither file is written, so both may be
    locked."""
    shot_dir, paths = shot
    cook_file = shot_dir / "layout.cook.usda"
    _link_cook_sublayers(shot_dir, "layout")
    before = {p: p.read_bytes() for p in (shot_dir / "layout.usda", cook_file)}
    scene = Scene.open(str(shot_dir / "shot.usda"), "pytest")
    try:
        lm = chunk_registry.ChunkLayerManager()
        by_path = {chunk_stage.get_chunk_prim_path(c): c for c in scene.get_chunks()}
        cook = lm.get_chunk_target(by_path[paths["A1"]].get_hash())
        assert cook == Sdf.Layer.Find(str(cook_file))
    finally:
        scene.close()
    assert {p: p.read_bytes() for p in before} == before


def test_a_layer_without_a_cook_sublayer_cooks_into_the_session_layer(shot):
    """Opt-in by the link: lighting lists no cook sublayer, so its chunks cook into the
    session layer as before -- giving it one on open would dirty the file."""
    shot_dir, paths = shot
    _link_cook_sublayers(shot_dir, "layout")
    scene = Scene.open(str(shot_dir / "shot.usda"), "pytest")
    try:
        lm = chunk_registry.ChunkLayerManager()
        by_path = {chunk_stage.get_chunk_prim_path(c): c for c in scene.get_chunks()}
        target = lm.get_chunk_target(by_path[paths["B1"]].get_hash())
        assert target == scene.get_stage().GetSessionLayer()
        assert not Sdf.Layer.Find(str(shot_dir / "lighting.usda")).dirty
    finally:
        scene.close()


def _plain_open(path):
    """path opened as a consumer without usd_chunks would: plain USD, read from disk."""
    stage = Usd.Stage.Open(str(path))
    for layer in stage.GetUsedLayers():
        if not layer.anonymous:
            layer.Reload()
    return stage


def test_a_bake_is_written_to_each_cook_file_and_composes_without_usd_chunks(shot):
    """`usdchunks run --bake` writes what each department cooked into its cook file, and the
    shot opened in plain USD -- nothing cooking -- shows every result."""
    shot_dir, paths = shot
    _link_cook_sublayers(shot_dir, "layout", "lighting")
    out = run(shot_dir / "shot.usda", "--bake")
    assert "layout.cook.usda" in out and "lighting.cook.usda" in out
    z = read_z(_plain_open(shot_dir / "shot.usda"), "abcd")
    assert z == pytest.approx({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0})
    bake = Sdf.Layer.OpenAsAnonymous(str(shot_dir / "layout.cook.usda"))
    assert not bake.subLayerPaths, "the bake lists layers that only existed in memory"
    data = bake.customLayerData
    assert data["usdchunks:derived"]
    stamps = data["usdchunks:cooks"]
    assert {paths["A1"], paths["A2"]} <= set(stamps)
    assert not {paths["B1"], paths["B2"]} & set(stamps), "lighting's chunks stamped in layout's bake"
    for stamp in stamps.values():
        assert stamp["module"] and stamp["inputs"] and not stamp["partial"]


def test_a_live_cook_supersedes_the_saved_bake(shot):
    """The bake is the last result; once the chunks cook, a prim only the old bake made is
    gone -- it does not linger under the live cooks."""
    shot_dir, _ = shot
    _link_cook_sublayers(shot_dir, "layout")
    bake = Sdf.Layer.FindOrOpen(str(shot_dir / "layout.cook.usda"))
    Sdf.CreatePrimInLayer(bake, "/stale").specifier = Sdf.SpecifierDef
    bake.Save()
    del bake
    scene = Scene.open(str(shot_dir / "shot.usda"), "pytest")
    try:
        assert not scene.get_stage().GetPrimAtPath("/stale")
    finally:
        scene.close()


def _house_shot(tmp_path, shot_chunks: bool):
    """shot.usda referencing house.usda's network at /World/house/chunks, over the cubes.

    The house moves /a and /b; with shot_chunks the shot has a chain of its own moving /c.
    """
    save_cubes(tmp_path / "set.usda", "abc")
    save_network(tmp_path / "house.usda", {"H1": ("/a", 1.0), "H2": ("/b", 2.0)}, ends=("output",))
    shot = tmp_path / "shot.usda"
    if shot_chunks:
        save_network(shot, {"S1": ("/c", 3.0)}, ends=("output",))
        layer = Sdf.Layer.FindOrOpen(str(shot))
    else:
        layer = Sdf.Layer.CreateNew(str(shot))
    layer.subLayerPaths.append("./set.usda")
    spec = Sdf.CreatePrimInLayer(layer, "/World/house/chunks")
    spec.specifier = Sdf.SpecifierDef
    spec.referenceList.Prepend(Sdf.Reference("./house.usda", "/chunks"))
    for path in ("/World", "/World/house"):
        layer.GetPrimAtPath(path).specifier = Sdf.SpecifierDef
    layer.Save()
    return shot


def test_a_referenced_network_cooks_on_its_own(tmp_path):
    assert run_and_read(_house_shot(tmp_path, False), "ab") == pytest.approx({"a": 1.0, "b": 2.0})


def test_a_referenced_network_cooks_before_the_shots_own_chain(tmp_path):
    """The house comes in by a reference authored in shot.usda, so it is upstream of the
    shot's first chunk: cooking to the shot's Output cooks the house too, rather than
    muting it for not being wired to anything."""
    z = run_and_read(_house_shot(tmp_path, True), "abc")
    assert z == pytest.approx({"a": 1.0, "b": 2.0, "c": 3.0})


def test_a_network_referenced_by_a_stronger_layer_cooks_in_its_turn(tmp_path):
    """lighting.usda references the house. It cooks before lighting's first chunk, after
    layout's chain -- layout is weaker than the layer that brings the house in, so it does
    not wait for it."""
    save_cubes(tmp_path / "set.usda", "abcd")
    save_network(tmp_path / "house.usda", {"H1": ("/d", 4.0)}, ends=("output",))
    layout = save_network(tmp_path / "layout.usda", {"A1": ("/a", 1.0), "A2": ("/b", 2.0)},
                          ends=("output",))
    lighting = save_network(tmp_path / "lighting.usda", {"B1": ("/c", 3.0)}, ends=("output",))
    light_layer = Sdf.Layer.FindOrOpen(str(tmp_path / "lighting.usda"))
    spec = Sdf.CreatePrimInLayer(light_layer, "/World/house/chunks")
    spec.specifier = Sdf.SpecifierDef
    spec.referenceList.Prepend(Sdf.Reference("./house.usda", "/chunks"))
    for path in ("/World", "/World/house"):
        light_layer.GetPrimAtPath(path).specifier = Sdf.SpecifierDef
    light_layer.Save()
    shot = tmp_path / "shot.usda"
    shot_layer = Sdf.Layer.CreateNew(str(shot))
    shot_layer.subLayerPaths[:] = ["./lighting.usda", "./layout.usda", "./set.usda"]
    shot_layer.Save()

    assert run_and_read(shot, "abcd") == pytest.approx({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0})

    scene = Scene.open(str(shot), "pytest")
    try:
        stage = scene.get_stage()
        house_h1 = "/World/house/chunks/ChunkGroup/H1"
        assert stage.GetPrimAtPath(house_h1).IsValid()
        assert set(chunk_stage.implied_sources(stage.GetPrimAtPath(lighting["B1"]))) == {
            layout["A2"], house_h1}
        assert chunk_stage.implied_sources(stage.GetPrimAtPath(layout["A1"])) == []
    finally:
        scene.close()


def test_a_referenced_network_loads_in_place(tmp_path):
    """Where USD composed it, not copied: the house's chunks are the prims at
    /World/house/chunks, and nothing is re-made under a tab named after house.usda."""
    shot = _house_shot(tmp_path, True)
    scene = Scene.open(str(shot), "pytest")
    try:
        stage = scene.get_stage()
        paths = {chunk_stage.get_chunk_prim_path(c) for c in scene.get_chunks()}
        assert "/World/house/chunks/ChunkGroup/H1" in paths
        assert not any(p.startswith("/house") for p in paths)
        assert not stage.GetPrimAtPath("/house")
        assert "/chunks/ChunkGroup/S1" in paths
        # Nothing written into the shot for the house's chunks: they are not the shot's.
        assert not stage.GetRootLayer().GetPrimAtPath("/World/house/chunks/ChunkGroup/H1")
    finally:
        scene.close()


def test_opening_the_shot_writes_nothing_into_it(shot):
    """The department files were saved uncooked, so their transforms mint their op ids on
    the first cook. Those chunks come from files the shot does not own: the id is computed,
    the same on every open, and nothing is written into shot.usda for it."""
    shot_dir, _ = shot
    scene = Scene.open(str(shot_dir / "shot.usda"), "pytest")
    try:
        root = scene.get_stage().GetRootLayer()
        assert not root.dirty, root.ExportToString()
    finally:
        scene.close()


def test_a_computed_op_id_is_the_same_on_every_open(shot):
    shot_dir, paths = shot
    ops = []
    for _ in range(2):
        scene = Scene.open(str(shot_dir / "shot.usda"), "pytest")
        try:
            a1 = next(c for c in scene.get_chunks()
                      if chunk_stage.get_chunk_prim_path(c) == paths["A1"])
            ops.append(chunk_utils.xform_op_name(a1))
        finally:
            scene.close()
    assert ops[0] == ops[1]


def test_a_stronger_layer_wiring_a_weaker_chunk_after_its_own(shot):
    """lighting.usda rewires A2 to follow B1 (an `over` on A2's exec input). A2 then waits
    for lighting's chain, and B1 must not wait for A2 in turn: that was a cycle, and the
    load failed. Layout still delivers its Output. A1 no longer feeds anything -- the over
    replaced A2's input -- so, like any chunk wired to no Output, it does not cook."""
    shot_dir, paths = shot
    lighting = Sdf.Layer.FindOrOpen(str(shot_dir / "lighting.usda"))
    spec = Sdf.CreatePrimInLayer(lighting, paths["A2"])
    spec.specifier = Sdf.SpecifierOver
    attr = Sdf.AttributeSpec(spec, "inputs:chunk", Sdf.ValueTypeNames.Token)
    attr.connectionPathList.explicitItems = [Sdf.Path(f"{paths['B1']}.outputs:chunk")]
    lighting.Save()
    z = run_and_read(shot_dir / "shot.usda", "abcd")
    assert z == pytest.approx({"a": 0.0, "b": 2.0, "c": 3.0, "d": 4.0})
