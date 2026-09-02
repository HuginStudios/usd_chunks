"""A chunkgroup's inner chunks must cook AFTER the content they act on, on scene load.

Only exec roots are cooked directly by load_chunks; everything with a dependency cooks by
cascade from one. But a chunkgroup's inner chunks are roots of their OWN subgraph, and
nothing connects them to the group's upstream -- only the GROUP is wired into the outer
chain -- so the DAG imposes no order between them and the outer chunks whose content they
act on.

Live-reported: reopening a saved scene ran an AMD MaterialX Library's inner BindMaterial
before the Sublayer feeding the group had brought the mesh onto the stage. It found no
target, authored nothing, and never ran again -- so the file opened with the original
material still bound, and only diving into the group and back out (which forces a proper
dependency-ordered recook) brought the applied material back.
"""
from __future__ import annotations

import weakref

from pxr import Sdf, Usd, UsdShade

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import (
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    get_chunk_prim_path,
    _CHUNKS_ROOT,
    initialize as _chunk_usd_init,
    recook_focus,
    save_scene,
)


class _RootContext:
    """Stand-in for the tab context the app focuses. Passing None instead means "no
    context", which makes recook treat a group's inner chunks as nodes of the ROOT graph
    and mute them -- see is_node_in_context."""

    # The container the chunks in this test actually sit in. It used to stand in for the
    # /chunks scope, which was where a context-less chunk landed; they now land inside the
    # scene's default chunkgroup, and a context naming the scope instead matches nothing --
    # is_node_in_context compares prim parents, so every chunk reads as "not in this
    # context" and the cook quietly does nothing.
    def __init__(self, scene=None):
        ctx = scene.peek_root_context() if scene is not None else None
        self.prim_path = get_chunk_prim_path(ctx) if ctx is not None else _CHUNKS_ROOT

    name = "Tab1"

    class _Cfg:
        module = "chunkgroup"

    def has_flag(self, _f):
        return True

    def get_config(self):
        return _RootContext._Cfg()

    def get_context(self):
        return None


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _binding(stage, path="/apple"):
    prim = stage.GetPrimAtPath(path)
    if not (prim and prim.IsDefined()):
        return "NO PRIM"
    return str(UsdShade.MaterialBindingAPI(prim).GetDirectBinding().GetMaterialPath()) or "<unbound>"


def test_group_inner_chunks_cook_after_their_upstream_on_load(qapp, tmp_path) -> None:
    # A scene file the Sublayer chunk pulls in: a mesh already bound to its own material.
    bowl_path = tmp_path / "bowl.usda"
    bowl = Usd.Stage.CreateNew(str(bowl_path))
    apple = bowl.DefinePrim("/apple", "Xform")
    UsdShade.MaterialBindingAPI.Apply(apple).Bind(UsdShade.Material.Define(bowl, "/OrigMat"))
    bowl.GetRootLayer().Save()

    # A library file the Reference chunk pulls the replacement material out of.
    matlib_path = tmp_path / "matlib.usda"
    matlib = Usd.Stage.CreateNew(str(matlib_path))
    UsdShade.Material.Define(matlib, "/MyMat")
    matlib.GetRootLayer().Save()

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    def _mk(key):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    def _wire(dst, src):
        p = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
        a = p.GetAttribute(_EXEC_INPUT) or p.CreateAttribute(
            _EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
        )
        a.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])

    sub = _mk("sublayer")
    lg = sub.get_param("imports")
    lg.size = 1
    lg.get_indexed_param("sublayer_path", 0).value = str(bowl_path).replace("\\", "/")

    grp = _mk("chunkgroup")
    _wire(grp, sub)

    scene._current_context = weakref.ref(grp)
    ref = _mk("reference")
    ref.get_param("file_path").value = str(matlib_path).replace("\\", "/")
    ref.get_param("import_primpath").value = "/MyMat"
    ref.get_param("prim_type").value = "Material"
    ref.get_param("primpath_location").value = "/NewMat"
    bind = _mk("bind_material")
    bind.get_param("material_path").value = "/NewMat"
    bind.get_param("primpath_location").value = "/apple"
    _wire(bind, ref)
    inner_view = _mk("view")
    _wire(inner_view, bind)
    scene._current_context = None

    outer_view = _mk("view")
    _wire(outer_view, grp)

    # Cook once so the live scene is in the state the user saves from.
    recook_focus(stage, scene, _RootContext(scene), outer_view)
    assert _binding(stage) == "/NewMat", "test setup must be faithful -- it must bind live first"

    out = tmp_path / "scene.usda"
    save_scene(scene, out)

    # --- reopen, exactly as the app does on File > Open
    stage2 = Usd.Stage.Open(str(out))

    class _StageScene2(_DummyScene):
        def get_stage(self):
            return stage2

    scene2 = _StageScene2()
    chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage2)
    _chunk_usd_init(stage2, scene2)
    qapp.processEvents()

    assert _binding(stage2) == "/NewMat", (
        "the group's inner BindMaterial must cook after the Sublayer that brings its target "
        "prim onto the stage -- otherwise the reopened scene keeps the original material"
    )
