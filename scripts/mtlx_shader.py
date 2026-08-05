from pxr import Sdf, Tf, UsdShade

from usd_chunks import chunk, chunk_registry, chunk_utils, shader_defs
from usd_chunks.chunk_flags import ChunkFlags

SOURCE_TYPE = "mtlx"

# Scope new shader networks are authored under when the path is left to resolve itself.
_DEFAULT_SCOPE = "/materials"

# Params the chunk always owns. Everything else on the chunk was lofted from the current
# shader definition and gets torn down when the definition changes.
_FIXED = ("nodedef", "prim_path", "bind_to")

# Child prim name a terminal's shader takes under its Material.
_SHADER_LEAF = "shader"

# UsdShade render context for MaterialX terminals: a material's mtlx surface output is
# outputs:mtlx:surface, not the universal outputs:surface, so a renderer picks the
# MaterialX network only when it asks for it.
_RENDER_CONTEXT = "mtlx"


def _shader_def(node):
    """The ShaderDef for this chunk's current nodedef, or None if unset/unknown."""
    identifier = (node.get_param("nodedef").value or "").strip()
    if not identifier:
        return None
    return shader_defs.get_shader_def(identifier, SOURCE_TYPE) or shader_defs.get_shader_def(identifier)


def _stage_for(node):
    """The stage to resolve paths against.

    node.get_stage() is empty until the chunk is attached to the scene, and a variant's
    presets land before that -- so fall back to the scene the registry already holds.
    """
    if stage := node.get_stage():
        return stage
    scene = getattr(chunk_registry.ChunkRegistry(), "scene", None)
    return scene.get_stage() if scene else None


def _node_name(sdef) -> str:
    """The chunk's display name for a shader definition -- its identifier minus the ND_."""
    return Tf.MakeValidIdentifier(sdef.identifier.removeprefix("ND_") or sdef.name)


def _ensure_prim_path(node, sdef) -> str:
    """Resolve an unset Path, leaving a user-chosen one alone.

    Only fills a blank, so a path saved into USD comes back populated on load and is not
    re-derived. Named from the Sdr family ("standard_surface") rather than the identifier,
    which is the same thing wearing an ND_ prefix and a _surfaceshader suffix.
    """
    param = node.get_param("prim_path")
    if param is None:
        return ""

    if current := (param.value or "").strip():
        return current

    if not (stage := _stage_for(node)):
        return ""

    base = Tf.MakeValidIdentifier(sdef.family or sdef.name or "shader")
    param.value = chunk_utils.get_unique_path(stage, f"{_DEFAULT_SCOPE}/{base}")
    return param.value


def _clear_lofted(owner):
    """Remove every param lofted from the previous shader definition.

    get_my_params, not get_params: the latter walks the nested ChainMap and would reach
    into params this chunk does not own.
    """
    for p in owner.get_my_params():
        if p.name not in _FIXED:
            owner.remove_param_or_group(p)


def _loft_params(owner, sdef):
    """Create params for a shader definition's inputs, grouped by their Sdr page.

    Inputs with no page land directly on the chunk; the rest go into a collapsed Group
    per page, which is what keeps a 42-input surface shader readable.
    """
    groups: dict = {}

    for prop in sdef.inputs:
        param = shader_defs.param_for_prop(prop)
        if param is None:
            continue

        if prop.help:
            param.description = prop.help

        if not prop.page:
            owner.add_param(param)
            continue

        group = groups.get(prop.page)
        if group is None:
            group = chunk.Group(
                prop.page,
                prop.page,
                flags=ChunkFlags.GroupFlags.COLLAPSABLE | ChunkFlags.GroupFlags.LABELED,
            )
            # Collapsed by default: a surface shader carries 40-odd inputs across nine
            # pages, and every one of them expanded makes a node taller than the graph.
            group.set_state(ChunkFlags.State.COLLAPSED)
            groups[prop.page] = group
            owner.add_param(group)
        group.add_param(param)


def _rebuild(owner):
    """Retarget the chunk at whatever nodedef is currently set."""
    _clear_lofted(owner)

    sdef = _shader_def(owner)
    if sdef is None:
        return

    _loft_params(owner, sdef)
    _ensure_prim_path(owner, sdef)

    # Name the node after the shader it authors, so a graph of these reads as the network
    # it builds rather than as a column of identical "MaterialX Shader" boxes.
    owner.name = _node_name(sdef)

    # Binding only means anything for a terminal (a surface/displacement/volume); an
    # intermediate node like ND_multiply_float is wired to other shaders, never bound.
    bind = owner.get_param("bind_to")
    if bind is not None:
        if sdef.is_terminal:
            bind.unset_state(ChunkFlags.State.HIDDEN)
        else:
            bind.set_state(ChunkFlags.State.HIDDEN)


def _nodedef_changed(owner, _notice, _sender):
    _rebuild(owner)


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    sdef = _shader_def(node)
    if sdef is None:
        return

    _ensure_prim_path(node, sdef)

    # Pass the param, not its value: get_unique_path clears this chunk's own procedural
    # layer first, so re-cooking finds its previous prim gone and keeps the same path
    # instead of walking the suffix up on every execute.
    prim_path = chunk_utils.get_unique_path(stage, node.get_param("prim_path"))
    if not prim_path or not Sdf.Path.IsValidPathString(prim_path):
        return

    # A terminal needs a Material to be bindable, and a Material cannot itself carry the
    # shader, so the shader becomes its child. Everything else IS the shader -- an
    # intermediate node is wired to other shaders, never bound, so wrapping it in a
    # Material would author a prim nothing ever looks at.
    shader_path = f"{prim_path}/{_SHADER_LEAF}" if sdef.is_terminal else prim_path

    shader = UsdShade.Shader.Define(stage, shader_path)
    shader.CreateIdAttr(sdef.identifier)

    _prim_path = getattr(node, "_exec_prim_path", None) or getattr(node, "prim_path", None)
    chunk_prim = stage.GetPrimAtPath(_prim_path) if _prim_path else None

    for prop in sdef.inputs:
        param = node.get_param(prop.name)
        if param is None:
            continue

        sdf_type = Sdf.ValueTypeNames.Find(prop.sdf_type)
        if not sdf_type:
            continue

        inp = shader.CreateInput(prop.name, sdf_type)
        try:
            inp.Set(param.value)
        except Exception:  # noqa: BLE001 - param value this input type cannot hold
            pass

        param.connect_usd_attr(inp.GetAttr())
        if prop.connectable:
            chunk_utils.wire_shader_input(stage, chunk_prim, prop.name, inp.GetAttr())

    for prop in sdef.outputs:
        if sdf_type := Sdf.ValueTypeNames.Find(prop.sdf_type):
            shader.CreateOutput(prop.name, sdf_type)

    if not sdef.is_terminal:
        return

    material = UsdShade.Material.Define(stage, prim_path)
    out_name = sdef.outputs[0].name if sdef.outputs else "out"
    material.CreateSurfaceOutput(_RENDER_CONTEXT).ConnectToSource(shader.ConnectableAPI(), out_name)

    bind_param = node.get_param("bind_to")
    if bind_param and bind_param.value:
        for target_path in chunk_utils.iter_prim_targets(stage, bind_param):
            prim = stage.GetPrimAtPath(target_path)
            if prim and prim.IsValid():
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)


def main():
    chunk_api = chunk.Chunk("MaterialX Shader")

    nodedef = chunk.LineEdit(
        "nodedef",
        "Node Definition",
        flags=ChunkFlags.ParamFlags.LABELED,
    )
    prim_path = chunk.LineEdit(
        "prim_path",
        "Path",
        flags=ChunkFlags.ParamFlags.LABELED,
    )
    bind_to = chunk.LineEdit(
        "bind_to",
        "Bind To",
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    bind_to.set_state(ChunkFlags.State.HIDDEN)

    chunk_api.add_params([nodedef, prim_path, bind_to])
    chunk_api.add_callback(_nodedef_changed, sender=nodedef)

    return chunk_api


def config():
    # One menu entry per MaterialX shader, nested under its Sdr family so ~800 entries
    # stay navigable in the tree while remaining flat in search. Empty in a runtime with
    # no MaterialX (pip usd-core), which is the correct answer there rather than an error.
    variants = [
        (f"{d.family or 'other'}/{d.name}", {"nodedef": d.identifier})
        for d in shader_defs.iter_shader_defs(SOURCE_TYPE)
    ]

    return chunk.DefaulConfig(
        category="MaterialX",
        variants=variants,
        description="""
        Authors a MaterialX shader as a UsdShade.Shader prim, with its params generated
        from the shader's own definition in USD's shader registry (Sdr).

        Node Definition is the Sdr identifier, e.g. ND_standard_surface_surfaceshader.
        Picking a shader from the chunk menu presets it; changing it rebuilds the params
        to match the new definition. Inputs are grouped into collapsible sections
        following the definition's own pages (Base, Specular, Coat, ...).

        Path is where the network is authored. Left blank it resolves itself to
        /materials/<shader name>, suffixed to the first free name -- so a second Standard
        Surface lands on standard_surface1 rather than on top of the first. Setting it
        explicitly disables that; it is never renumbered once it holds a value.

        For a terminal shader (one whose context is surface, displacement or volume) Path
        is a UsdShade.Material, the shader is authored as its "shader" child and connected
        through outputs:mtlx:surface, and Bind To binds the material to the prims you name.
        Every other shader IS Path -- an intermediate node is wired to other shaders in the
        node graph, never bound, so it gets no Material wrapper.
        """,
    )
