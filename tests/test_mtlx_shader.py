"""Tests for the MaterialX shader chunk and the Sdr-backed shader_defs provider.

Skipped wholesale where the running USD has no MaterialX: pip usd-core ships no usdMtlx,
so Sdr reports only ['USD', 'glslfx'] there and every shader these assert on is absent.
They do run against a USD built with usdMtlx (the usdbundle usdview environment).
"""

import pytest
from pxr import Sdf, Usd, UsdShade

from usd_chunks import chunk_registry, shader_defs
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins.chunk_usd import ChunkStageSync, export_chunks
from usd_chunks.plugins.usdchunks_cli import _all_chunks

pytestmark = pytest.mark.skipif(
    "mtlx" not in shader_defs.available_source_types(),
    reason="this USD has no MaterialX shader definitions (no usdMtlx plugin)",
)

SURFACE = "pbr/standard_surface"
TEXTURE = "texture/tiledimage"
ADD = "math/add"


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def env():
    """A stage with the registry and stage-sync live, plus a menu-entry lookup.

    Entries are keyed by their namespace below the MaterialX category, so a test asks for
    "pbr/standard_surface" rather than reconstructing an ND_ identifier.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)

    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    ChunkStageSync().initialize(stage, scene)

    entries = {
        "/".join(e.namespace[1:]): e
        for e in reg.get_menu_entries()
        if e.module.module_name == "mtlx_shader" and len(e.namespace) > 2
    }
    module = reg.get_chunk_module("mtlx_shader").module
    return stage, scene, entries, module


def _create(env_, key):
    stage, scene, entries, _ = env_
    chunk = entries[key].create()[0]
    scene.add_chunk(chunk)
    return chunk


def _chunk_prim(stage, chunk):
    return ChunkStageSync().prim_of(chunk)


def _cook(stage, chunk, module):
    """Execute a chunk into a layer of its own, as the scheduler does.

    execute() clears the current edit target on the assumption it owns that layer, so
    cooking two chunks against one target would have each wipe the other's work.
    """
    layer = Sdf.Layer.CreateAnonymous(chunk.name)
    stage.GetRootLayer().subLayerPaths.append(layer.identifier)
    with Usd.EditContext(stage, layer):
        module.execute(chunk)


def _ports(prim, prefix):
    return {
        a.GetName()[len(prefix):]: str(a.GetTypeName())
        for a in prim.GetAttributes()
        if a.GetName().startswith(prefix) and a.GetName() != f"{prefix}chunk"
    }


def test_menu_collapses_type_variants(env):
    """One entry per family, not per nodedef -- 784 definitions are ~217 operations."""
    _, _, entries, _ = env

    assert len(entries) < 400, "type variants are not being collapsed"
    assert SURFACE in entries
    assert ADD in entries

    roles = {key.split("/")[0] for key in entries}
    assert "math" in roles and "pbr" in roles


def test_signature_lists_every_sibling_definition(env):
    """Signatures key off the identifier, not the Sdr category.

    ND_add_color3 and ND_add_color3FA share category color3, so keying on category drops
    one of every such pair.
    """
    _, _, _, module = env

    variants = module._variants_for("add")
    families = [d for d in shader_defs.iter_shader_defs("mtlx") if d.family == "add"]

    assert len(variants) == len(families)
    assert "color3" in variants and "color3FA" in variants


def test_created_shader_lofts_params_and_ports(env):
    stage, _, _, _ = env
    surf = _create(env, SURFACE)
    prim = _chunk_prim(stage, surf)

    inputs = _ports(prim, "inputs:")
    assert "Base:base_color" in inputs, "page groups namespace the port name"
    assert inputs["Base:base_color"] == "float3"

    # Groups start collapsed: 40-odd inputs across nine pages is taller than the graph.
    groups = [p for p in surf.get_my_params() if hasattr(p, "add_param")]
    assert groups and all(g.has_state(ChunkFlags.State.COLLAPSED) for g in groups)


def test_outputs_are_bare_attrs_typed_to_match_their_input_port(env):
    """Outputs get no param: a hidden one is still yielded by _iter_connectable_params,
    which makes _place_prim_output_ports skip it while _place_param_ports has no visible
    widget to position it against -- so the port vanishes entirely.

    A colour output is authored Float3 rather than Color3f so that its port type matches
    the vector port of the Vector param it feeds; Color3f would give a colour port that
    cannot connect to the very input it exists for.
    """
    stage, _, _, _ = env
    tex = _create(env, TEXTURE)
    prim = _chunk_prim(stage, tex)

    assert _ports(prim, "outputs:") == {"out": "float3"}
    assert tex.get_param("out") is None, "an output must not create a param"


def test_signature_switch_retypes_the_output_port(env):
    stage, _, _, _ = env
    node = _create(env, ADD)

    assert _ports(_chunk_prim(stage, node), "outputs:")["out"] == "float3"

    node.get_param("signature").value = "float"

    assert node.get_param("nodedef").value == "ND_add_float"
    # Re-read the prim: switching signature renames the chunk, so the prim moves from
    # /chunks/add_color3 to /chunks/add_float and the old handle expires.
    assert _ports(_chunk_prim(stage, node), "outputs:")["out"] == "float"


def test_duplicate_shaders_get_unique_names_and_paths(env):
    """A second shader of the same type used to collide, failing the chunk prim rename
    and leaving both chunks pointing at one prim."""
    stage, _, _, _ = env
    nodes = [_create(env, ADD) for _ in range(3)]

    names = [n.name for n in nodes]
    paths = [ChunkStageSync().prim_path_of(n) for n in nodes]

    assert len(set(names)) == 3, names
    assert len(set(paths)) == 3, paths


@pytest.mark.xfail(
    reason="two nodes created before either cooks claim the same path: get_unique_path "
    "counts only typed prims on the stage, and nothing is authored until a cook",
    strict=False,
)
def test_prim_path_resolves_per_node(env):
    stage, _, _, _ = env
    a = _create(env, SURFACE)
    b = _create(env, SURFACE)

    assert a.get_param("prim_path").value != b.get_param("prim_path").value
    assert a.get_param("prim_path").value.startswith("/materials/standard_surface")


def test_terminal_authors_material_and_intermediate_does_not(env):
    stage, _, _, module = env

    surf = _create(env, SURFACE)
    _cook(stage, surf, module)
    surf_path = surf.get_param("prim_path").value
    assert stage.GetPrimAtPath(surf_path).GetTypeName() == "Material"
    assert stage.GetPrimAtPath(f"{surf_path}/shader").GetTypeName() == "Shader"

    material = UsdShade.Material(stage.GetPrimAtPath(surf_path))
    assert "outputs:mtlx:surface" in [o.GetFullName() for o in material.GetOutputs()]

    node = _create(env, ADD)
    _cook(stage, node, module)
    assert stage.GetPrimAtPath(node.get_param("prim_path").value).GetTypeName() == "Shader"


@pytest.mark.xfail(
    reason="the upstream chunk's outputs:out is gone by the time the downstream cook "
    "reads it, so the second hop of the chain cannot be authored -- the wire between two "
    "shader nodes does not reach the real shaders",
    strict=False,
)
def test_texture_wires_into_surface_base_color(env):
    """The whole chain: a graph wire between two chunk prims has to end up as a USD
    connection between the two real shaders.

    wire_shader_input walks it in two hops -- the downstream chunk's inputs: connection
    names the upstream chunk's prim, and that prim's outputs: attr carries the connection
    to the real shader. Both hops have to be authored or the wire resolves to nothing.
    """
    stage, _, _, module = env

    surf = _create(env, SURFACE)
    tex = _create(env, TEXTURE)

    surf_prim_path = ChunkStageSync().prim_path_of(surf)
    tex_prim_path = ChunkStageSync().prim_path_of(tex)

    # Wire as the node graph does. Two details matter and both come from reading what the
    # graph actually authors: the connection lands on the BARE param name (its ports are
    # built from param.name, ignoring the group that namespaces the value-storage attr),
    # and the source is named in its .inputs: form (_author_connection rewrites .outputs:
    # to .inputs: for typed params). The attr is created here because the graph creates it
    # on demand too when a port has never been connected.
    surf_prim = stage.GetPrimAtPath(surf_prim_path)
    attr = surf_prim.GetAttribute("inputs:base_color")
    if not (attr and attr.IsValid()):
        attr = surf_prim.CreateAttribute("inputs:base_color", Sdf.ValueTypeNames.Float3, custom=False)
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        attr.SetConnections([Sdf.Path(f"{tex_prim_path}.inputs:out")])

    _cook(stage, tex, module)
    _cook(stage, surf, module)

    # Hop one: the texture chunk's output port points at its real shader output.
    tex_out = stage.GetAttributeAtPath(Sdf.Path(f"{tex_prim_path}.outputs:out"))
    assert tex_out and tex_out.IsValid(), "texture chunk has no outputs:out"
    tex_shader_out = tex_out.GetConnections()
    assert tex_shader_out, "outputs:out is not connected to the real shader output"

    # Hop two: the surface's shader input resolves through to that same shader output.
    shader = UsdShade.Shader(
        stage.GetPrimAtPath(f"{surf.get_param('prim_path').value}/shader")
    )
    base_color = shader.GetInput("base_color")
    assert base_color, "surface shader has no base_color input"
    assert base_color.GetAttr().GetConnections() == tex_shader_out


def test_saved_shader_values_survive_a_reload(env, tmp_path):
    """A saved network must come back with its authored inputs, not the nodedef defaults.

    This chunk does not own its shader inputs -- it lofts them from the nodedef, in the
    callback that fires when nodedef *changes*. A load restores nodedef as a value rather
    than as a change, so nothing lofted them, and execute() (which skips any input it has
    no param for) then cooked a bare shader at its definition defaults. Every authored
    value was silently gone, in a file that still looked intact on disk.

    The round trip is the point: the values were always present on the chunk prim as
    inputs:<Page>:<name>, so anything short of reopening the file passes regardless.
    """
    stage, scene, _, _ = env
    surf = _create(env, SURFACE)
    surf.get_param("base_color").value = (0.42, 0.016, 0.02)
    surf.get_param("metalness").value = 0.55
    surf.get_param("coat").value = 1.0

    path = str(tmp_path / "net.usda")
    export_chunks(scene, path)

    chunk_registry.ChunkLayerManager().reset()
    reloaded_stage = Usd.Stage.Open(path)
    reloaded_scene = _Scene(reloaded_stage)
    chunk_registry.ChunkLayerManager().initialize(reloaded_stage)
    chunk_registry.ChunkRegistry().initialize(reloaded_scene, "pytest")
    ChunkStageSync().initialize(reloaded_stage, reloaded_scene)

    shaders = [
        c for c in _all_chunks(reloaded_scene.get_chunks())
        if c.get_param("nodedef") is not None
    ]
    assert shaders, "the MaterialX Shader chunk should come back on load"
    restored = shaders[0]

    assert restored.get_param("base_color") is not None, (
        "the lofted inputs must be rebuilt on load, or execute() authors none of them"
    )
    assert restored.get_param("base_color").value == pytest.approx((0.42, 0.016, 0.02))
    assert restored.get_param("metalness").value == pytest.approx(0.55)
    assert restored.get_param("coat").value == pytest.approx(1.0)


def test_reload_keeps_the_name_the_file_gave_the_chunk(env, tmp_path):
    """Rebuilding on load must not rename the chunk after its shader.

    A full _rebuild would: it ends in _rename, which names a chunk for its nodedef. On
    load that collapses every Standard Surface in a file onto one name plus uniquifying
    suffixes, breaking anything that addresses a chunk by name -- `usdchunks run
    net.usda CarPaint.base_color=...`, a lofted interface, a saved reference to it.
    """
    stage, scene, _, _ = env
    surf = _create(env, SURFACE)
    surf.name = "CarPaint"
    surf.get_param("base_color").value = (0.9, 0.1, 0.1)

    path = str(tmp_path / "named.usda")
    export_chunks(scene, path)

    chunk_registry.ChunkLayerManager().reset()
    reloaded_stage = Usd.Stage.Open(path)
    reloaded_scene = _Scene(reloaded_stage)
    chunk_registry.ChunkLayerManager().initialize(reloaded_stage)
    chunk_registry.ChunkRegistry().initialize(reloaded_scene, "pytest")
    ChunkStageSync().initialize(reloaded_stage, reloaded_scene)

    names = {c.name for c in _all_chunks(reloaded_scene.get_chunks())}
    assert "CarPaint" in names, f"the saved name must survive the load, got {names}"
