from pxr import Sdf, UsdShade

from usd_chunks import chunk, chunk_utils, shader_defs
from usd_chunks.chunk_flags import ChunkFlags

SOURCE_TYPE = "mtlx"

# Params the chunk always owns. Everything else on the chunk was lofted from the current
# shader definition and gets torn down when the definition changes.
_FIXED = ("nodedef", "shader_path", "bind_to")

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

    shader_path = (node.get_param("shader_path").value or "").strip()
    if not shader_path or not Sdf.Path.IsValidPathString(shader_path):
        return

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

    # A terminal needs a Material to be bindable. Author it as the shader's parent so the
    # network stays self-contained under one prim.
    mat_path = Sdf.Path(shader_path).GetParentPath()
    if mat_path.IsAbsoluteRootPath():
        return

    material = UsdShade.Material.Define(stage, mat_path)
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
    shader_path = chunk.LineEdit(
        "shader_path",
        "Shader Path",
        default_value="/materials/mtlx/shader",
        flags=ChunkFlags.ParamFlags.LABELED,
    )
    bind_to = chunk.LineEdit(
        "bind_to",
        "Bind To",
        flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    bind_to.set_state(ChunkFlags.State.HIDDEN)

    chunk_api.add_params([nodedef, shader_path, bind_to])
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

        Shader Path is where the Shader prim is authored. For a terminal shader (one
        whose context is surface, displacement or volume) a UsdShade.Material is also
        authored at the parent path, connected via outputs:mtlx:surface, and Bind To
        binds it to the prims you name. Intermediate nodes author only the Shader prim
        and are meant to be wired to other shaders in the node graph.
        """,
    )
