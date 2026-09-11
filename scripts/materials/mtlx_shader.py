from pxr import Sdf, Tf, Usd, UsdShade

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_utils, shader_defs
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_usd

SOURCE_TYPE = "mtlx"

# Scope new shader networks are authored under when the path is left to resolve itself.
_DEFAULT_SCOPE = "/materials"

# Params the chunk always owns. Everything else on the chunk was lofted from the current
# shader definition and gets torn down when the definition changes.
_FIXED = ("nodedef", "signature", "prim_path", "bind_to")

# family -> {category: identifier}, built once from the registry. Lets the signature menu
# list a family's sibling definitions without re-walking every shader on each rebuild.
_VARIANTS: dict = {}

# Which signature a menu entry starts on, best first. MaterialX spells the same operation
# once per operand type, and picking arbitrarily would land "add" on matrix33 as often as
# on float.
_PREFERRED = ("color3", "float", "vector3", "color4", "vector2")

# Child prim name a terminal's shader takes under its Material.
_SHADER_LEAF = "shader"

# Whether page groups start folded. A surface shader carries 40-odd inputs across nine
# pages and is unusable expanded, so this stays on. It used to cost ~1.3s of a 4s create
# (2.65s off against 3.99s on, measured 2026-08-08) -- all of it the re-cook each
# set_state(COLLAPSED) triggered, ~3,100 extra attribute writes interleaved with the widget
# build. Now that only DISABLED dirties a chunk it is free: 2.51-2.61s collapsed against
# 2.60s expanded, at identical Qt call counts *and* identical per-call cost. Kept as a
# switch because it is the cleanest A/B for anything that makes building groups expensive.
_COLLAPSE_PAGES = True

# Output attr types that deliberately differ from the shader's declared type, so that an
# output port and the input port it feeds agree on port type.
#
# The graph derives the two ends differently: an output port is typed from its USD attr
# (_sdf_to_port_type), an input port from its param class (_param_port_type). color3f needs
# no entry here -- it is a ColorPicker input and a Color3f output, and both read as a
# colour port. color4f has no ColorPicker (which is rgb only) so its input is a Vector, and
# Float4 is what puts that vector port on the same footing as the output.
_OUTPUT_SDF_TYPES = {
    "color4f": Sdf.ValueTypeNames.Float4,
}

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


def _signature_of(sdef) -> str:
    """What distinguishes a definition from its siblings, e.g. add's "color3" vs "color3FA".

    Taken from the identifier rather than the Sdr category, because MaterialX gives several
    definitions in a family the same category: ND_add_color3 and ND_add_color3FA (the
    color3-plus-float form) are both category color3, so keying on it drops one of them.
    """
    tail = sdef.identifier.removeprefix("ND_")
    if sdef.family and tail.startswith(f"{sdef.family}_"):
        tail = tail[len(sdef.family) + 1:]
    return tail or sdef.category or sdef.name


def _variants_for(family: str) -> dict:
    """{signature: identifier} for every definition sharing a family, e.g. add's operand types."""
    if not _VARIANTS:
        for d in shader_defs.iter_shader_defs(SOURCE_TYPE):
            _VARIANTS.setdefault(d.family, {})[_signature_of(d)] = d.identifier
    return _VARIANTS.get(family, {})


def _preferred(sdefs: list):
    """The definition a family's menu entry should start on."""
    for want in _PREFERRED:
        for d in sdefs:
            if _signature_of(d) == want:
                return d
    return sdefs[0]


def _sync_signature(owner, sdef) -> None:
    """Point the signature menu at this shader's family, selecting its own type.

    menu_items is assigned before value because Menu.convert_attr silently rewrites a
    value that is not in the list back to the first entry.

    Hidden for a family with nothing to choose between -- most pbr shaders exist in one
    form only, and a menu with a single entry is just clutter.
    """
    param = owner.get_param("signature")
    if param is None:
        return

    variants = _variants_for(sdef.family)
    param.menu_items = sorted(variants)

    signature = _signature_of(sdef)
    if signature in variants:
        param.value = signature

    if len(variants) > 1:
        param.unset_state(ChunkFlags.State.HIDDEN)
    else:
        param.set_state(ChunkFlags.State.HIDDEN)


def _chunk_loaded(owner, _notice, _sender):
    """Author the output ports once the chunk is in the scene -- and, for a chunk that
    came back from a saved network, loft its inputs back first.

    _rebuild cannot author the outputs: a variant's presets land before add_chunk
    (PARAM_ADDED builds widgets that read values immediately), so at that point the chunk
    has no prim for the attrs to hang off.

    Only outputs need that. Input ports come from the param list -- _place_param_ports
    builds an inputs:/outputs: pair for every connectable param whether or not an attr
    exists -- and the attr is authored lazily, when a value stops being default or when
    _author_connection wires the port. Forcing all of them cost ~114 USD writes per
    standard surface, each firing change notices the UI reacts to, and bought nothing.

    The load case is the other half. This chunk does not own its inputs; it lofts them
    from the nodedef, in the callback that fires when nodedef *changes*. Loading restores
    nodedef as a value rather than as a change, so nothing ever lofts them and the chunk
    arrives holding only _FIXED -- and execute(), which skips any input it has no param
    for, then cooks a bare shader at its definition defaults. Every authored value is
    silently gone, in a file that still looks intact on disk.

    Lofting here is also all it takes to get the values back. load_chunks runs
    _restore_dynamic_params immediately after this notice, for exactly this case: its
    earlier restore pass walked the same inputs:<Page>:<name> attrs before these params
    existed and had nowhere to put them, and the second pass finds them waiting.

    Blocked for the reason _rebuild blocks: each of the 40-odd add_param calls would
    otherwise re-cook the chunk on the spot.
    """
    sdef = _shader_def(owner)
    if sdef is None:
        return

    # Only _FIXED means nothing has lofted yet, which for a chunk being announced to the
    # scene means it was loaded rather than created. A definition that genuinely has no
    # inputs also lands here, and re-applying it is harmless.
    if all(p.name in _FIXED for p in owner.get_my_params()):
        was_blocked = getattr(owner, "_blocked", False)
        owner.set_blocked(True)
        try:
            _apply_definition(owner, sdef)
        finally:
            owner.set_blocked(was_blocked)
        return

    _author_outputs(owner, sdef)


def _signature_changed(owner, _notice, sender):
    """Swap to the sibling definition of the chosen type; params rebuild from there."""
    nodedef = owner.get_param("nodedef")
    if nodedef is None:
        return

    sdef = _shader_def(owner)
    if sdef is None:
        return

    identifier = _variants_for(sdef.family).get(sender.value)
    # Guarded so the write from _sync_signature (which re-selects the current type during
    # a rebuild) does not bounce straight back into another rebuild.
    if identifier and identifier != nodedef.value:
        nodedef.value = identifier


def _chunk_prim_paths(c) -> set:
    """Every prim path a chunk may be identified by, as a set of strings.

    A cooking chunk carries _exec_prim_path, which is what the rest of execute() matches
    on and need not equal what get_chunk_prim_path derives -- inside a chunkgroup or a
    view evaluation they differ, and comparing only one of them silently finds nothing.
    """
    paths = {
        getattr(c, "_exec_prim_path", None),
        getattr(c, "prim_path", None),
        chunk_usd.get_chunk_prim_path(c),
    }
    return {str(p) for p in paths if p}


def _consumer_chunk(node, stage, scene):
    """The chunk whose typed input is wired to this one's output, or None.

    A reverse lookup, because USD indexes connections only from the consumer side. The
    exec chain (inputs:chunk) is deliberately ignored: it says what cooks after what, not
    what feeds what, and a shader's place in the network follows the data.
    """
    my_paths = _chunk_prim_paths(node)
    if not my_paths:
        return None

    for other in scene.get_chunks():
        if other is node:
            continue

        for path in _chunk_prim_paths(other):
            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                continue

            for attr in prim.GetAttributes():
                name = attr.GetName()
                if not name.startswith("inputs:") or name == "inputs:chunk":
                    continue
                if any(str(c.GetPrimPath()) in my_paths for c in attr.GetConnections()):
                    return other

    return None


def _network_root(node, stage, scene) -> str:
    """The Material path of the terminal this shader ultimately feeds, or "".

    Walks consumer to consumer so a shader three nodes upstream of the surface still
    lands under that surface's Material -- a UsdShade network is conventionally flat
    under the Material it terminates in, not nested by wire depth.
    """
    seen = {id(node)}
    current = node

    while True:
        consumer = _consumer_chunk(current, stage, scene)
        if consumer is None or id(consumer) in seen:
            return ""
        seen.add(id(consumer))

        param = consumer.get_param("nodedef")
        if param is None:
            return ""

        sdef = shader_defs.get_shader_def((param.value or "").strip(), SOURCE_TYPE)
        if sdef is not None and sdef.is_terminal:
            path_param = consumer.get_param("prim_path")
            return (path_param.value or "").strip() if path_param else ""

        current = consumer


def _node_name(sdef) -> str:
    """The chunk's display name for a shader definition -- its identifier minus the ND_."""
    return Tf.MakeValidIdentifier(sdef.identifier.removeprefix("ND_") or sdef.name)


def _rename(owner, sdef) -> None:
    """Name the chunk after its shader, suffixed if a sibling already holds that name.

    Without this a second add lands on the same name as the first, the chunk prim rename
    fails outright ("Failed to rename prim /chunks/add_color3FA -> /chunks/add_float"),
    and the two chunks end up pointing at one prim path.

    get_valid_child_name strips a trailing number before re-suffixing, which is wrong for
    these names specifically: add_color3 would be handed back as add_color4, and that is a
    real and different MaterialX signature rather than a copy. Names ending in a digit get
    a separator first, so the collision reads as add_color3_1.
    """
    desired = _node_name(sdef)
    if owner.name == desired:
        return

    scene = getattr(chunk_registry.ChunkRegistry(), "scene", None)
    reserved = {c.name for c in scene.get_chunks() if c is not owner} if scene else set()
    if desired not in reserved:
        owner.name = desired
        return

    base = f"{desired}_" if desired[-1:].isdigit() else desired
    owner.name = owner.get_valid_child_name(base, reserved)


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
            if _COLLAPSE_PAGES:
                group.set_state(ChunkFlags.State.COLLAPSED)
            groups[prop.page] = group
            owner.add_param(group)
        group.add_param(param)


def _author_outputs(owner, sdef) -> None:
    """Author an outputs: attr per shader output, with no param behind it.

    An output is something to wire from, not something to edit, so it gets no widget.
    Hiding a param does not achieve that: _iter_connectable_params yields it regardless of
    visibility, which puts its outputs: attr in _place_prim_output_ports' "covered" set, so
    that function skips it -- and _place_param_ports, which would have drawn it, finds no
    visible widget to position against and skips it too. The port disappears entirely.

    A bare attr is in nobody's param iteration, so _place_prim_output_ports draws it as a
    labelled port on the node's right edge. It also keeps the shader's real type (color3f
    rather than the float3 a Vector param would round it to).
    """
    sync = chunk_usd.ChunkStageSync()
    if not getattr(sync, "initialized", False) or not getattr(sync, "_stage", None):
        return

    prim_path = sync._chunk_prim_paths.get(id(owner))
    prim = sync._stage.GetPrimAtPath(Sdf.Path(prim_path)) if prim_path else None
    if not (prim and prim.IsValid()):
        return

    wanted = {}
    for prop in sdef.outputs:
        if sdf_type := _OUTPUT_SDF_TYPES.get(prop.sdf_type) or Sdf.ValueTypeNames.Find(prop.sdf_type):
            wanted[f"outputs:{prop.name}"] = sdf_type

    # Drop what the previous definition left behind -- switching signature can rename an
    # output or retype it, and a stale attr would keep drawing a port that connects to
    # nothing. outputs:chunk is the exec chain and is not ours to touch.
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith("outputs:") or name == "outputs:chunk":
            continue
        if name not in wanted or attr.GetTypeName() != wanted[name]:
            sync._delete_stale_attr_specs(prim, name)

    for name, sdf_type in wanted.items():
        existing = prim.GetAttribute(name)
        if existing and existing.IsValid() and existing.GetTypeName() == sdf_type:
            continue
        with sync._chunk_prim_edit_context(owner):
            prim.CreateAttribute(name, sdf_type, custom=False)


def _apply_definition(owner, sdef) -> None:
    """Shape the chunk to a shader definition: signature, inputs, outputs, bindability.

    Everything here is derived purely from the definition, so it is what a chunk needs
    whether it just changed nodedef or just came back from a saved network. Shared
    between those two paths deliberately -- when the load path was missing pieces of it,
    a reopened network cooked bare shaders at their defaults and quietly lost its look.

    What is NOT here is the other half of a rebuild -- _ensure_prim_path and _rename --
    which is about where the chunk sits rather than what it is. A load has both of those
    restored from USD already, and re-deriving them would overwrite a saved path and
    rename every Standard Surface in the file back onto one name.
    """
    _sync_signature(owner, sdef)
    _loft_params(owner, sdef)
    _author_outputs(owner, sdef)

    # Binding only means anything for a terminal (a surface/displacement/volume); an
    # intermediate node like ND_multiply_float is wired to other shaders, never bound.
    bind = owner.get_param("bind_to")
    if bind is not None:
        if sdef.is_terminal:
            bind.unset_state(ChunkFlags.State.HIDDEN)
        else:
            bind.set_state(ChunkFlags.State.HIDDEN)


def _rebuild(owner):
    """Retarget the chunk at whatever nodedef is currently set.

    The whole rebuild runs blocked. Every param written here -- the signature, the path,
    each of 40-odd lofted values -- otherwise re-cooks the chunk on the spot: creating one
    Standard Surface executed it 11 times and ran wire_shader_input 378 times, about a
    quarter of a 6s create. Blocked, the work happens once and the single cook that
    matters comes from whatever asked for the rebuild.
    """
    was_blocked = getattr(owner, "_blocked", False)
    owner.set_blocked(True)
    try:
        _clear_lofted(owner)

        sdef = _shader_def(owner)
        if sdef is None:
            return

        _apply_definition(owner, sdef)
        _ensure_prim_path(owner, sdef)

        # Name the node after the shader it authors, so a graph of these reads as the
        # network it builds rather than as a column of identical "MaterialX Shader" boxes.
        _rename(owner, sdef)
    finally:
        owner.set_blocked(was_blocked)


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
    path_param = node.get_param("prim_path")

    home = (path_param.value or "").strip()
    if not home or not Sdf.Path.IsValidPathString(home):
        return

    # An intermediate belongs inside the network it feeds: a UsdShade Material is
    # conventionally self-contained, holding every shader that contributes to it. While
    # unwired it stays at its own path, and moves under the Material once connected.
    scene = getattr(chunk_registry.ChunkRegistry(), "scene", None)
    root = "" if sdef.is_terminal else (_network_root(node, stage, scene) if scene else "")

    # Uniquify the path actually being authored. Uniquifying `home` first and then
    # composing would carry a suffix earned at the old location into a nested slot that
    # was free -- an image landing on <material>/image1 with no image beside it.
    #
    # Passing the param (rather than its value) is what lets a re-cook keep its path:
    # get_unique_path clears this chunk's own procedural layer first, so it does not
    # collide with its own prim from last time and walk the suffix up on every execute.
    if root:
        prim_path = chunk_utils.get_unique_path(stage, f"{root}/{Sdf.Path(home).name}")
    else:
        prim_path = chunk_utils.get_unique_path(stage, path_param)

    if not prim_path or not Sdf.Path.IsValidPathString(prim_path):
        return

    # Show where the prim actually went. Guarded by the comparison so it converges: the
    # write re-cooks, the re-cook resolves the same path, and the second pass writes
    # nothing. Once it holds a resolved path, unwiring leaves the shader where it is
    # rather than moving it back out -- the param now says where it lives.
    if home != prim_path:
        path_param.value = prim_path

    # A terminal needs a Material to be bindable, and a Material cannot itself carry the
    # shader, so the shader becomes its child. Everything else IS the shader.
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
            # The bare param name, not its group-namespaced path. Two different attrs
            # carry these: the graph authors connections on inputs:<param.name> (its ports
            # are built from param.name alone, ignoring grouping), while chunk_usd stores
            # values on inputs:<Group>:<param.name>. Asking about the namespaced one finds
            # no connections, so wire_shader_input clears the shader input instead.
            chunk_utils.wire_shader_input(stage, chunk_prim, prop.name, inp.GetAttr())

    for prop in sdef.outputs:
        if sdf_type := Sdf.ValueTypeNames.Find(prop.sdf_type):
            shader.CreateOutput(prop.name, sdf_type)

    # Point each chunk-prim output port at the real shader output. This is the second hop
    # of the chain wire_shader_input walks: a downstream chunk's inputs: connection names
    # this chunk's prim, and it then reads *this* attr's connections to find the shader to
    # wire to. Without it a wire between two nodes resolves to nothing. Same authoring
    # (session layer, chunk-prim bookkeeping) as scripts/uv_texture.py.
    if chunk_prim and chunk_prim.IsValid():
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            for prop in sdef.outputs:
                attr = chunk_prim.GetAttribute(f"outputs:{prop.name}")
                if attr and attr.IsValid():
                    attr.SetConnections([Sdf.Path(f"{shader_path}.outputs:{prop.name}")])

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
    signature = chunk.Menu(
        "signature",
        "Signature",
        flags=ChunkFlags.ParamFlags.LABELED,
    )
    signature.set_state(ChunkFlags.State.HIDDEN)
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

    chunk_api.add_params([nodedef, signature, prim_path, bind_to])
    chunk_api.add_callback(_nodedef_changed, sender=nodedef)
    chunk_api.add_callback(_signature_changed, sender=signature)
    chunk_api.add_callback(
        _chunk_loaded,
        notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED,
    )

    return chunk_api


def _menu_variants():
    """The menu entries, built only when a menu is actually drawn.

    Walking the Sdr registry for 784 definitions costs ~1.9s, and config() runs at
    discovery in every process -- including `usdchunks run` and `sweep`, which never draw a
    menu. Handing ChunkConfig a callable moves that off the startup path entirely; it is
    resolved and memoised by ChunkConfig.get_variants on first use.

    One entry per family, grouped by role. MaterialX spells the same operation once
    MaterialX spells the same operation once per operand type -- 784 definitions are only
    217 distinct operations, with "convert" alone accounting for 47 -- so the entries are
    collapsed the way Houdini's MaterialX VOPs are, one node with a Signature to pick the
    type. Role is MaterialX's own nodegroup (math, pbr, adjustment, ...); family would be
    the wrong axis, being close to per-node, and grouping by it just moves the unnavigable
    list one level down.

    Empty in a runtime with no MaterialX (pip usd-core), which is the correct answer there
    rather than an error.
    """
    families: dict = {}
    for d in shader_defs.iter_shader_defs(SOURCE_TYPE):
        families.setdefault((d.role or d.context or "other", d.family), []).append(d)

    return [
        (f"{role}/{family}", {"nodedef": _preferred(defs).identifier})
        for (role, family), defs in families.items()
    ]


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.MTLX,
        variants=_menu_variants,
        description="""
        Authors a MaterialX shader as a UsdShade.Shader prim, with its params generated
        from the shader's own definition in USD's shader registry (Sdr).

        Node Definition is the Sdr identifier, e.g. ND_standard_surface_surfaceshader.
        Picking a shader from the chunk menu presets it; changing it rebuilds the params
        to match the new definition. Inputs are grouped into collapsible sections
        following the definition's own pages (Base, Specular, Coat, ...).

        Path is where the network is authored, and always reports where the prim actually
        went. Left blank it resolves itself to /materials/<shader name>, suffixed to the
        first free name -- so a second Standard Surface lands on standard_surface1 rather
        than on top of the first. Wiring a shader into another moves it under that
        network's Material, and Path updates to match.

        For a terminal shader (one whose context is surface, displacement or volume) Path
        is a UsdShade.Material, the shader is authored as its "shader" child and connected
        through outputs:mtlx:surface, and Bind To binds the material to the prims you name.
        Every other shader IS Path -- an intermediate node is wired to other shaders in the
        node graph, never bound, so it gets no Material wrapper.
        """,
    )
