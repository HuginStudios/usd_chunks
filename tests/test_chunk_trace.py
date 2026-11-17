"""A selected prim names the chunk that made it and the chunks that changed it, and a chunk
names the prims it made or changed: read from each chunk's own layer, nothing indexed."""
from __future__ import annotations

import pytest
from pxr import Sdf

from usd_chunks import Scene, chunk_trace


def _asset(folder) -> str:
    path = folder / "rock.usda"
    layer = Sdf.Layer.CreateNew(str(path))
    layer.ImportFromString("\n".join([
        "#usda 1.0", '(defaultPrim = "rock")', 'def Xform "rock"', "{", '    def Mesh "geo"', "    {", "    }", "}", ""]))
    layer.Save()
    return str(path)


def _names(chunks) -> list[str]:
    return [c.name for c in chunks]


def test_a_prim_names_its_maker_then_its_changers_in_the_order_they_apply(tmp_path) -> None:
    with Scene() as scene:
        box = scene.create.cube(prim_path="/World/box", width=2.0)
        moved = scene.create.transform(box, primpath_location="/World/box", ty=3.0)
        coloured = scene.create.set_display_color(moved, prim_path="/World/box", color_r=1.0)
        stage = scene.get_stage()
        trace = chunk_trace.trace_prim(stage.GetPrimAtPath("/World/box"))
        assert trace.made_by.name == box.name
        assert _names(trace.changed_by) == [moved.name, coloured.name]
        assert trace.via is None
        assert _names(trace.chunks) == [box.name, moved.name, coloured.name]


def test_a_prim_inside_a_referenced_asset_names_the_chunk_that_brought_it_in(tmp_path) -> None:
    asset = _asset(tmp_path)
    with Scene() as scene:
        ref = scene.create.reference(file_path=asset, primpath_location="/World/rock")
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/World/rock/geo"), "the asset did not come in"
        inside = chunk_trace.trace_prim(stage.GetPrimAtPath("/World/rock/geo"))
        assert inside.made_by is None and inside.changed_by == ()
        assert inside.via[0] == Sdf.Path("/World/rock") and _names(inside.via[1]) == [ref.name]
        assert _names(inside.chunks) == [ref.name]
        assert chunk_trace.trace_prim(stage.GetPrimAtPath("/World/rock")).made_by.name == ref.name


def test_a_chunk_names_the_prims_it_made_or_changed(tmp_path) -> None:
    asset = _asset(tmp_path)
    with Scene() as scene:
        box = scene.create.cube(prim_path="/World/box", width=2.0)
        moved = scene.create.transform(box, primpath_location="/World/box", ty=3.0)
        ref = scene.create.reference(moved, file_path=asset, primpath_location="/World/rock")
        stage = scene.get_stage()
        assert chunk_trace.chunk_prims(box, stage) == ([Sdf.Path("/World/box")], False)
        assert chunk_trace.chunk_prims(moved, stage) == ([Sdf.Path("/World/box")], False)
        # /World is only made sure to exist by the later chunks: not something they made.
        assert chunk_trace.chunk_prims(ref, stage) == ([Sdf.Path("/World/rock")], False)
        paths, more = chunk_trace.chunk_prims(box, stage, limit=0)
        assert paths == [] and more


def test_a_prim_no_chunk_touched_has_an_empty_trace(tmp_path) -> None:
    with Scene() as scene:
        scene.create.cube(prim_path="/World/box", width=2.0)
        stage = scene.get_stage()
        stage.DefinePrim("/Elsewhere")
        assert chunk_trace.trace_prim(stage.GetPrimAtPath("/Elsewhere")).chunks == ()


def test_scripts_ask_the_scene_which_chunks_made_a_prim(tmp_path) -> None:
    with Scene() as scene:
        box = scene.create.cube(prim_path="/World/box", width=2.0)
        lift = scene.create.transform(box, primpath_location="/World/box", ty=3.0)
        trace = scene.trace_prim("/World/box")
        assert trace.made_by.name == box.name and _names(trace.changed_by) == [lift.name]
        assert scene.get_chunk_prims(lift) == [Sdf.Path("/World/box")]
        assert _names(scene.get_chunk_stack("/World/box")) == [lift.name, box.name]
        scene.set_selected_chunks(scene.get_chunk_stack("/World/box"))
        assert set(_names(scene.get_selected_chunks())) == {lift.name, box.name}
        with pytest.raises(ValueError, match="/World/nothing"):
            scene.trace_prim("/World/nothing")
