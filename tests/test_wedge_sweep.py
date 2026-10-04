"""`usdchunks sweep` end to end: a network in, N different variants out, all reproducible.

The unit tests in test_wedge.py pin the sampling. This pins the part that only breaks once
a file has been through a save and a reload -- which is where the first working version of
this failed, silently: runtime-lofted params do not survive an export, so the reloaded wedge
held only its controls and every variant of a sweep came out identical to variant 0, from a
file that looked intact on disk.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import (
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    export_chunks,
    get_chunk_prim_path,
    initialize as init_chunk_usd,
)

REPO = Path(__file__).resolve().parents[1]


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def network(tmp_path):
    """A wedge driving a transform, exported to a .usda the CLI can sweep."""
    out = tmp_path / "net.usda"
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    init_chunk_usd(stage, scene)

    def make(module_name, node_name):
        mod = registry.get_chunk_module(module_name)
        chunks = mod.create() if mod.loaded else mod.load()
        node = chunks[0]
        node.name = node_name
        for c in chunks:
            scene.add_chunk(c)
        return node

    wedge = make("wedge", "Wedge")
    wedge.get_param("seed").value = 11
    wedge.get_param("count").value = 4
    wedge.get_param("ranges").value = "height 1 9"

    place = make("transform", "Place")
    place.get_param("primpath_location").value = "/World/box"
    place.get_param("tz").value = "@Wedge.height@"

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(place)))
    attr = prim.GetAttribute(_EXEC_INPUT) or prim.CreateAttribute(
        _EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
    )
    attr.SetConnections([Sdf.Path(f"{get_chunk_prim_path(wedge)}.{_EXEC_OUTPUT}")])
    place.recook()

    export_chunks(scene, str(out))
    # The cube has to be in the exported file, not only in the in-memory root layer:
    # export_chunks writes the network, and a reloaded network whose target prim is absent
    # cooks without complaint and exports an empty stage.
    exported = Usd.Stage.Open(str(out))
    UsdGeom.Cube.Define(exported, "/World/box")
    exported.GetRootLayer().Save()
    return out


def _sweep(network, out, *extra):
    # --jobs 4: a sweep that runs one process per variant runs them one at a time by
    # default, which made this the slowest file in the suite. A test's own --jobs, in
    # `extra`, comes later and wins.
    cmd = [sys.executable, "-m", "usd_chunks.cli", "sweep",
           str(network), "--out", str(out), "--jobs", "4", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))


def _tz(variant_usda):
    """The z translation the transform baked, read off the exported stage."""
    stage = Usd.Stage.Open(str(variant_usda))
    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/World/box")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()[2]


def test_the_spec_survives_the_file_and_the_variants_differ(network, tmp_path):
    """The failure this exists to catch: a wedge that reloads holding nothing drives
    nothing, and every variant is a copy of variant 0."""
    out = tmp_path / "data"
    result = _sweep(network, out)
    assert result.returncode == 0, result.stdout + result.stderr

    heights = [_tz(out / f"variant_{i:05d}" / "variant.usda") for i in range(4)]
    assert len(set(heights)) == 4, f"variants did not differ: {heights}"
    assert all(1.0 <= h <= 9.0 for h in heights), heights


def test_one_variant_regenerates_alone_and_matches(network, tmp_path):
    """--only is the whole recipe claim in miniature: variant 3 without producing 0-2."""
    out = tmp_path / "data"
    assert _sweep(network, out).returncode == 0
    expected = _tz(out / "variant_00003" / "variant.usda")

    alone = tmp_path / "alone"
    assert _sweep(network, alone, "--only", "3").returncode == 0

    assert _tz(alone / "variant_00003" / "variant.usda") == expected


def test_verify_reports_every_variant_identical(network, tmp_path):
    out = tmp_path / "data"
    assert _sweep(network, out).returncode == 0

    result = _sweep(network, out, "--verify")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "identical : 4/4" in result.stdout


def test_verify_notices_a_variant_that_changed(network, tmp_path):
    """A verifier that cannot fail proves nothing, so tamper with one and check it says so."""
    out = tmp_path / "data"
    assert _sweep(network, out).returncode == 0

    tampered = out / "variant_00002" / "variant.usda"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    result = _sweep(network, out, "--verify")
    assert result.returncode == 1
    assert "variant_00002" in result.stdout
    assert "identical : 3/4" in result.stdout


def test_a_network_without_a_wedge_is_refused(tmp_path):
    plain = tmp_path / "plain.usda"
    # Held in a local: dropping the stage collects it, and the layer it hands back is
    # already invalid by the time Save() reaches it.
    stage = Usd.Stage.CreateNew(str(plain))
    stage.GetRootLayer().Save()

    result = _sweep(plain, tmp_path / "data")
    assert result.returncode == 1
    assert "No wedge chunk" in result.stdout + result.stderr



@pytest.fixture
def variant_network(tmp_path):
    """An asset with a real `color` variantSet, and a wedge driving it through Set Variant.

    The point of the variants entry kind: the options are not restated on the wedge, they
    are read off the asset, so whoever authored it decides what paints exist and a variant
    can carry anything -- a binding, a mesh, a sub-asset reference -- not just a value.
    """
    out = tmp_path / "vnet.usda"
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    init_chunk_usd(stage, scene)

    def make(module_name, node_name):
        mod = registry.get_chunk_module(module_name)
        chunks = mod.create() if mod.loaded else mod.load()
        node = chunks[0]
        node.name = node_name
        for c in chunks:
            scene.add_chunk(c)
        return node

    wedge = make("wedge", "Wedge")
    wedge.get_param("seed").value = 3
    wedge.get_param("count").value = 8
    wedge.get_param("ranges").value = "Paint variants /World/thing color"

    setter = make("set_variant", "Paint")
    setter.get_param("primpath_location").value = "/World/thing"
    setter.get_param("variant_set").value = "color"
    setter.get_param("variant_selection").value = "@Wedge.Paint@"

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(setter)))
    attr = prim.GetAttribute(_EXEC_INPUT) or prim.CreateAttribute(
        _EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
    )
    attr.SetConnections([Sdf.Path(f"{get_chunk_prim_path(wedge)}.{_EXEC_OUTPUT}")])

    export_chunks(scene, str(out))

    # The asset, authored into the exported file: three colour variants, each setting a
    # different displayColor, so which one composed is readable from the result.
    exported = Usd.Stage.Open(str(out))
    thing = UsdGeom.Cube.Define(exported, "/World/thing")
    vset = thing.GetPrim().GetVariantSets().AddVariantSet("color")
    for name, rgb in (("red", (1, 0, 0)), ("green", (0, 1, 0)), ("blue", (0, 0, 1))):
        vset.AddVariant(name)
        vset.SetVariantSelection(name)
        with vset.GetVariantEditContext():
            thing.CreateDisplayColorAttr([Gf.Vec3f(*rgb)])
    vset.ClearVariantSelection()
    exported.GetRootLayer().Save()
    return out


def _display_color(variant_usda):
    stage = Usd.Stage.Open(str(variant_usda))
    attr = UsdGeom.Cube(stage.GetPrimAtPath("/World/thing")).GetDisplayColorAttr()
    value = attr.Get() if attr else None
    return tuple(value[0]) if value else None


def test_a_wedge_drives_a_variantset(variant_network, tmp_path):
    """Different indices select different variants, and only ones the asset offers."""
    out = tmp_path / "data"
    result = _sweep(variant_network, out)
    assert result.returncode == 0, result.stdout + result.stderr

    colours = [_display_color(out / f"variant_{i:05d}" / "variant.usda") for i in range(8)]
    authored = {(1, 0, 0), (0, 1, 0), (0, 0, 1)}
    assert all(c in authored for c in colours), colours
    assert len(set(colours)) > 1, f"the wedge selected one variant every time: {colours}"


def test_selecting_a_variant_is_reproducible(variant_network, tmp_path):
    out = tmp_path / "data"
    assert _sweep(variant_network, out).returncode == 0

    result = _sweep(variant_network, out, "--verify")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "identical : 8/8" in result.stdout



def _author_asset(stage):
    """A cube with two orthogonal variantSets: 3 colours, 2 bodies."""
    thing = UsdGeom.Cube.Define(stage, "/World/thing")
    prim = thing.GetPrim()
    colours = prim.GetVariantSets().AddVariantSet("color")
    for name, rgb in (("red", (1, 0, 0)), ("green", (0, 1, 0)), ("blue", (0, 0, 1))):
        colours.AddVariant(name)
        colours.SetVariantSelection(name)
        with colours.GetVariantEditContext():
            thing.CreateDisplayColorAttr([Gf.Vec3f(*rgb)])
    colours.ClearVariantSelection()

    bodies = prim.GetVariantSets().AddVariantSet("body")
    for name, size in (("coupe", 1.0), ("wagon", 2.0)):
        bodies.AddVariant(name)
        bodies.SetVariantSelection(name)
        with bodies.GetVariantEditContext():
            thing.CreateSizeAttr(size)
    bodies.ClearVariantSelection()
    return thing


@pytest.fixture
def two_set_network(tmp_path):
    """An asset with two orthogonal variantSets -- 3 colours x 2 bodies -- and a wedge in
    Enumerate mode. Two sets rather than one because the whole point of enumerate is the
    cross product, and one set cannot show a stride bug."""
    out = tmp_path / "enum.usda"
    stage = Usd.Stage.CreateInMemory()

    # The asset first, chunks second: the wedge counts combinations by reading the
    # variantSets off the stage, so a wedge created before them enumerates nothing.
    _author_asset(stage)

    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    init_chunk_usd(stage, scene)

    def make(module_name, node_name):
        mod = registry.get_chunk_module(module_name)
        chunks = mod.create() if mod.loaded else mod.load()
        node = chunks[0]
        node.name = node_name
        for c in chunks:
            scene.add_chunk(c)
        return node

    wedge = make("wedge", "Wedge")
    wedge.get_param("seed").value = 5
    wedge.get_param("mode").value = "Enumerate"
    wedge.get_param("ranges").value = (
        "Paint variants /World/thing color\n"
        "Body variants /World/thing body"
    )

    previous = wedge
    for node_name, set_name in (("SetPaint", "color"), ("SetBody", "body")):
        setter = make("set_variant", node_name)
        setter.get_param("primpath_location").value = "/World/thing"
        setter.get_param("variant_set").value = set_name
        setter.get_param("variant_selection").value = (
            "@Wedge.Paint@" if set_name == "color" else "@Wedge.Body@"
        )
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(setter)))
        attr = prim.GetAttribute(_EXEC_INPUT) or prim.CreateAttribute(
            _EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
        )
        attr.SetConnections([Sdf.Path(f"{get_chunk_prim_path(previous)}.{_EXEC_OUTPUT}")])
        previous = setter

    export_chunks(scene, str(out))

    # export_chunks writes the network, not the scene, so the asset has to be authored into
    # the exported file too or the reloaded network cooks against nothing.
    exported = Usd.Stage.Open(str(out))
    _author_asset(exported)
    exported.GetRootLayer().Save()
    return out


def _config(variant_usda):
    """(colour, size) of the composed asset -- which combination this variant selected."""
    stage = Usd.Stage.Open(str(variant_usda))
    cube = UsdGeom.Cube(stage.GetPrimAtPath("/World/thing"))
    colour = cube.GetDisplayColorAttr().Get()
    return (tuple(colour[0]) if colour else None, cube.GetSizeAttr().Get())


def test_enumerate_covers_every_combination_exactly_once(two_set_network, tmp_path):
    """3 colours x 2 bodies is 6 configurations, and enumerate should produce each once.

    A stride bug shows up here and nowhere else: get it wrong and you sweep six variants
    that cover three combinations twice.
    """
    out = tmp_path / "data"
    result = _sweep(two_set_network, out)
    assert result.returncode == 0, result.stdout + result.stderr

    seen = [_config(out / f"variant_{i:05d}" / "variant.usda") for i in range(6)]
    assert len(set(seen)) == 6, f"combinations repeated: {sorted(seen)}"
    assert {c for c, _ in seen} == {(1, 0, 0), (0, 1, 0), (0, 0, 1)}
    assert {s for _, s in seen} == {1.0, 2.0}


def test_enumerate_writes_the_derived_count_into_the_file(two_set_network):
    """`usdchunks sweep` reads inputs:count off the prim without loading the network, so
    the product has to be in the file rather than computed by the CLI."""
    stage = Usd.Stage.Open(str(two_set_network))
    wedge = next(p for p in stage.TraverseAll()
                 if p.GetTypeName() == "Chunk"
                 and p.GetAttribute("info:chunkType")
                 and p.GetAttribute("info:chunkType").Get() == "wedge")
    assert wedge.GetAttribute("inputs:count").Get() == 6


def test_an_expression_onto_a_lofted_param_updates_live(network):
    """The listener has to survive load, not just the value.

    A param lofted in a CHUNK_LOADED callback -- the wedge's ranges, mtlx_shader's shader
    inputs -- does not exist during the first expression pass, so resolution silently
    attached no listener. Reads stayed correct, which is why sweeps and exports were right
    the whole time and only the UI never updated when the referenced param changed.

    In a subprocess for the reason every other test here forks: the registry and stage-sync
    singletons hold the stage a network was loaded into, so a second load_chunks in one
    interpreter returns the chunkgroup and none of its children.
    """
    script = (
        "import sys;"
        "from pxr import Usd;"
        "from usd_chunks import chunk_registry;"
        "from usd_chunks.chunk_scene import SceneSender;"
        "from usd_chunks.chunk_registry import ChunkLayerManager;"
        "from usd_chunks.chunk_stage import load_chunks;"
        "from usd_chunks.cli import _all_chunks;"
        "stage = Usd.Stage.Open(sys.argv[1]);"
        "S = type('S', (SceneSender,), {"
        "  'get_stage': lambda self: stage,"
        "  'get_selected_prims': lambda self: []});"
        "scene = S();"
        "chunk_registry.ChunkRegistry().initialize(scene, 'cli');"
        "ChunkLayerManager().initialize(stage);"
        "load_chunks(stage, scene);"
        "cs = {c.name: c for c in _all_chunks(scene.get_chunks())};"
        "tz = cs['Place'].get_param('tz');"
        "print('LOFTED', cs['Wedge'].get_param('height') is not None);"
        "print('RAW', tz.raw_value);"
        "print('LISTENER', bool(getattr(tz, '_expression_connections', None)))"
    )
    out = subprocess.run([sys.executable, "-c", script, str(network)],
                         capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "LOFTED True" in out.stdout, "the wedge did not loft on load"
    assert "RAW @Wedge.height@" in out.stdout
    assert "LISTENER True" in out.stdout, (
        "no live-update listener: the expression resolved to nothing because the wedge had "
        "not lofted its ranges yet when it was tried"
    )


def test_sweep_hands_overrides_to_every_variant(network, tmp_path):
    """`sweep` takes the same overrides as `run`, written anywhere on the line.

    What makes a GPU sweep possible from the command line (`vray_render_device=7`). Written
    after the options is the natural place for them, and the one argparse used to refuse.
    """
    plain, reseeded = tmp_path / "plain", tmp_path / "reseeded"
    assert _sweep(network, plain, "--only", "1").returncode == 0
    result = _sweep(network, reseeded, "--only", "1", "Wedge.seed=99")
    assert result.returncode == 0, result.stdout + result.stderr

    assert _tz(reseeded / "variant_00001" / "variant.usda") != _tz(plain / "variant_00001" / "variant.usda")


def test_a_batched_sweep_writes_what_one_process_per_variant_does(network, tmp_path):
    """Batching is the default; its variants have to be the per-variant ones, byte for byte."""
    batched, separate = tmp_path / "batched", tmp_path / "separate"
    result = _sweep(network, batched, "--only", "0,2,3")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "batched" in result.stdout, result.stdout
    assert _sweep(network, separate, "--only", "0,2,3", "--per-variant").returncode == 0
    # --jobs hands part of the preparing to worker processes; the stitch and commit stay one.
    shared = tmp_path / "shared"
    assert _sweep(network, shared, "--only", "0,2,3", "--jobs", "2").returncode == 0

    for i in (0, 2, 3):
        b = (separate / f"variant_{i:05d}" / "variant.usda").read_bytes()
        for other in (batched, shared):
            a = (other / f"variant_{i:05d}" / "variant.usda").read_bytes()
            assert a == b, f"variant {i} differs between {other.name} and a per-variant sweep"
    assert (batched / "stitched.usda").exists()
