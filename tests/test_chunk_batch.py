"""`usdchunks batch`: the planner at the USD level, then the command end to end.

The pure glob math is covered separately in test_chunk_glob.py. The planner part exercises
what reads a real stage: which attributes count as pattern sites, which single one of them
drives the batch, what each run is handed, and the checks that refuse a batch before it
writes anything. The end-to-end part runs the CLI in a subprocess, as test_wedge_sweep does,
since each run is its own process anyway.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pxr import Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk_registry
from usd_chunks.chunk_batch import (
    BatchError,
    apply_overrides,
    build_plan,
    check_commit_targets,
    committing_chunk_names,
    find_pattern_sites,
    format_plan,
    select_driver,
    split_overrides,
    write_session,
)
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import export_chunks, initialize as init_chunk_usd

REPO = Path(__file__).resolve().parents[1]


def _chunk_prim(stage, path, chunk_type):
    prim = stage.DefinePrim(path, "Chunk")
    prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set(chunk_type)
    return prim


def _set_str(prim, name, value):
    prim.CreateAttribute(f"inputs:{name}", Sdf.ValueTypeNames.String).Set(value)


@pytest.fixture
def asset_tree(tmp_path):
    """Two real assets to be found, plus a directory that must never be returned."""
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("crate.usda", "hero.usda"):
        (assets / name).write_text("#usda 1.0\n")
    (tmp_path / "empty").mkdir()
    return tmp_path


@pytest.fixture
def stage():
    return Usd.Stage.CreateInMemory()


@pytest.fixture
def network(stage):
    """A Reference driving the batch, an Export receiving it, and a chunkgroup whose doc
    prose contains a `*` that must not be mistaken for a pattern."""
    group = _chunk_prim(stage, "/chunks", "chunkgroup")
    _set_str(group, "doc", "Bullets:\n  * convert every asset\n  * write one file each")

    ref = _chunk_prim(stage, "/chunks/Reference", "reference")
    _set_str(ref, "file_path", "./assets/*.usda")

    export = _chunk_prim(stage, "/chunks/Export", "export_usd")
    _set_str(export, "output_file", "out/*.usda")
    return stage


# ---------------------------------------------------------------------------
# find_pattern_sites
# ---------------------------------------------------------------------------


def test_finds_patterns_only_where_they_exist(network):
    labels = {s.label for s in find_pattern_sites(network)}
    assert labels == {"Reference.file_path", "Export.output_file"}


def test_plain_paths_are_not_pattern_sites(stage):
    ref = _chunk_prim(stage, "/chunks/Reference", "reference")
    _set_str(ref, "file_path", "./assets/hero.usda")
    assert find_pattern_sites(stage) == []


def test_doc_prose_is_never_a_pattern(network):
    assert not any(s.param_name == "doc" for s in find_pattern_sites(network))


def test_exec_inputs_are_skipped(stage):
    ref = _chunk_prim(stage, "/chunks/Reference", "reference")
    ref.CreateAttribute("inputs:chunk", Sdf.ValueTypeNames.Token)
    _set_str(ref, "chunk1", "a/*.usda")
    labels = {s.label for s in find_pattern_sites(stage)}
    assert "Reference.chunk" not in labels and "Reference.chunk1" not in labels


def test_expressions_are_not_patterns(stage):
    """An expression's value is computed from another chunk's param -- overwriting it with a
    batch path would destroy the wiring it stands for."""
    ref = _chunk_prim(stage, "/chunks/Reference", "reference")
    _set_str(ref, "primpath_location", "@Prim.primpath_location@{test=A}/geo*")
    assert find_pattern_sites(stage) == []


def test_a_pattern_in_a_list_param_is_refused_by_name(stage, asset_tree):
    """`run` sets whole params, so a list entry cannot be handed over -- refused before
    anything runs rather than left as a literal glob in every run."""
    ref = _chunk_prim(stage, "/chunks/Reference", "reference")
    _set_str(ref, "file_path", "./assets/*.usda")
    sub = _chunk_prim(stage, "/chunks/Sublayer", "sublayer")
    sub.CreateAttribute("inputs:imports:sublayer_path", Sdf.ValueTypeNames.StringArray).Set(
        Vt.StringArray(["lib/*.usda", "fixed/base.usda"])
    )
    with pytest.raises(BatchError, match=r"list param.*Sublayer\.imports\.sublayer_path\[0\]"):
        build_plan(stage, asset_tree)


def test_a_chunk_name_shared_by_two_chunks_is_refused(stage, asset_tree):
    """`run` finds a chunk by name; with two of them it would set whichever came first."""
    ref = _chunk_prim(stage, "/chunks/A/Reference", "reference")
    _set_str(ref, "file_path", "./assets/*.usda")
    _chunk_prim(stage, "/chunks/B/Reference", "reference")
    with pytest.raises(BatchError, match="more than one chunk is named Reference"):
        build_plan(stage, asset_tree)


# ---------------------------------------------------------------------------
# select_driver
# ---------------------------------------------------------------------------


def test_driver_is_the_pattern_matching_real_files(network, asset_tree):
    driver, matches = select_driver(find_pattern_sites(network), asset_tree)
    assert driver.label == "Reference.file_path"
    assert [path for path, _ in matches] == ["./assets/crate.usda", "./assets/hero.usda"]


def test_two_matching_patterns_is_a_hard_error(network, asset_tree):
    """Both name real files, so which one is the input is genuinely unknowable."""
    network.GetPrimAtPath("/chunks/Export").GetAttribute("inputs:output_file").Set("./assets/*.usda")
    with pytest.raises(BatchError) as exc:
        select_driver(find_pattern_sites(network), asset_tree)
    message = str(exc.value)
    assert "Reference.file_path" in message and "Export.output_file" in message
    assert "--driver" in message


def test_explicit_driver_resolves_ambiguity(network, asset_tree):
    network.GetPrimAtPath("/chunks/Export").GetAttribute("inputs:output_file").Set("./assets/*.usda")
    driver, matches = select_driver(find_pattern_sites(network), asset_tree, driver="Export.output_file")
    assert driver.label == "Export.output_file"
    assert len(matches) == 2


def test_explicit_driver_matching_nothing_is_an_error(network, asset_tree):
    with pytest.raises(BatchError, match="matched no files"):
        select_driver(find_pattern_sites(network), asset_tree, driver="Export.output_file")


def test_unknown_driver_label_lists_the_real_ones(network, asset_tree):
    with pytest.raises(BatchError) as exc:
        select_driver(find_pattern_sites(network), asset_tree, driver="Nope.missing")
    assert "Reference.file_path" in str(exc.value)


def test_no_pattern_matching_any_file_is_an_error(network, tmp_path):
    with pytest.raises(BatchError, match="no pattern matched any existing file"):
        select_driver(find_pattern_sites(network), tmp_path)


def test_a_network_with_no_patterns_at_all(stage, asset_tree):
    _chunk_prim(stage, "/chunks/Reference", "reference")
    with pytest.raises(BatchError, match="no glob patterns"):
        select_driver(find_pattern_sites(stage), asset_tree)


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_plan_fills_sinks_from_driver_captures(network, asset_tree):
    plan = build_plan(network, asset_tree)
    assert [r.source for r in plan.runs] == ["./assets/crate.usda", "./assets/hero.usda"]
    assert [dict((s.label, v) for s, v in r.sink_values) for r in plan.runs] == [
        {"Export.output_file": "out/crate.usda"},
        {"Export.output_file": "out/hero.usda"},
    ]


def _labelled(values):
    return [(site.label, value) for site, value in values]


def test_each_run_is_handed_its_driver_file_and_sink_values(network, asset_tree):
    plan = build_plan(network, asset_tree)
    assert _labelled(plan.runs[1].values(plan.driver)) == [
        ("Reference.file_path", "./assets/hero.usda"),
        ("Export.output_file", "out/hero.usda"),
    ]


def test_a_run_is_handed_absolute_paths_given_the_anchor(network, asset_tree):
    """A chunk's anonymous layer has no folder for a relative path to resolve against."""
    plan = build_plan(network, asset_tree)
    root = asset_tree.resolve().as_posix()
    assert _labelled(plan.runs[1].values(plan.driver, asset_tree)) == [
        ("Reference.file_path", f"{root}/assets/hero.usda"),
        ("Export.output_file", f"{root}/out/hero.usda"),
    ]


def test_a_runs_session_layer_holds_its_values_on_the_chunk_prims(network, asset_tree, tmp_path):
    """Opened as the session layer, the network composes with this run's values."""
    plan = build_plan(network, asset_tree)
    path = str(tmp_path / "run.usda")
    write_session(network, plan.runs[0].values(plan.driver), path)
    composed = Usd.Stage.Open(network.GetRootLayer(), Sdf.Layer.FindOrOpen(path))
    assert composed.GetAttributeAtPath("/chunks/Reference.inputs:file_path").Get() == "./assets/crate.usda"
    assert composed.GetAttributeAtPath("/chunks/Export.inputs:output_file").Get() == "out/crate.usda"


def test_plan_fills_the_output_pattern(network, asset_tree):
    plan = build_plan(network, asset_tree, output_pattern="baked/*.usdc")
    assert [r.output for r in plan.runs] == ["baked/crate.usdc", "baked/hero.usdc"]


def test_sink_wildcard_mismatch_is_reported_against_the_sink(network, asset_tree):
    network.GetPrimAtPath("/chunks/Export").GetAttribute("inputs:output_file").Set("out/*/*.usda")
    with pytest.raises(BatchError, match="Export.output_file"):
        build_plan(network, asset_tree)


def test_format_plan_names_the_anchor_and_every_run(network, asset_tree):
    text = format_plan(build_plan(network, asset_tree, output_pattern="o/*.usda"), "net.usda")
    assert f"Anchor: {asset_tree}" in text
    assert "Runs:   2" in text and "./assets/hero.usda -> o/hero.usda" in text


# ---------------------------------------------------------------------------
# overrides
# ---------------------------------------------------------------------------


def test_split_overrides_sorts_globs_from_values():
    patterns, plain = split_overrides(["Reference.file_path=lib/*.usda", "Export.flatten=1"])
    assert patterns == ["Reference.file_path=lib/*.usda"]
    assert plain == ["Export.flatten=1"]


def test_a_glob_override_can_become_the_driver(stage, asset_tree):
    """A network is worth pointing at another folder without editing it."""
    ref = _chunk_prim(stage, "/chunks/Reference", "reference")
    _set_str(ref, "file_path", "./elsewhere/fixed.usda")
    applied = apply_overrides(stage, ["Reference.file_path=./assets/*.usda"])
    assert applied == ["Reference.file_path = './assets/*.usda'"]
    assert build_plan(stage, asset_tree).driver.label == "Reference.file_path"
    in_network = stage.GetRootLayer().GetAttributeAtPath("/chunks/Reference.inputs:file_path").default
    assert in_network == "./elsewhere/fixed.usda", "overrides go in the session layer, not the network"


def test_a_glob_override_creates_a_param_left_at_its_default(stage, asset_tree):
    """A param at its default has no inputs: attribute -- an untouched Reference chunk."""
    _chunk_prim(stage, "/chunks/Reference", "reference")
    apply_overrides(stage, ["Reference.file_path=./assets/*.usda"])
    assert len(build_plan(stage, asset_tree).runs) == 2


def test_a_grouped_param_is_found_by_its_name_and_labelled_as_run_reads_it(stage, asset_tree):
    """A param inside a group is stored as inputs:Group:name; `run` reaches it as
    Group.name or by its bare name, and so does an override here."""
    ref = _chunk_prim(stage, "/chunks/Reference", "reference")
    _set_str(ref, "file_path", "./assets/*.usda")
    bake = _chunk_prim(stage, "/chunks/Bake", "bake_textures")
    _set_str(bake, "Output:output_dir", "fixed")
    assert apply_overrides(stage, ["Bake.output_dir=out/*_tex"]) == ["Bake.Output.output_dir = 'out/*_tex'"]
    plan = build_plan(stage, asset_tree)
    assert [s.label for s in plan.sinks] == ["Bake.Output.output_dir"]
    assert _labelled(plan.runs[0].values(plan.driver))[1] == ("Bake.Output.output_dir", "out/crate_tex")
    assert not bake.HasAttribute("inputs:output_dir"), "no stray ungrouped attribute"


def test_a_bare_override_cannot_invent_a_param(stage):
    _chunk_prim(stage, "/chunks/Reference", "reference")
    with pytest.raises(BatchError, match="qualify it"):
        apply_overrides(stage, ["file_path=./assets/*.usda"])


def test_an_override_naming_no_chunk_lists_the_real_ones(stage):
    _chunk_prim(stage, "/chunks/Reference", "reference")
    with pytest.raises(BatchError, match=r"names no chunk 'Nope' \(chunks: Reference\)"):
        apply_overrides(stage, ["Nope.file_path=./assets/*.usda"])


# ---------------------------------------------------------------------------
# what writes
# ---------------------------------------------------------------------------


class _Registry:
    """Just enough of ChunkRegistry: module name -> a module with or without commit."""

    def __init__(self, committing):
        self._committing = committing

    def get_chunk_module(self, name):
        module = SimpleNamespace(commit=lambda node: None) if name in self._committing else SimpleNamespace()
        return SimpleNamespace(module=module)


def test_committing_chunks_are_found_from_the_registry(network):
    assert committing_chunk_names(network, _Registry({"export_usd"})) == ["Export"]


def test_a_writer_with_a_per_run_path_passes(network, asset_tree):
    check_commit_targets(["Export"], build_plan(network, asset_tree))


def test_a_network_that_writes_nothing_is_refused(network, asset_tree):
    with pytest.raises(BatchError, match="nothing in this network writes"):
        check_commit_targets([], build_plan(network, asset_tree))


def test_a_writer_with_a_fixed_path_is_refused(network, asset_tree):
    """N cooks, N writes, one file left behind -- the silent failure this guards."""
    network.GetPrimAtPath("/chunks/Export").GetAttribute("inputs:output_file").Set("out/all.usda")
    with pytest.raises(BatchError, match="Export writes on every run but its output path never changes"):
        check_commit_targets(["Export"], build_plan(network, asset_tree))


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def ref_network(tmp_path):
    """A Reference chunk whose file path is a glob, saved beside two assets that differ."""
    assets = tmp_path / "assets"
    assets.mkdir()
    for name, size in (("small", 1.0), ("large", 3.0)):
        asset = Usd.Stage.CreateNew(str(assets / f"{name}.usda"))
        cube = UsdGeom.Cube.Define(asset, "/root")
        cube.CreateSizeAttr(size)
        asset.SetDefaultPrim(cube.GetPrim())
        asset.GetRootLayer().Save()

    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    init_chunk_usd(stage, scene)
    mod = registry.get_chunk_module("reference")
    chunks = mod.create() if mod.loaded else mod.load()
    node = chunks[0]
    node.name = "Reference"
    for c in chunks:
        scene.add_chunk(c)
    node.get_param("primpath_location").value = "/Asset"

    out = tmp_path / "net.usda"
    export_chunks(scene, str(out))
    # The glob goes straight into the saved file: the param only takes a path that exists,
    # and a glob names no single file. A network saved with one has to load anyway.
    saved = Usd.Stage.Open(str(out))
    prim = next(p for p in saved.Traverse() if p.GetName() == "Reference")
    prim.CreateAttribute("inputs:file_path", Sdf.ValueTypeNames.String).Set("./assets/*.usda")
    saved.GetRootLayer().Save()
    return out


def _batch(network, *extra):
    cmd = [sys.executable, "-m", "usd_chunks.cli", "batch", str(network), "--jobs", "2", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))


def _size(path):
    """The referenced cube's size at /Asset -- the stage held, or the prim dies with it."""
    stage = Usd.Stage.Open(str(path))
    return stage.GetPrimAtPath("/Asset").GetAttribute("size").Get()


def test_batch_writes_one_result_per_matched_file(ref_network, tmp_path):
    result = _batch(ref_network, "-o", "out/*.usda")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _size(tmp_path / "out" / "small.usda") == 1.0
    assert _size(tmp_path / "out" / "large.usda") == 3.0
    assert not Usd.Stage.Open(str(tmp_path / "out" / "small.usda")).GetPrimAtPath("/chunks"), (
        "an -o export is flattened without Chunk prims, as `run -o` is"
    )


def test_batch_leaves_the_network_file_untouched(ref_network):
    before = ref_network.read_text()
    assert _batch(ref_network, "-o", "out/*.usda").returncode == 0
    assert ref_network.read_text() == before


def test_a_glob_override_points_the_network_at_another_folder(ref_network, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (tmp_path / "assets" / "large.usda").rename(other / "moved.usda")
    result = _batch(ref_network, "Reference.file_path=./other/*.usda", "-o", "out/*.usda")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["moved.usda"]


_COUNT_COOKS = (
    "import sys, collections;"
    "from usd_chunks import chunk_cook, cli;"
    "cooks = collections.Counter();"
    "chunk_cook.after_cook(lambda c: cooks.update([c.name]));"
    "sys.argv = ['usdchunks'] + sys.argv[1:];"
    "cli.main();"
    "print('COOKS', dict(cooks))"
)


def _run_counting(*args):
    """`usdchunks run ...` in a fresh process, with how many times each chunk cooked."""
    out = subprocess.run([sys.executable, "-c", _COUNT_COOKS, "run", *args],
                         capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, out.stdout + out.stderr
    line = next(line for line in out.stdout.splitlines() if line.startswith("COOKS"))
    return eval(line[len("COOKS"):])  # noqa: S307 -- our own dict literal


def _saved_with_file(network, path):
    """The network with its Reference saved pointing at path, so file_path is authored."""
    saved = Usd.Stage.Open(str(network))
    prim = next(p for p in saved.Traverse() if p.GetName() == "Reference")
    prim.GetAttribute("inputs:file_path").Set(str(path))
    saved.GetRootLayer().Save()


def test_run_cooks_once_with_an_override(ref_network, tmp_path):
    """Applied after load, an override re-cooked a network load had already cooked with the
    saved value. Read at load from the session layer, the network cooks once."""
    _saved_with_file(ref_network, tmp_path / "assets" / "small.usda")
    out = tmp_path / "result.usda"
    cooks = _run_counting(str(ref_network), f"Reference.file_path={tmp_path / 'assets' / 'large.usda'}", "-o", str(out))
    assert cooks.get("Reference") == 1, cooks
    assert _size(out) == 3.0, "and with the override's file, not the saved one"


def test_an_override_load_cannot_take_still_applies_after_load(ref_network, tmp_path):
    """A param at its default has no attribute to author onto -- it is set after load, as before."""
    _saved_with_file(ref_network, tmp_path / "assets" / "small.usda")
    out = tmp_path / "result.usda"
    _run_counting(str(ref_network), "Reference.prim_type=Scope", "-o", str(out))
    stage = Usd.Stage.Open(str(out))
    assert stage.GetPrimAtPath("/Asset").GetTypeName() == "Scope"


def test_without_o_a_network_that_writes_nothing_is_refused_before_running(ref_network, tmp_path):
    result = _batch(ref_network)
    assert result.returncode == 1
    assert "nothing in this network writes" in result.stdout
    assert not (tmp_path / "out").exists()


def test_a_sink_can_use_the_driver_capture_twice(network, asset_tree):
    """proxies/<name>/<name>.usda: one capture, used twice."""
    network.GetPrimAtPath("/chunks/Export").GetAttribute("inputs:output_file").Set("proxies/{1}/{1}.usda")
    plan = build_plan(network, asset_tree, output_pattern="proxies/{1}/textures")
    assert plan.runs[0].sink_values[0][1] == "proxies/crate/crate.usda"
    assert plan.runs[0].output == "proxies/crate/textures"


def test_batch_relative_writes_a_library_that_moves(ref_network, tmp_path):
    """Default prim set, and every asset path relative: the folder can move or be shared."""
    result = _batch(ref_network, "-o", "lib/{1}/{1}.usda", "--relative")
    assert result.returncode == 0, result.stdout + result.stderr
    out = tmp_path / "lib" / "small" / "small.usda"
    layer = Sdf.Layer.FindOrOpen(str(out))
    assert layer.defaultPrim == "Asset"
    assert layer.GetPrimAtPath("/Asset").referenceList.GetAddedOrExplicitItems()[0].assetPath == "../../assets/small.usda"
    assert _size(out) == 1.0

    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(tmp_path / "lib", elsewhere / "lib")
    shutil.copytree(tmp_path / "assets", elsewhere / "assets")
    assert _size(elsewhere / "lib" / "small" / "small.usda") == 1.0, "moved together, it still composes"
