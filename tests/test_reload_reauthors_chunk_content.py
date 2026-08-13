"""Regression tests for the "chunks that only authored via add_callback never got a signal
to re-author on reload" class of bug (see docs/ discussion; found via an audit of every
chunk script that has no reserved top-level execute(node) hook).

load_chunks only ever calls a chunk's reserved execute(node) hook after bulk-restoring its
param values -- restoring values themselves deliberately does NOT re-trigger add_callback
handlers (chunk_notice._loading_stage), so a chunk whose real authoring work lived only in
an add_callback handler came back on reload with correct-looking param values but none of
its actual authored content. Each chunk below got a matching reserved execute(node) hook,
mirroring the fix scripts/sublayer.py already had.

Shared pattern: build the chunk live, confirm it authors correctly; export just the chunk
graph (export_chunks -- never the target content); reload into a fresh stage with whatever
non-chunk content the chunk targets recreated (mirroring an existing asset already present
in a real scene); confirm the chunk re-authors the same content, not nothing.
"""

from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import export_chunks, initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _make_scene(stage: Usd.Stage) -> SceneSender:
    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return scene


def _make(scene, module_name: str):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module_name)
    chunks = mod.create() if mod.loaded else mod.load()
    primary = chunks[0]
    scene.add_chunk(primary)
    return primary


def _reload(scene, tmp_path, name: str, content_setup=None):
    """Export just the chunk graph, then load it into a fresh stage. content_setup, if
    given, recreates whatever non-chunk content the chunk needs to target -- export_chunks
    never captures that, mirroring an existing asset already present in a real scene."""
    path = str(tmp_path / f"{name}.usda")
    export_chunks(scene, path)

    stage2 = Usd.Stage.Open(path)
    if content_setup is not None:
        content_setup(stage2)
    scene2 = _make_scene(stage2)
    return scene2, stage2


def test_offset_transform_reapplies_on_reload(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    UsdGeom.Xform.Define(stage, "/box")

    c = _make(scene, "offset_transform")
    c.get_param("prim_path").value = "/box"
    c.get_param("tx").value = 5.0

    ops = UsdGeom.Xformable(stage.GetPrimAtPath("/box")).GetOrderedXformOps()
    assert list(ops[0].Get(0)) == [5.0, 0.0, 0.0]  # sanity: live authoring works

    scene2, stage2 = _reload(scene, tmp_path, "offset_transform", lambda s: UsdGeom.Xform.Define(s, "/box"))
    ops2 = UsdGeom.Xformable(stage2.GetPrimAtPath("/box")).GetOrderedXformOps()
    assert ops2, "offset must be reapplied on reload, not left with no xform ops at all"
    assert list(ops2[0].Get(0)) == [5.0, 0.0, 0.0]


def test_reset_transform_reapplies_on_reload(tmp_path) -> None:
    def _content(s):
        xf = UsdGeom.Xform.Define(s, "/box")
        xf.AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    _content(stage)

    c = _make(scene, "reset_transform")
    c.get_param("prim_path").value = "/box"
    c.get_param("run").execute()

    assert not UsdGeom.Xformable(stage.GetPrimAtPath("/box")).GetOrderedXformOps()  # sanity

    scene2, stage2 = _reload(scene, tmp_path, "reset_transform", _content)
    assert not UsdGeom.Xformable(stage2.GetPrimAtPath("/box")).GetOrderedXformOps(), (
        "the reset must be reapplied on reload -- the freshly recreated content prim's own "
        "translate op must be cleared again, not left standing"
    )


def test_set_display_color_reapplies_on_reload(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    UsdGeom.Sphere.Define(stage, "/geo")

    c = _make(scene, "set_display_color")
    c.get_param("prim_path").value = "/geo"
    c.get_param("color_r").value = 1.0
    c.get_param("color_g").value = 0.0
    c.get_param("color_b").value = 0.0

    color = UsdGeom.Gprim(stage.GetPrimAtPath("/geo")).GetDisplayColorAttr().Get()
    assert tuple(color[0]) == (1.0, 0.0, 0.0)  # sanity

    scene2, stage2 = _reload(scene, tmp_path, "set_display_color", lambda s: UsdGeom.Sphere.Define(s, "/geo"))
    color2 = UsdGeom.Gprim(stage2.GetPrimAtPath("/geo")).GetDisplayColorAttr().Get()
    assert color2 is not None, "displayColor must be reauthored on reload, not left unset"
    assert tuple(color2[0]) == (1.0, 0.0, 0.0)


def _define_texture_shader(stage) -> str:
    """A minimal UsdUVTexture shader with an 'st' input, fed by a plain UV reader -- the
    shape set_uv_transform.py expects to splice a UsdTransform2d into."""
    mat_path = "/mat"
    stage.DefinePrim(mat_path, "Scope")
    reader_path = f"{mat_path}/reader"
    reader = UsdShade.Shader.Define(stage, reader_path)
    reader.CreateIdAttr("UsdPrimvarReader_float2")
    reader_out = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    tex_path = f"{mat_path}/texture"
    texture = UsdShade.Shader.Define(stage, tex_path)
    texture.CreateIdAttr("UsdUVTexture")
    st_in = texture.CreateInput("st", Sdf.ValueTypeNames.Float2)
    st_in.ConnectToSource(reader_out)
    return tex_path


def test_set_uv_transform_reapplies_on_reload(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    tex_path = _define_texture_shader(stage)

    c = _make(scene, "set_uv_transform")
    c.get_param("shader_path").value = tex_path
    c.get_param("scale_s").value = 2.0

    xform_path = f"{tex_path.rsplit('/', 1)[0]}/UVTransform"
    assert stage.GetPrimAtPath(xform_path).IsValid()  # sanity

    scene2, stage2 = _reload(scene, tmp_path, "set_uv_transform", _define_texture_shader)
    assert stage2.GetPrimAtPath(xform_path).IsValid(), (
        "the UsdTransform2d node must be reinserted on reload, not left out of the shading "
        "graph"
    )
    scale_attr = stage2.GetPrimAtPath(xform_path).GetAttribute("inputs:scale")
    assert tuple(scale_attr.Get()) == (2.0, 1.0)


def test_inline_usd_reapplies_on_reload(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)

    c = _make(scene, "inline_usd")
    c.get_param("code").value = "#usda 1.0\n\ndef Sphere 'ball' {\n\tdouble radius = 3\n}"

    assert stage.GetPrimAtPath("/ball").IsValid()  # sanity: live authoring works

    scene2, stage2 = _reload(scene, tmp_path, "inline_usd")
    assert stage2.GetPrimAtPath("/ball").IsValid(), (
        "the inline text must be re-imported into the chunk's own layer on reload -- "
        "export_chunks never captures a chunk's own layer content, only its bookkeeping prim"
    )


def test_property_editor_rebuilds_widgets_on_reload(tmp_path) -> None:
    def _content(s):
        UsdGeom.Sphere.Define(s, "/geo").CreateRadiusAttr(1.0)

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    _content(stage)

    c = _make(scene, "property_editor")
    c.get_param("prim").value = "/geo"

    dyn_grp = c.get_param("dyn_grp")
    assert dyn_grp.get_my_params(), "sanity: live selection builds at least one dynamic group"

    scene2, stage2 = _reload(scene, tmp_path, "property_editor", _content)
    c2 = next(ch for ch in scene2.get_chunks() if (ch.get_config() and ch.get_config().module) == "property_editor")
    dyn_grp2 = c2.get_param("dyn_grp")
    assert dyn_grp2.get_my_params(), (
        "the per-attribute widget group must be rebuilt for the saved prim selection on "
        "reload, not left empty until the selection is touched again"
    )


def test_add_mesh_reapplies_correct_type_on_reload(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)

    c = _make(scene, "modelling.add_mesh")
    c.get_param("prim_path").value = "/mycube"
    c.get_param("mesh_type").value = "cube"
    c.get_param("dimx").value = 3  # touching a cube-group param is what actually triggers the build

    prim = stage.GetPrimAtPath("/mycube")
    assert prim.IsValid() and UsdGeom.Mesh(prim).GetPointsAttr().Get()  # sanity

    scene2, stage2 = _reload(scene, tmp_path, "add_mesh")
    prim2 = stage2.GetPrimAtPath("/mycube")
    assert prim2.IsValid(), "the mesh must be rebuilt on reload, not left missing"
    pts2 = UsdGeom.Mesh(prim2).GetPointsAttr().Get()
    assert pts2 and len(pts2) == len(UsdGeom.Mesh(prim).GetPointsAttr().Get()), (
        "reload must rebuild the SAVED mesh_type (cube), not some other/no shape"
    )


def test_apply_schema_reapplies_on_reload(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    UsdGeom.Xform.Define(stage, "/geo")

    c = _make(scene, "apply_schema")
    c.get_param("prim").value = "/geo"
    c.get_param("schemas").value = "UsdGeomModelAPI"
    c.get_param("apply").execute()

    assert stage.GetPrimAtPath("/geo").HasAPI("GeomModelAPI") or "GeomModelAPI" in [
        s.split(":")[0] for s in stage.GetPrimAtPath("/geo").GetAppliedSchemas()
    ]  # sanity

    scene2, stage2 = _reload(scene, tmp_path, "apply_schema", lambda s: UsdGeom.Xform.Define(s, "/geo"))
    applied2 = stage2.GetPrimAtPath("/geo").GetAppliedSchemas()
    assert any("GeomModelAPI" in s for s in applied2), (
        f"the saved schema must be reapplied on reload, not left off the freshly recreated "
        f"target prim (got {list(applied2)})"
    )


def test_uv_checker_reapplies_on_reload(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    UsdGeom.Sphere.Define(stage, "/geo")

    c = _make(scene, "uvchecker.uv_checker")
    c.get_param("prim").value = "/geo"
    c.get_param("apply").execute()

    binding = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/geo")).ComputeBoundMaterial()[0]
    assert binding and binding.GetPath() == Sdf.Path("/materials/UVCheckerMaterial")  # sanity

    scene2, stage2 = _reload(scene, tmp_path, "uv_checker", lambda s: UsdGeom.Sphere.Define(s, "/geo"))
    binding2 = UsdShade.MaterialBindingAPI(stage2.GetPrimAtPath("/geo")).ComputeBoundMaterial()[0]
    assert binding2 and binding2.GetPath() == Sdf.Path("/materials/UVCheckerMaterial"), (
        "the UV checker material must be rebound on reload, not left off the freshly "
        "recreated target prim"
    )
