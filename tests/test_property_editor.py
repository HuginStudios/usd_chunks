"""The Property Editor keeps one edit list, not tied to a prim, and writes it onto its targets.

Before, each edit was stored under the prim's name (inputs:dyn_grp:<prim>:<attr>). So a
duplicate came out with no edits, changing Prim dropped them, and every attribute that
already had a value got pinned in the chunk's layer, touched or not.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk_stage
from usd_chunks.graph import engine


def _widget(node, name):
    """The widget for a property, by its base name."""
    for _, p in node.get_param("dyn_grp").iter_hierarchy():
        if getattr(p, "name", None) == name and "property_editor.prop" in p.user_data:
            return p
    raise LookupError(name)


def _spheres(stage, *paths):
    for i, path in enumerate(paths):
        sphere = UsdGeom.Sphere.Define(stage, path)
        sphere.AddTranslateOp().Set((float(i), 0.0, 0.0))
        sphere.GetPrim().CreateAttribute("weight", Sdf.ValueTypeNames.Float).Set(1.0)


def _get(stage, path, attr):
    return stage.GetPrimAtPath(path).GetAttribute(attr).Get()


def _duplicate(scene, node):
    """Through the engine's copy and paste, which the synchronous frontends use."""
    stage = scene.get_stage()
    path = chunk_stage.get_chunk_prim_path(node)
    nodes, edges = engine.snapshot_selection(
        {path: node}, {path: stage.GetPrimAtPath(path)}, {path: (0.0, 0.0)}, scene, stage, {path},
    )
    return engine.paste_nodes(scene, stage, nodes, edges, to_usd=lambda x, y: (x + 100.0, y))[0]


def test_an_edit_lands_on_the_prim():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a"

        _widget(editor, "weight").value = 5.0
        _widget(editor, "visibility").value = "invisible"

        assert _get(stage, "/a", "weight") == pytest.approx(5.0)
        assert _get(stage, "/a", "visibility") == "invisible"


def test_a_duplicate_makes_the_same_edits_on_its_new_prim():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a", "/b")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a"
        _widget(editor, "weight").value = 5.0
        _widget(editor, "radius").value = 3.0

        copy = _duplicate(scene, editor)
        copy.get_param("prim").value = "/b"

        assert _get(stage, "/b", "weight") == pytest.approx(5.0)
        assert _get(stage, "/b", "radius") == pytest.approx(3.0)
        assert _get(stage, "/a", "weight") == pytest.approx(5.0), "the original keeps its edit"


def test_one_edit_applies_to_every_target():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a", "/b", "/c")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a,/b,/c"

        _widget(editor, "weight").value = 7.0

        assert [_get(stage, p, "weight") for p in ("/a", "/b", "/c")] == [7.0, 7.0, 7.0]


def test_a_prim_taken_out_of_prim_loses_the_edits():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a", "/b")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a"
        _widget(editor, "weight").value = 5.0

        editor.get_param("prim").value = "/b"

        assert _get(stage, "/a", "weight") == pytest.approx(1.0)
        assert _get(stage, "/b", "weight") == pytest.approx(5.0)


def test_an_untouched_attribute_is_not_written():
    """An attribute with a value was pinned in the chunk's layer just by being shown."""
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a", "/b")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a"
        _widget(editor, "weight").value = 5.0

        spec = editor.get_edit_context().GetPrimAtPath("/a")
        assert spec is not None and list(spec.properties.keys()) == ["weight"]

        # And a copy moved to /b does not carry /a's translate with it.
        copy = _duplicate(scene, editor)
        copy.get_param("prim").value = "/b"
        assert _get(stage, "/b", "xformOp:translate")[0] == pytest.approx(1.0)


def test_the_widgets_are_not_saved_on_the_chunk_prim():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a"
        _widget(editor, "weight").value = 5.0
        _widget(editor, "translate").value = (4.0, 0.0, 0.0)

        prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(editor))
        inputs = [n for n in prim.GetAuthoredPropertyNames() if n.startswith("inputs:")]
        assert sorted(inputs) == ["inputs:edits_grp:edits", "inputs:prim"]


def test_clear_edits_and_deleting_a_line():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a"
        _widget(editor, "weight").value = 5.0
        _widget(editor, "radius").value = 3.0

        edits = editor.get_param("edits")
        edits.value = "\n".join(line for line in edits.value.splitlines() if not line.startswith("weight"))
        assert _get(stage, "/a", "weight") == pytest.approx(1.0)
        assert _get(stage, "/a", "radius") == pytest.approx(3.0)
        assert _widget(editor, "weight").value == pytest.approx(1.0), "the widget shows the prim again"

        editor.get_param("clear_edits").click()
        assert _get(stage, "/a", "radius") == pytest.approx(1.0)
        assert edits.value == ""


def test_a_prim_without_the_attribute_is_skipped():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a")
        UsdGeom.Xform.Define(stage, "/x")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a,/x"

        _widget(editor, "radius").value = 3.0

        assert _get(stage, "/a", "radius") == pytest.approx(3.0)
        assert not stage.GetPrimAtPath("/x").GetAttribute("radius")


def test_the_edits_come_back_after_a_reload(tmp_path):
    path = str(tmp_path / "scene.usda")
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a"
        _widget(editor, "weight").value = 5.0
        scene.save(path)

    stage = Usd.Stage.Open(path)
    if not stage.GetPrimAtPath("/a"):
        _spheres(stage, "/a")  # the save holds the chunks only
    with Scene(stage) as scene:
        assert _get(stage, "/a", "weight") == pytest.approx(5.0)
        editor = next(c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "property_editor")
        assert _widget(editor, "weight").value == pytest.approx(5.0)


def test_a_typed_edit_creates_the_attribute_where_it_is_missing():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a")
        UsdGeom.Xform.Define(stage, "/x")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a,/x"

        editor.get_param("edits").value = 'bool vray:env:bg:tex:use = true\nfloat3 tint = [1, 0.5, 0]\nweight = 2'

        for path in ("/a", "/x"):
            attr = stage.GetPrimAtPath(path).GetAttribute("vray:env:bg:tex:use")
            assert attr.GetTypeName() == Sdf.ValueTypeNames.Bool
            assert attr.Get() is True
            assert tuple(_get(stage, path, "tint")) == pytest.approx((1.0, 0.5, 0.0))
        assert _get(stage, "/a", "weight") == pytest.approx(2.0)
        assert not stage.GetPrimAtPath("/x").GetAttribute("weight"), "an untyped edit still skips"


def test_a_widget_edit_keeps_the_types_in_the_list():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a"
        edits = editor.get_param("edits")
        edits.value = "double exposure = 1.5"

        _widget(editor, "weight").value = 5.0

        assert edits.value.splitlines() == ["double exposure = 1.5", "weight = 5.0"]
        assert _get(stage, "/a", "exposure") == pytest.approx(1.5)


def test_an_unknown_type_writes_nothing():
    with Scene() as scene:
        stage = scene.get_stage()
        _spheres(stage, "/a")
        editor = scene.create.property_editor()
        editor.get_param("prim").value = "/a"

        editor.get_param("edits").value = "flaot tint = 1"

        assert not stage.GetPrimAtPath("/a").GetAttribute("tint")


# ---------------------------------------------------------------------------
# Lofting a widget to a group
# ---------------------------------------------------------------------------


def _lofted(group) -> list:
    return [p for p in group.get_my_params() if getattr(p, "_lofted", False)]


def _promoted(editor) -> list:
    return list(editor.get_param("promoted_grp").get_my_params())


def _editor_in_group(scene):
    """A Property Editor on /a's weight, inside a group."""
    _spheres(scene.get_stage(), "/a")
    group = scene.create.chunkgroup(name="grp")
    with group:
        editor = scene.create.property_editor(prim="/a", name="editor")
    return group, editor


def test_a_lofted_widget_is_published_on_the_group():
    with Scene() as scene:
        group, editor = _editor_in_group(scene)

        chunk_stage.loft_param_to_context(_widget(editor, "weight"), group)
        [lofted] = _lofted(group)
        lofted.value = 7.0

        assert _get(scene.get_stage(), "/a", "weight") == pytest.approx(7.0)
        assert [p.name for p in _promoted(editor)] == ["weight"]
        with pytest.raises(LookupError):
            _widget(editor, "weight")  # shown under Lofted instead


def test_a_lofted_widget_survives_a_reopen(tmp_path):
    path = str(tmp_path / "lofted.usda")
    with Scene() as scene:
        group, editor = _editor_in_group(scene)
        chunk_stage.loft_param_to_context(_widget(editor, "weight"), group)
        _lofted(group)[0].value = 7.0
        scene.save(path)

    with Scene.open(path) as scene:
        group = next(c for c in scene.get_contexts() if c.name == "grp")
        editor = next(c for c in scene.get_chunks() if c.name == "editor")
        [lofted] = _lofted(group)
        assert lofted.value == pytest.approx(7.0)
        _spheres(scene.get_stage(), "/a")  # the file holds the network, not the sphere

        lofted.value = 9.0

        assert _get(scene.get_stage(), "/a", "weight") == pytest.approx(9.0)
        assert [p.name for p in _promoted(editor)] == ["weight"]


def test_unlofting_turns_it_back_into_a_widget():
    with Scene() as scene:
        group, editor = _editor_in_group(scene)
        chunk_stage.loft_param_to_context(_widget(editor, "weight"), group)
        _lofted(group)[0].value = 7.0

        chunk_stage.unloft_param(_lofted(group)[0])

        assert _promoted(editor) == []
        assert _widget(editor, "weight").value == pytest.approx(7.0)
        assert _get(scene.get_stage(), "/a", "weight") == pytest.approx(7.0), "the edit stays"
