"""The Value Clips chunk stacks clip sets, and a sparse clip on top changes only what it names.

Two clips stand in for a walk cycle and a wave: `walk.usda` authors a full three-element
`rotations` array every frame, `wave.usda` authors an array *edit* that writes element 2
and nothing else. Stacked wave-over-walk, elements 0 and 1 must keep walking while
element 2 waves -- the OpenUSD 26.03 sparse array-edit override composing across clip
sets. Row order is the strength, and the chunk has to say so with a `clipSets` list-op,
because USD's own default is alphabetical (walk beats wave).
"""
from __future__ import annotations

import os

import pytest
from pxr import Gf, Sdf, Usd, UsdSkel, Vt

from usd_chunks import chunk as _chunk
from usd_chunks import chunk_registry
from usd_chunks import chunk_widget_api
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

import scripts.usd.value_clips as value_clips

_CUBE_CLIPS = os.path.join(os.path.dirname(__file__), "..", "scripts", "usd",
                           "test_value_clips", "clips", "clip.###.usda")


# Sparse array edits (Vt.*ArrayEditBuilder) arrived in USD 26. Older builds -- the 25.x
# usdviews still in use -- lack the API entirely, so these tests cannot run there; a skip
# says why, where an AttributeError per test (and a cascade of knock-on failures in the
# tests after it) would bury any real regression in the rest of the suite.
needs_array_edits = pytest.mark.skipif(
    not hasattr(Vt, "QuatfArrayEditBuilder"),
    reason="needs USD 26+ (Vt.QuatfArrayEditBuilder, sparse array edits)",
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _make_scene(stage: Usd.Stage) -> SceneSender:
    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return scene


def _make_chunk(scene: SceneSender):
    mod = chunk_registry.ChunkRegistry().get_chunk_module("usd.value_clips")
    chunks = mod.create() if mod.loaded else mod.load()
    node = chunks[0]
    # Chain the way the Params tab does (prev -> new via inputs:chunk), so a second chunk
    # is downstream of the first and its layer is the stronger one.
    prev = chunk_stage._params_tab_create
    chunk_stage._params_tab_create = True
    try:
        scene.add_chunk(node)
    finally:
        chunk_stage._params_tab_create = prev
    return node


def _set_rows(node, rows: list[dict]):
    """Fill the clip-set list, blocked so the chunk cooks once at the end, not per field."""
    group = node.get_param("clip_sets", _chunk.is_inst(_chunk.ListGroup))
    node.set_blocked(True)
    try:
        group.size = len(rows)
        for i, row in enumerate(rows):
            for key, value in row.items():
                group.get_indexed_param(key, i).value = value
    finally:
        node.set_blocked(False)
    node.execute()


@pytest.fixture
def clips(tmp_path):
    """walk.usda: full arrays on frames 0..4. wave.usda: `write` edits to element 2 only."""
    walk = Sdf.Layer.CreateNew(str(tmp_path / "walk.usda"))
    stage = Usd.Stage.Open(walk)
    anim = UsdSkel.Animation.Define(stage, "/Anim")
    for f in range(5):
        anim.GetRotationsAttr().Set(
            Vt.QuatfArray([Gf.Quatf(f, 0, 0, 0), Gf.Quatf(f, 1, 0, 0), Gf.Quatf(f, 2, 0, 0)]), f)
    stage.SetDefaultPrim(anim.GetPrim())
    walk.Save()

    wave = Sdf.Layer.CreateNew(str(tmp_path / "wave.usda"))
    stage = Usd.Stage.Open(wave)
    anim = UsdSkel.Animation.Define(stage, "/Anim")
    for f in range(5):
        builder = Vt.QuatfArrayEditBuilder()
        builder.Write(Gf.Quatf(100 + f, 0, 0, 1), 2)
        anim.GetRotationsAttr().Set(builder.FinalizeAndReset(), f)
    stage.SetDefaultPrim(anim.GetPrim())
    wave.Save()
    assert "edit [write" in wave.ExportToString(), "the wave must be sparse, or the test proves nothing"
    return str(tmp_path / "walk.usda").replace("\\", "/"), str(tmp_path / "wave.usda").replace("\\", "/")


def _reals(attr, frame):
    return [round(q.GetReal(), 3) for q in attr.Get(frame)]


@needs_array_edits
class TestStacking:
    def test_wave_over_walk_changes_only_the_element_it_writes(self, clips):
        walk, wave = clips
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/Char/Anim", "SkelAnimation")
        scene = _make_scene(stage)
        node = _make_chunk(scene)
        node.get_param("target_prim").value = "/Char/Anim"
        _set_rows(node, [
            {"clip_path": wave, "start_frame": 0},
            {"clip_path": walk, "start_frame": 0},
        ])

        rotations = stage.GetAttributeAtPath("/Char/Anim.rotations")
        assert _reals(rotations, 2) == [2, 2, 102]
        assert _reals(rotations, 1.5) == [1.5, 1.5, 101.5]

    def test_row_order_is_strength_and_is_authored_explicitly(self, clips):
        walk, wave = clips
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/Char/Anim", "SkelAnimation")
        scene = _make_scene(stage)
        node = _make_chunk(scene)
        node.get_param("target_prim").value = "/Char/Anim"
        _set_rows(node, [
            {"clip_path": walk, "start_frame": 0},
            {"clip_path": wave, "start_frame": 0},
        ])

        rotations = stage.GetAttributeAtPath("/Char/Anim.rotations")
        assert _reals(rotations, 2) == [2, 2, 2], "walk on top hides the wave entirely"
        spec = node.get_edit_context().GetPrimAtPath("/Char/Anim")
        assert list(spec.GetInfo("clipSets").prependedItems) == ["walk", "wave"]

    def test_two_chunks_on_one_prim_compose(self, clips):
        """One chunk per clip set -- a crowd rig adds gestures as chunks -- must stack, not
        clobber: an explicit clipSets list in the stronger chunk's layer would replace the
        weaker chunk's list and drop its sets from resolution entirely."""
        walk, wave = clips
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/Char/Anim", "SkelAnimation")
        scene = _make_scene(stage)
        first = _make_chunk(scene)
        first.get_param("target_prim").value = "/Char/Anim"
        _set_rows(first, [{"clip_path": walk, "start_frame": 0}])
        second = _make_chunk(scene)
        second.get_param("target_prim").value = "/Char/Anim"
        _set_rows(second, [{"clip_path": wave, "start_frame": 0}])

        rotations = stage.GetAttributeAtPath("/Char/Anim.rotations")
        assert _reals(rotations, 2) == [2, 2, 102], "the later (stronger) chunk's wave sits over the first chunk's walk"

    def test_names_come_from_the_file_and_everything_lands_on_the_chunk_layer(self, clips):
        walk, wave = clips
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/Char/Anim", "SkelAnimation")
        scene = _make_scene(stage)
        node = _make_chunk(scene)
        node.get_param("target_prim").value = "/Char/Anim"
        _set_rows(node, [{"clip_path": wave, "start_frame": 0}, {"clip_path": walk, "start_frame": 0}])

        layer = node.get_edit_context()
        spec = layer.GetPrimAtPath("/Char/Anim")
        assert spec is not None and set(spec.GetInfo("clips").keys()) == {"walk", "wave"}
        assert stage.GetSessionLayer().GetPrimAtPath("/Char/Anim") is None
        assert spec.GetInfo("clips")["walk"]["primPath"] == "/Anim", "a SkelAnimation target takes the clip's single SkelAnimation"

    def test_a_skel_target_finds_the_clips_animation_prim_wherever_it_is(self, tmp_path):
        """A DCC names the animation prim after the action, under the character -- so the
        defaultPrim (the character root) is the wrong answer and typing the path per row
        was the only way. A SkelAnimation target now takes the clip's one SkelAnimation."""
        layer = Sdf.Layer.CreateNew(str(tmp_path / "cycle.usda"))
        st = Usd.Stage.Open(layer)
        anim = UsdSkel.Animation.Define(st, "/root/RIG/Skel/Sprite_Walk")
        anim.GetRotationsAttr().Set(Vt.QuatfArray([Gf.Quatf(1, 0, 0, 0)]), 0)
        anim.GetRotationsAttr().Set(Vt.QuatfArray([Gf.Quatf(1, 0, 0, 0)]), 1)
        st.SetDefaultPrim(st.GetPrimAtPath("/root"))
        layer.Save()

        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/Char/Anim", "SkelAnimation")
        stage.DefinePrim("/Char/Mesh", "Mesh")
        scene = _make_scene(stage)
        node = _make_chunk(scene)
        node.get_param("target_prim").value = "/Char/Anim"
        _set_rows(node, [{"clip_path": str(tmp_path / "cycle.usda").replace("\\", "/"), "start_frame": 0}])
        clips = node.get_edit_context().GetPrimAtPath("/Char/Anim").GetInfo("clips")
        assert clips["cycle"]["primPath"] == "/root/RIG/Skel/Sprite_Walk"

        node.get_param("target_prim").value = "/Char/Mesh"
        node.execute()
        clips = node.get_edit_context().GetPrimAtPath("/Char/Mesh").GetInfo("clips")
        assert clips["cycle"]["primPath"] == "/root", "any other target still takes the defaultPrim"


@needs_array_edits
class TestTiming:
    def test_loop_repeats_the_clip_with_jump_discontinuities(self, clips):
        walk, _ = clips
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/Char/Anim", "SkelAnimation")
        scene = _make_scene(stage)
        node = _make_chunk(scene)
        node.get_param("target_prim").value = "/Char/Anim"
        _set_rows(node, [{"clip_path": walk, "start_frame": 10, "end_frame": 18, "loop": True}])

        rotations = stage.GetAttributeAtPath("/Char/Anim.rotations")
        assert _reals(rotations, 10) == [0, 0, 0]
        assert _reals(rotations, 13) == [3, 3, 3]
        assert _reals(rotations, 14) == [0, 0, 0], "frame 14 is the jump back to the clip's first frame"
        assert _reals(rotations, 17) == [3, 3, 3]
        assert stage.GetStartTimeCode() == 10 and stage.GetEndTimeCode() == 18

    def test_end_zero_means_the_clips_own_length(self, clips):
        walk, _ = clips
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/Char/Anim", "SkelAnimation")
        scene = _make_scene(stage)
        node = _make_chunk(scene)
        node.get_param("target_prim").value = "/Char/Anim"
        _set_rows(node, [{"clip_path": walk, "start_frame": 100}])

        rotations = stage.GetAttributeAtPath("/Char/Anim.rotations")
        assert _reals(rotations, 104) == [4, 4, 4]
        assert stage.GetStartTimeCode() == 100 and stage.GetEndTimeCode() == 104

    def test_stage_range_widens_and_never_narrows(self, clips):
        walk, _ = clips
        stage = Usd.Stage.CreateInMemory()
        stage.SetStartTimeCode(90)
        stage.SetEndTimeCode(95)
        stage.DefinePrim("/Char/Anim", "SkelAnimation")
        scene = _make_scene(stage)
        node = _make_chunk(scene)
        node.get_param("target_prim").value = "/Char/Anim"
        _set_rows(node, [{"clip_path": walk, "start_frame": 100}])
        assert (stage.GetStartTimeCode(), stage.GetEndTimeCode()) == (90, 104)

    def test_loop_times_layout(self):
        assert value_clips._loop_times(0, 4, 0, 2) == [(0, 0), (2, 2), (2, 0), (4, 2)]
        assert value_clips._loop_times(101, 158, 101, 129)[-1] == (185, 129)


class TestTemplate:
    def test_template_reads_its_range_off_disk(self):
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/CubeGeo", "Mesh")
        scene = _make_scene(stage)
        node = _make_chunk(scene)
        node.get_param("target_prim").value = "/CubeGeo"
        _set_rows(node, [{"clip_path": os.path.abspath(_CUBE_CLIPS).replace("\\", "/"), "start_frame": 0}])

        info = Usd.ClipsAPI(stage.GetPrimAtPath("/CubeGeo")).GetClips()["clip"]
        assert (info["templateStartTime"], info["templateEndTime"]) == (1.0, 10.0)
        assert "active" not in info, "USD derives active from a template and ignores an authored one"
        points = stage.GetAttributeAtPath("/CubeGeo.points")
        assert points.Get(1)[4][1] == pytest.approx(0.5)
        assert points.Get(10)[4][1] == pytest.approx(3.0)

    def test_default_names(self):
        assert value_clips._default_name("C:/x/HumanFemale.walk.usd") == "walk"
        assert value_clips._default_name("./clips/clip.###.usda") == "clip"
        assert value_clips._default_name("sim.####.usdc") == "sim"


class TestClipPathBrowser:
    """The Clip field is FILE_EXISTING, so its browse dialog opens in Open (not Save)
    mode -- reported live as showing a Save button. That flag also gates whether a typed
    value must exist on disk, which conflicts with this same field's other job: taking a
    #-padded template path that, by construction, never exists as one literal file."""

    def test_dialog_opens_in_open_mode(self):
        node = value_clips.main()
        rows = node.get_param("clip_sets", _chunk.is_inst(_chunk.ListGroup))
        clip_param = rows.get_param("clip_path")
        assert clip_param.has_flag(ChunkFlags.ParamFlags.FILE_EXISTING)
        spec = chunk_widget_api.file_dialog_spec(clip_param)
        assert spec.must_exist, "Clip must open a file picker in Open mode, not Save"

    def test_typing_a_template_path_is_not_blocked_by_file_existing(self):
        fb = _chunk.FileBrowser("clip_path", "Clip", extensions=[".usda"],
                                flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.FILE_EXISTING)
        fb.value = "C:/cache/sim.####.usdc"
        assert fb.value == "C:/cache/sim.####.usdc"

    def test_a_real_nonexistent_path_is_still_rejected(self):
        fb = _chunk.FileBrowser("clip_path", "Clip", extensions=[".usda"],
                                flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.FILE_EXISTING)
        with pytest.raises(ValueError):
            fb.value = "C:/definitely/not/a/real/path.usda"
