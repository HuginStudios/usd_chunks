"""The framework clears a procedural chunk's layer before every cook, and cooks only what is stale.

A chunk's execute() used to start by clearing its own layer, by hand -- 51 of 76 modules did,
and a module that forgot accumulated prims or bumped its own unique path on every cook. The
dispatch that runs a module now clears the layer itself, right before execute(), for any chunk
whose layer is its own (anonymous). A cook therefore always starts from nothing and authors its
whole result.

That made the hand-rolled cook caches (camera, light, uv_texture, urdf_import) impossible --
skipping a cook after a clear leaves an empty layer -- and the reason most of them existed went
with them: recook_for_view re-executed every chunk upstream of the view on every view switch,
wire and delete. It now only executes chunks that are stale (chunk_notice.is_cook_stale). Muting
keeps a layer's content, so a clean chunk comes back with its last result.

These run through the real dispatch and the scripting Scene, the way a session does.
"""
from __future__ import annotations

import pytest
from pxr import Sdf

from usd_chunks import Scene, chunk_notice, chunk_registry, chunk_stage
from usd_chunks.chunk_core import ChunkConfig


def _param(node, suffix):
    """A param by name anywhere in the chunk, groups included."""
    for _, p in node.iter_hierarchy():
        if p is not node and p.name == suffix:
            return p
    return None


def _count_cooks(monkeypatch, *nodes):
    """{chunk name: number of module executes} for these chunks, from here on."""
    counts = {n.name: 0 for n in nodes}
    for module in {id(n._module): n._module for n in nodes}.values():
        original = module.execute

        def traced(node, original=original):
            if node.name in counts:
                counts[node.name] += 1
            return original(node)

        monkeypatch.setattr(module, "execute", traced)
    return counts


def test_a_chunk_with_no_clear_of_its_own_does_not_pile_up():
    """Prim never clears its layer by hand. Repeated cooks leave one prim at one path."""
    with Scene() as scene:
        prim = scene.create.prim(primpath_location="/world/thing")
        stage = prim.get_stage()
        for _ in range(3):
            prim.execute()

        assert prim.get_param("primpath_location").value == "/world/thing", (
            "its own prim from the last cook must not read as a collision"
        )
        children = [p.GetName() for p in stage.GetPrimAtPath("/world").GetChildren()]
        assert children == ["thing"]


def test_a_changed_param_leaves_only_the_new_result():
    """What the last cook authored is gone before the next one starts."""
    with Scene() as scene:
        prim = scene.create.prim(primpath_location="/a")
        stage = prim.get_stage()
        prim.get_param("primpath_location").value = "/b"

        assert stage.GetPrimAtPath("/b").IsValid()
        assert not stage.GetPrimAtPath("/a").IsValid(), "the previous cook's prim must be cleared"


def test_an_interactive_chunk_is_never_cleared():
    """interactive=True: the chunk has no layer of its own and authors into the stage's edit
    target -- the user's file. Nothing there is the framework's to clear, and a cook must
    not bump its own claimed path along either, since its last result is still standing."""
    with Scene() as scene:
        module = chunk_registry.ChunkRegistry().get_chunk_module("prim")
        prim = (module.create() if module.loaded else module.load())[0]
        # Interactive from the start, before it joins the scene and could get a layer.
        prim._config = ChunkConfig(module="prim", interactive=True)
        prim.get_param("primpath_location").value = "/mine"
        stage = scene.get_stage()
        stage.DefinePrim("/users_own", "Xform")
        scene.add_chunk(prim)
        assert prim.get_edit_context() is None

        for _ in range(3):
            prim.execute()

        target = stage.GetEditTarget().GetLayer()
        assert target.GetPrimAtPath("/users_own") is not None, "the user's work must survive a cook"
        assert prim.get_param("primpath_location").value == "/mine"
        names = sorted(p.GetName() for p in stage.GetPseudoRoot().GetChildren() if p.GetName() != "chunks")
        assert names == ["mine", "users_own"]


def test_a_cook_leaves_the_chunk_clean_and_a_new_layer_makes_it_stale():
    with Scene() as scene:
        prim = scene.create.prim(primpath_location="/p")
        prim.execute()
        assert not chunk_notice.is_cook_stale(prim)

        chunk_notice.mark_cook_stale(prim)
        assert chunk_notice.is_cook_stale(prim)
        prim.execute()
        assert not chunk_notice.is_cook_stale(prim)


def test_recook_for_view_does_not_rerun_clean_chunks(monkeypatch):
    """A view switch, wire or delete recooks the view's tree. What already holds its result
    is revealed, not executed -- an import that parses a file used to redo it every time."""
    with Scene() as scene:
        a = scene.create.prim(primpath_location="/a")
        b = scene.create.prim(a, primpath_location="/b")
        view = scene.create.view(b)
        stage = a.get_stage()
        chunk_stage.recook_for_view(stage, scene, None, view)
        assert stage.GetPrimAtPath("/a").IsValid() and stage.GetPrimAtPath("/b").IsValid()

        counts = _count_cooks(monkeypatch, a, b)
        chunk_stage.recook_for_view(stage, scene, None, view)

        assert counts == {a.name: 0, b.name: 0}
        assert stage.GetPrimAtPath("/a").IsValid() and stage.GetPrimAtPath("/b").IsValid(), (
            "a clean chunk's layer is unmuted with its last result still in it"
        )


def test_recook_for_view_cooks_a_stale_chunk_and_everything_below_it(monkeypatch):
    """A stale chunk cooks, and its cook reaches the chunks downstream while they are still
    muted, which marks them stale in turn -- so the whole chain below a change recooks."""
    with Scene() as scene:
        a = scene.create.prim(primpath_location="/a")
        b = scene.create.prim(a, primpath_location="/b")
        view = scene.create.view(b)
        stage = a.get_stage()
        chunk_stage.recook_for_view(stage, scene, None, view)

        chunk_notice.mark_cook_stale(a)
        counts = _count_cooks(monkeypatch, a, b)
        chunk_stage.recook_for_view(stage, scene, None, view)

        assert counts == {a.name: 1, b.name: 1}
        assert not chunk_notice.is_cook_stale(a) and not chunk_notice.is_cook_stale(b)


def test_recook_for_view_does_not_execute_a_chunk_with_nothing_to_cook(monkeypatch):
    """A chunk with no execute() (Property Editor works through callbacks) never gets its
    stale mark cleared by a cook. Executing it on every recook would push a DAG solve to
    everything downstream of it each time."""
    with Scene() as scene:
        a = scene.create.prim(primpath_location="/a")
        editor = scene.create.property_editor(a)
        b = scene.create.prim(editor, primpath_location="/b")
        view = scene.create.view(b)
        stage = a.get_stage()
        chunk_stage.recook_for_view(stage, scene, None, view)

        solves = []
        monkeypatch.setattr(type(editor), "execute", lambda self: solves.append(self.name))
        counts = _count_cooks(monkeypatch, a, b)
        chunk_stage.recook_for_view(stage, scene, None, view)

        assert solves == [] and counts == {a.name: 0, b.name: 0}
        assert not chunk_notice.is_cook_stale(editor)


def test_a_light_keeps_its_params_across_a_cook():
    """A light builds its params from the prim it authors. Every edit is a cook, and rebuilding
    them on each one deleted the widget under a dragged slider. Same attributes, same params."""
    with Scene() as scene:
        light = scene.create.rect_light()
        stage = light.get_stage()
        path = light.get_param("primpath_location").value
        intensity = _param(light, "intensity")

        for value in (68.0, 70.0, 72.5):
            intensity.value = value
        light.execute()

        assert _param(light, "intensity") is intensity, "the same param object, not a rebuilt one"
        assert stage.GetAttributeAtPath(f"{path}.inputs:intensity").Get() == pytest.approx(72.5)


def test_a_light_rebuilds_its_params_for_a_new_type():
    with Scene() as scene:
        light = scene.create.rect_light()
        stage = light.get_stage()
        intensity = _param(light, "intensity")
        intensity.value = 68.0

        light.get_param("light_type").value = "SphereLight"

        assert _param(light, "radius") is not None, "a SphereLight's own attributes are shown"
        assert _param(light, "intensity") is not intensity
        path = light.get_param("primpath_location").value
        assert stage.GetPrimAtPath(path).GetTypeName() == "SphereLight"
        assert stage.GetAttributeAtPath(f"{path}.inputs:intensity").Get() == pytest.approx(68.0), (
            "an edited value comes back from the chunk prim"
        )


def test_a_driven_attr_survives_its_chunks_cook():
    """What a param drives (connect_usd_attr) is the param's state, not the cook's output. It
    is authored into the chunk's layer -- possibly outside a cook -- and must not be lost to the
    clear the next cook starts with."""
    with Scene() as scene:
        # Off its default first: a param only gets the storage attribute a driven connection
        # hangs from once it holds a value of its own.
        ops = scene.create.math_ops(a=5.0)
        stage = ops.get_stage()
        target = stage.DefinePrim("/geo/x", "Xform").CreateAttribute(
            "driven", Sdf.ValueTypeNames.Float, custom=True)
        ops.get_param("a").connect_usd_attr(target)
        assert target.Get() == pytest.approx(5.0), "test setup: the binding has to land first"

        ops.get_param("a").value = 10.0
        ops.execute()

        assert stage.GetAttributeAtPath("/geo/x.driven").Get() == pytest.approx(10.0)
