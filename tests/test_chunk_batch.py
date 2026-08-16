"""Planner tests for `usdchunks batch` — driver selection, sink filling and the
command-line overrides, all at the USD level.

The pure glob math is covered separately in test_chunk_glob.py. What is exercised here is
the part that reads a real stage: which attributes count as pattern sites, which single one
of them drives the batch, and what actually gets authored per run.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, Vt

from usd_chunks.plugins.chunk_batch import (
    BatchError,
    apply_overrides,
    apply_run,
    build_plan,
    check_commit_targets,
    committing_chunks,
    find_pattern_sites,
    format_plan,
    select_driver,
)


def _chunk_prim(stage, path, chunk_type):
    prim = stage.DefinePrim(path, "Chunk")
    prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set(chunk_type)
    return prim


def _set_str(prim, name, value):
    prim.CreateAttribute(f"inputs:{name}", Sdf.ValueTypeNames.String).Set(value)


def _set_str_array(prim, name, values):
    attr = prim.CreateAttribute(f"inputs:{name}", Sdf.ValueTypeNames.StringArray)
    attr.Set(Vt.StringArray(values))
    return attr


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


def test_finds_scalar_patterns_only_where_they_exist(network):
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
    """An expression's value is computed from another chunk's param — overwriting it with a
    batch path would destroy the wiring it stands for."""
    ref = _chunk_prim(stage, "/chunks/Reference", "reference")
    _set_str(ref, "primpath_location", "@Prim.primpath_location@{test=A}/geo*")
    assert find_pattern_sites(stage) == []


def test_array_entries_are_pattern_sites(stage):
    sub = _chunk_prim(stage, "/chunks/Sublayer", "sublayer")
    _set_str_array(sub, "imports:sublayer_path", ["lib/*.usda", "fixed/base.usda"])
    sites = find_pattern_sites(stage)
    assert [s.label for s in sites] == ["Sublayer.imports:sublayer_path[0]"]
    assert sites[0].index == 0


# ---------------------------------------------------------------------------
# select_driver
# ---------------------------------------------------------------------------


def test_driver_is_the_pattern_matching_real_files(network, asset_tree):
    sites = find_pattern_sites(network)
    driver, matches = select_driver(sites, asset_tree)
    assert driver.label == "Reference.file_path"
    assert [path for path, _ in matches] == ["./assets/crate.usda", "./assets/hero.usda"]


def test_two_matching_patterns_is_a_hard_error(network, asset_tree):
    """Both name real files, so which one is the input is genuinely unknowable."""
    export = network.GetPrimAtPath("/chunks/Export")
    export.GetAttribute("inputs:output_file").Set("./assets/*.usda")

    with pytest.raises(BatchError) as exc:
        select_driver(find_pattern_sites(network), asset_tree)

    message = str(exc.value)
    assert "Reference.file_path" in message and "Export.output_file" in message
    assert "--driver" in message


def test_explicit_driver_resolves_ambiguity(network, asset_tree):
    export = network.GetPrimAtPath("/chunks/Export")
    export.GetAttribute("inputs:output_file").Set("./assets/*.usda")

    driver, matches = select_driver(
        find_pattern_sites(network), asset_tree, driver="Export.output_file"
    )
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


def test_plan_fills_the_output_pattern(network, asset_tree):
    plan = build_plan(network, asset_tree, output_pattern="baked/*.usdc")
    assert [r.output for r in plan.runs] == ["baked/crate.usdc", "baked/hero.usdc"]


def test_sink_wildcard_mismatch_is_reported_against_the_sink(network, asset_tree):
    export = network.GetPrimAtPath("/chunks/Export")
    export.GetAttribute("inputs:output_file").Set("out/*/*.usda")
    with pytest.raises(BatchError, match="Export.output_file"):
        build_plan(network, asset_tree)


def test_plan_header_names_the_anchor(network, asset_tree):
    """Patterns resolving to the .usda rather than the shell's cwd surprises people once."""
    text = format_plan(build_plan(network, asset_tree), "convert.usda")
    assert str(asset_tree) in text
    assert "Reference.file_path" in text


# ---------------------------------------------------------------------------
# apply_run
# ---------------------------------------------------------------------------


def test_apply_run_authors_into_the_session_layer(network, asset_tree):
    plan = build_plan(network, asset_tree)
    apply_run(network, plan, plan.runs[1])

    assert network.GetAttributeAtPath("/chunks/Reference.inputs:file_path").Get() == "./assets/hero.usda"
    assert network.GetAttributeAtPath("/chunks/Export.inputs:output_file").Get() == "out/hero.usda"
    # The file on disk must be left exactly as authored — only the session layer moves.
    assert network.GetSessionLayer().GetAttributeAtPath("/chunks/Reference.inputs:file_path")
    assert not network.GetRootLayer().GetAttributeAtPath("/chunks/Reference.inputs:file_path").default.endswith("hero.usda")


def test_apply_run_replaces_one_array_entry_and_leaves_the_rest(stage, asset_tree):
    sub = _chunk_prim(stage, "/chunks/Sublayer", "sublayer")
    _set_str_array(sub, "imports:sublayer_path", ["./assets/*.usda", "keep/me.usda"])

    plan = build_plan(stage, asset_tree)
    apply_run(stage, plan, plan.runs[0])

    entries = list(stage.GetAttributeAtPath("/chunks/Sublayer.inputs:imports:sublayer_path").Get())
    assert entries == ["./assets/crate.usda", "keep/me.usda"]


# ---------------------------------------------------------------------------
# apply_overrides
# ---------------------------------------------------------------------------


def test_override_retargets_the_driver(network, asset_tree):
    applied = apply_overrides(network, ["Reference.file_path=./elsewhere/*.usd"])
    assert applied == ["Reference.file_path = './elsewhere/*.usd'"]
    assert network.GetAttributeAtPath("/chunks/Reference.inputs:file_path").Get() == "./elsewhere/*.usd"


def test_bare_param_name_applies_without_a_chunk(network):
    apply_overrides(network, ["file_path=./other/*.usd"])
    assert network.GetAttributeAtPath("/chunks/Reference.inputs:file_path").Get() == "./other/*.usd"


def test_override_targets_one_array_entry(stage):
    sub = _chunk_prim(stage, "/chunks/Sublayer", "sublayer")
    _set_str_array(sub, "imports:sublayer_path", ["a.usda", "b.usda"])

    apply_overrides(stage, ["Sublayer.imports:sublayer_path[1]=lib/*.usda"])
    entries = list(stage.GetAttributeAtPath("/chunks/Sublayer.inputs:imports:sublayer_path").Get())
    assert entries == ["a.usda", "lib/*.usda"]


def test_array_override_without_an_index_is_refused(stage):
    sub = _chunk_prim(stage, "/chunks/Sublayer", "sublayer")
    _set_str_array(sub, "imports:sublayer_path", ["a.usda"])
    with pytest.raises(BatchError, match=r"\[0\]"):
        apply_overrides(stage, ["Sublayer.imports:sublayer_path=lib/*.usda"])


def test_array_override_out_of_range(stage):
    sub = _chunk_prim(stage, "/chunks/Sublayer", "sublayer")
    _set_str_array(sub, "imports:sublayer_path", ["a.usda"])
    with pytest.raises(BatchError, match="out of range"):
        apply_overrides(stage, ["Sublayer.imports:sublayer_path[3]=lib/*.usda"])


@pytest.mark.parametrize(
    "text, expected",
    [("1", True), ("true", True), ("YES", True), ("on", True),
     ("0", False), ("false", False), ("no", False), ("Off", False)],
)
def test_bool_override_is_coerced_not_passed_as_a_string(stage, text, expected):
    """Everything arrives from argv as a string; a Checkbox param is a bool in USD, and
    handing it "1" raises a raw type-mismatch exception instead of doing the obvious."""
    export = _chunk_prim(stage, "/chunks/Export", "export_usd")
    export.CreateAttribute("inputs:flatten", Sdf.ValueTypeNames.Bool).Set(False)

    apply_overrides(stage, [f"Export.flatten={text}"])
    assert stage.GetAttributeAtPath("/chunks/Export.inputs:flatten").Get() is expected


def test_unreadable_bool_override_is_refused_clearly(stage):
    export = _chunk_prim(stage, "/chunks/Export", "export_usd")
    export.CreateAttribute("inputs:flatten", Sdf.ValueTypeNames.Bool).Set(False)
    with pytest.raises(BatchError, match="true/false"):
        apply_overrides(stage, ["Export.flatten=perhaps"])


def test_numeric_overrides_are_coerced(stage):
    prim = _chunk_prim(stage, "/chunks/Thing", "thing")
    prim.CreateAttribute("inputs:count", Sdf.ValueTypeNames.Int).Set(1)
    prim.CreateAttribute("inputs:scale", Sdf.ValueTypeNames.Float).Set(1.0)

    apply_overrides(stage, ["Thing.count=7", "Thing.scale=2.5"])
    assert stage.GetAttributeAtPath("/chunks/Thing.inputs:count").Get() == 7
    assert stage.GetAttributeAtPath("/chunks/Thing.inputs:scale").Get() == pytest.approx(2.5)


def test_non_numeric_override_of_a_number_is_refused(stage):
    prim = _chunk_prim(stage, "/chunks/Thing", "thing")
    prim.CreateAttribute("inputs:count", Sdf.ValueTypeNames.Int).Set(1)
    with pytest.raises(BatchError, match="expects int"):
        apply_overrides(stage, ["Thing.count=lots"])


def test_token_array_entry_keeps_its_array_type(stage):
    """A token[] param rebuilt as a StringArray is a type mismatch, not a conversion."""
    prim = _chunk_prim(stage, "/chunks/Sublayer", "sublayer")
    attr = prim.CreateAttribute("inputs:imports:kinds", Sdf.ValueTypeNames.TokenArray)
    attr.Set(Vt.TokenArray(["a", "b"]))

    apply_overrides(stage, ["Sublayer.imports:kinds[0]=z"])
    assert list(stage.GetAttributeAtPath("/chunks/Sublayer.inputs:imports:kinds").Get()) == ["z", "b"]


def test_override_of_an_unauthored_param_is_refused(network):
    """Type is only knowable from what the network already authored — creating the attribute
    here would guess String for something that might be a float."""
    with pytest.raises(BatchError, match="names no param"):
        apply_overrides(network, ["Reference.not_a_param=whatever"])


@pytest.mark.parametrize("bad", ["no_equals_sign", "=novalue"])
def test_malformed_overrides_are_refused(network, bad):
    with pytest.raises(BatchError, match="malformed"):
        apply_overrides(network, [bad])


def test_no_overrides_is_a_no_op(network):
    assert apply_overrides(network, []) == []


# ---------------------------------------------------------------------------
# commit targets
#
# Only .name and ._module are read, so plain stand-ins are enough and keep these
# tests away from a live registry.
# ---------------------------------------------------------------------------


class _Module:
    def __init__(self, commits):
        if commits:
            self.commit = lambda node: None


class _Chunk:
    def __init__(self, name, commits=False):
        self.name = name
        self._module = _Module(commits)


def test_committing_chunks_picks_only_those_with_the_hook():
    chunks = [_Chunk("Reference"), _Chunk("Export", commits=True)]
    assert [c.name for c in committing_chunks(chunks)] == ["Export"]


def test_committing_chunks_tolerates_a_chunk_with_no_module():
    stray = _Chunk("Odd")
    stray._module = None
    assert committing_chunks([stray]) == []


def test_commit_targets_accepts_an_exporter_that_owns_a_sink(network, asset_tree):
    plan = build_plan(network, asset_tree)
    chunks = [_Chunk("Reference"), _Chunk("Export", commits=True)]
    assert [c.name for c in check_commit_targets(chunks, plan)] == ["Export"]


def test_commit_targets_rejects_an_exporter_with_a_fixed_path(network, asset_tree):
    """The quiet-wrong-answer case: every run cooks and writes, and the folder ends up
    holding one file — the last asset — with nothing reporting a problem."""
    network.GetPrimAtPath("/chunks/Export").GetAttribute("inputs:output_file").Set("out/all.usda")
    plan = build_plan(network, asset_tree, output_pattern="ignored/*.usda")

    with pytest.raises(BatchError) as exc:
        check_commit_targets([_Chunk("Export", commits=True)], plan)
    assert "Export" in str(exc.value) and "overwrite the same file" in str(exc.value)


def test_commit_targets_rejects_a_network_that_writes_nothing(network, asset_tree):
    plan = build_plan(network, asset_tree)
    with pytest.raises(BatchError, match="nothing in this network writes to disk"):
        check_commit_targets([_Chunk("Reference")], plan)


def test_override_then_plan_makes_the_override_the_driver(network, asset_tree):
    """The whole point of applying overrides before planning: one network, any folder."""
    network.GetAttributeAtPath("/chunks/Reference.inputs:file_path").Set("./nowhere/*.usda")
    apply_overrides(network, ["Reference.file_path=./assets/*.usda"])

    plan = build_plan(network, asset_tree)
    assert plan.driver.label == "Reference.file_path"
    assert [r.source for r in plan.runs] == ["./assets/crate.usda", "./assets/hero.usda"]
