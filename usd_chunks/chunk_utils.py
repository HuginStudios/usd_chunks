import re
import uuid
from contextlib import contextmanager, ExitStack
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_flags import ChunkFlags

_EXPR_RE = re.compile(r'@(\w+)\.(\w+)@')


def get_edit_layer(stage: Usd.Stage, owner=None) -> Sdf.Layer:
    """Return the layer a chunk should author into for its exec-chain position: the layer
    published by the nearest Edit Target chunk reachable by walking the exec chain upstream
    from `owner`, or the session layer if there is no upstream Edit Target.

    A pure function of graph topology (see chunk_usd.nearest_upstream_edit_layer) — this
    replaces the old global ChunkStageSync._user_edit_layer state, so resolution no longer
    depends on which chunk happened to execute last.

    Use this instead of hardcoding stage.GetSessionLayer() for override/redirect-style
    opinions (variant selections, shader input redirects, etc.) so they follow whatever
    Edit Target applies at the authoring chunk's place in the graph, same as chunk prim
    bookkeeping does.

    owner is the authoring chunk. When None, falls back to the chunk whose execute() is
    currently running (chunk_notice._executing_chunk) — so callers invoked from inside a
    chunk's execute() (author_in_variant_context, wire_shader_input, …) resolve correctly
    without threading owner through every call site. With no owner at all, returns the
    session layer.

    The fallback is safe despite being ambient state because its sole writer
    (chunk_notice.py:1171-1185) save/restores it around each module run — set to the running
    module's own chunk right before module.execute(), popped back to the prior value in the
    finally — so it can never transiently point at the wrong chunk, even when a reconcile
    re-executes a chunk from inside another chunk's execute(). Anything running outside a
    module execute() (e.g. reconcile_edit_targets) passes owner explicitly rather than
    relying on this fallback.

    Local imports: plugins.chunk_usd imports this module at module scope, so importing it
    back at module scope here would fail depending on which of the two loads first.
    """
    if stage is None:
        return None
    if owner is None:
        from usd_chunks import chunk_notice
        owner = chunk_notice._executing_chunk
    if owner is None:
        return stage.GetSessionLayer()
    from usd_chunks.plugins.chunk_usd import nearest_upstream_edit_layer, get_chunk_prim_path
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(owner)))
    return nearest_upstream_edit_layer(stage, prim)


def get_bookkeeping_layer(stage: Usd.Stage, owner) -> Sdf.Layer:
    """Return where a chunk's own bookkeeping (its node prim's storage attrs, wiring,
    node-graph position) should be authored: get_edit_layer(stage, owner) for a regular
    chunk, or always the session layer for a CONTEXT chunk (Tab containers, groups),
    regardless of what target is currently active.

    CONTEXT chunks are structural scaffolding for the graph itself, not content —
    letting their own bookkeeping follow whatever target applies at their position would
    split a single container's own prim across layers over its lifetime, recreating the
    exact per-layer graph scatter get_edit_layer avoids for content. Regular (non-CONTEXT)
    chunks have no such risk, so they follow get_edit_layer like everything else.

    owner may be None (caller couldn't resolve an owning chunk) — treated as non-CONTEXT.
    """
    if owner is not None and owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
        return stage.GetSessionLayer()
    return get_edit_layer(stage, owner)


def get_attr_home_layer(attr: Usd.Attribute) -> Sdf.Layer | None:
    """Return the layer holding attr's strongest existing property spec, or None if it
    has no spec anywhere yet.

    Use this to update an attribute that already exists (value, connections, a rename),
    rather than re-resolving "the current Edit Target" on every change — a chunk's own
    bookkeeping attr can now live in whatever layer was active when it was first
    authored (get_bookkeeping_layer), and Edit Target may have moved on to somewhere
    else by the time it's edited again. Always writing new edits back to the attr's own
    existing home avoids splitting one attribute's opinions across multiple layers.
    """
    stack = attr.GetPropertyStack(Usd.TimeCode.Default())
    return stack[0].layer if stack else None


def chunk_internal_layer_ids() -> set:
    """Identifiers of layers owned by the chunk graph itself — every per-graph and
    per-chunk procedural layer, wherever they're currently sublayered (session layer
    by default, or an Edit-Target-chosen real layer if one is active).

    These are implementation details managed and Clear()ed by the chunk execution
    system; utility chunks that let a user pick a layer (e.g. edit-target or export
    pickers) should exclude them.

    Reads ChunkLayerManager's own incrementally-maintained internal_layer_ids set
    (same source of truth as chunk_registry.is_chunk_internal_layer) instead of
    deriving it by walking every tracked layer's live subLayerPaths — a callback
    that fires per stage edit, once per live Create Layer/Edit Target/Export USD
    chunk (see those modules' listen_globally refresh handlers), doing a full
    tree-walk here on top of that fan-out made chunk deletion cost scale with the
    total number of live chunks, not just the ones being deleted.
    """
    lm = chunk_registry.ChunkLayerManager()
    if not lm.initialized:
        return set()
    return set(lm.internal_layer_ids)


def find_collection(stage: Usd.Stage, name: str):
    """Find a UsdCollectionAPI named `name` anywhere on the stage, or None.

    Collections are looked up by name across the whole stage (no namespacing),
    matching Houdini's %collectionName resolution in primpattern fields.
    """
    for prim in stage.Traverse():
        for coll in Usd.CollectionAPI.GetAllCollections(prim):
            if coll.GetName() == name:
                return coll
    return None


def resolve_collection_members(stage: Usd.Stage, name: str) -> list[str]:
    """Return the resolved member prim paths of the collection named `name`, or []."""
    coll = find_collection(stage, name)
    if coll is None:
        return []
    query = coll.ComputeMembershipQuery()
    return [str(p) for p in Usd.ComputeIncludedPathsFromCollection(query, stage)]


def resolve_prim_paths(stage, location_str):
    """Split location_str on whitespace/commas and expand glob patterns and collection refs.

    Glob rules (Houdini-style):
      *   matches any characters within one path component (no slash crossing)
      **  matches any characters across any number of path components
      ?   matches one character within one path component

    /geo/*   → direct children of /geo only
    /geo/**  → everything recursively under /geo
    /**      → every prim on the stage
    mesh*    → root-level prims whose name starts with "mesh" (/ prepended)

    %name    → every prim in the UsdCollectionAPI named "name" (see find_collection),
               however it was authored (explicit includes/excludes, expansion rule, or
               a membership expression) — resolved via the collection's own query.
    """
    def _to_re(pattern):
        parts = []
        i = 0
        while i < len(pattern):
            if pattern[i:i+2] == "**":
                parts.append(".*")
                i += 2
            elif pattern[i] == "*":
                parts.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                parts.append("[^/]")
                i += 1
            else:
                parts.append(re.escape(pattern[i]))
                i += 1
        return re.compile("".join(parts) + "$")

    paths = []
    for token in re.split(r"[\s,]+", location_str.strip()):
        if not token:
            continue
        if token.startswith("%"):
            paths.extend(resolve_collection_members(stage, token[1:]))
            continue
        if not token.startswith("/"):
            token = "/" + token
        if token == "/":
            continue  # never target the pseudo-root (authoring an op on / raises)
        if "*" not in token and "?" not in token:
            paths.append(token)
            continue
        rx = _to_re(token)
        for prim in stage.Traverse():
            if rx.match(prim.GetPath().pathString):
                paths.append(prim.GetPath().pathString)
    # Order-preserving de-dup: a prim matched by both an explicit path and a
    # glob/collection should only be targeted once.
    return list(dict.fromkeys(paths))


def parse_expression(text: str) -> list[tuple[str, str]]:
    """Return (node_name, param_name) pairs for every @node.param@ token in text."""
    return _EXPR_RE.findall(text)


def has_expression(text: str) -> bool:
    return bool(_EXPR_RE.search(text))


def get_unique_path(stage, prim_path):
    """Return prim_path if unused on stage, else a unique variant with a numeric suffix.

    Also accepts a ChunkParamBase in place of a string.  If the param's raw value is an
    expression the resolved path is returned as-is — expressions define an explicit derived
    path, so uniqueness bumping would be wrong.

    When a param is passed, the chunk's procedural layer is cleared of the old prim spec
    before checking uniqueness, so previous executions don't inflate the suffix counter.
    """
    raw_value = getattr(prim_path, 'raw_value', None)
    if raw_value is not None:
        # Clear the chunk's procedural layer before resolving the path so stale prims
        # from a previous execution are removed regardless of whether the new path
        # comes from a literal value or an expression.
        chunk = getattr(prim_path, 'get_chunk', lambda: None)()
        if chunk is not None:
            layer = chunk.get_edit_context()
            if layer and layer.anonymous:
                layer.Clear()

        if isinstance(raw_value, str) and has_expression(raw_value):
            resolved = getattr(prim_path, 'value', None)
            return str(resolved) if resolved is not None else ""

        prim_path = getattr(prim_path, 'value', prim_path)

    if isinstance(prim_path, str) and has_expression(prim_path):
        return prim_path

    if not stage:
        return prim_path

    def _is_taken(path):
        prim = stage.GetPrimAtPath(path)
        return prim.IsValid() and bool(prim.GetTypeName())

    if not _is_taken(prim_path):
        return prim_path

    m = re.search(r'\d+$', prim_path)
    base = prim_path[: m.start()] if m else prim_path
    count = int(m.group()) + 1 if m else 1
    while _is_taken(f"{base}{count}"):
        count += 1

    return f"{base}{count}"


def split_variant_path(path_str: str) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Return (anchor_path, target_path, [(anchor_prim, variantSet, variant), ...]).

    anchor_path: first prim before any {} (the outermost anchor)
    target_path: full prim path with all variant selections stripped
    selections:  per-selection tuple including the prim that owns each variant set

    /myprim{LOD=high}/geom
        → ('/myprim', '/myprim/geom', [('/myprim', 'LOD', 'high')])

    /myprim{LOD=high}/geom/asset{season=summer}
        → ('/myprim', '/myprim/geom/asset',
           [('/myprim', 'LOD', 'high'), ('/myprim/geom/asset', 'season', 'summer')])

    /myprim
        → ('/myprim', '/myprim', [])
    """
    selections = []
    accumulated = ''
    remaining = path_str

    while remaining:
        path_match = re.match(r'^([^{]*)', remaining)
        path_part = path_match.group(1) if path_match else ''
        accumulated += path_part
        remaining = remaining[len(path_part):]

        var_match = re.match(r'^\{(\w+)=(\w+)\}', remaining)
        if var_match:
            anchor = accumulated.rstrip('/') or '/'
            selections.append((anchor, var_match.group(1), var_match.group(2)))
            remaining = remaining[var_match.end():]

    target = accumulated.rstrip('/') or '/'
    anchor = selections[0][0] if selections else target
    return anchor, target, selections


def wire_shader_input(
    stage: Usd.Stage,
    chunk_prim: Usd.Prim,
    param_name: str,
    shader_attr: Usd.Attribute,
    *,
    clear_on_miss: bool = False,
) -> bool:
    """Redirect a shader input to an upstream shader output when a chunk-level connection exists.

    When chunk_prim.inputs:<param_name> is connected to an upstream chunk's output port,
    that upstream chunk's execute() has already wired its outputs:<name> attr to the real
    shader output in the session layer. This function follows that two-hop chain and sets
    shader_attr directly to the upstream shader's output path.

    Writes redirects into the current edit layer (get_edit_layer — the user's chosen Edit
    Target if set, else the session layer) so they override the connect_usd_attr opinion
    (which _author_driven_connections also authors there). When no upstream connection is
    found that redirect is cleared, letting the anonymous-layer connect_usd_attr link (or
    nothing when clear_on_miss=True) take effect.

    Call after all connect_usd_attr calls in a chunk execute() for any shader input that
    can receive connections from other shader chunks in the nodegraph.

    Args:
        stage: the active USD stage.
        chunk_prim: this chunk's prim (from node._exec_prim_path or node.prim_path).
        param_name: the chunk param name whose nodegraph port carries the connection.
        shader_attr: the shader's USD attribute to wire (e.g. UsdUVTexture.inputs:st).
        clear_on_miss: if True, also clear shader_attr in the current layer when no
            upstream is found. Use for inputs that have no local value param fallback.

    Returns:
        True if an upstream shader connection was found and applied, False otherwise.
    """
    edit_layer = get_edit_layer(stage)

    if chunk_prim and chunk_prim.IsValid():
        chunk_input = chunk_prim.GetAttribute(f"inputs:{param_name}")
        if chunk_input and chunk_input.IsValid():
            for conn in chunk_input.GetConnections():
                upstream_prim = stage.GetPrimAtPath(conn.GetPrimPath())
                if not upstream_prim.IsValid():
                    continue
                # Nodegraph stores typed connections with inputs: on the source side;
                # the actual chunk output attr that carries the shader path uses outputs:.
                output_attr_name = "outputs:" + str(conn).rsplit(":", 1)[-1]
                upstream_output = upstream_prim.GetAttribute(output_attr_name)
                if not (upstream_output and upstream_output.IsValid()):
                    continue
                shader_conns = upstream_output.GetConnections()
                if shader_conns:
                    with Usd.EditContext(stage, edit_layer):
                        shader_attr.SetConnections([shader_conns[0]])
                    return True

    # No upstream connection found. Clear any redirect so the anonymous-layer
    # connect_usd_attr opinion (or cleared state) can take effect.
    with Usd.EditContext(stage, edit_layer):
        shader_attr.ClearConnections()
    if clear_on_miss:
        shader_attr.ClearConnections()
    return False


def iter_prim_targets(stage, param):
    """Yield target_path for every prim/pattern/collection resolved from `param`
    (a LineEdit-like param with .resolve_paths()), each already inside
    author_in_variant_context.

    Replaces the repeated:
        for path_str in param.resolve_paths():
            with author_in_variant_context(stage, path_str) as target_path:
                ...

    with:
        for target_path in iter_prim_targets(stage, param):
            ...
    """
    for path_str in param.resolve_paths():
        with author_in_variant_context(stage, path_str) as target_path:
            yield target_path


@contextmanager
def author_in_variant_context(stage, path_str):
    """Context manager that enters any variant edit contexts encoded in path_str.

    Yields target_path (variant specs stripped) inside the appropriate edit contexts.
    Supports variant selections at multiple levels of the path hierarchy.

    with author_in_variant_context(stage, '/prim{LOD=high}/geom/asset{season=summer}') as t:
        stage.DefinePrim(t, 'Xform')  # authored inside both variant contexts
    """
    _, target_path, var_selections = split_variant_path(path_str)

    edit_layer = get_edit_layer(stage)
    edit_target = Usd.EditTarget(edit_layer)

    with ExitStack() as stack:
        for anchor_prim_path, vs_name, variant in var_selections:
            anchor_prim = stage.GetPrimAtPath(anchor_prim_path)
            if not anchor_prim.IsValid():
                anchor_prim = stage.OverridePrim(anchor_prim_path)
            vset = anchor_prim.GetVariantSets().GetVariantSet(vs_name)

            # Record only the edit-layer opinion — not the composed selection.
            # Restoring the composed prev would bleed upstream chunk-layer defaults
            # back into the edit layer, overriding downstream overrides.
            spec = edit_layer.GetPrimAtPath(anchor_prim_path)
            prev_session = dict(spec.variantSelections).get(vs_name, "") if spec else ""

            with Usd.EditContext(stage, edit_target):
                vset.SetVariantSelection(variant)

            # On exit, restore only what was already in the edit layer.
            # If the edit layer had no prior opinion, clear it so the
            # edit layer stays free of composed (chunk-layer) opinions.
            def _restore(v=vset, p=prev_session):
                with Usd.EditContext(stage, edit_target):
                    if p:
                        v.SetVariantSelection(p)
                    else:
                        v.ClearVariantSelection()

            stack.callback(_restore)
            stack.enter_context(vset.GetVariantEditContext())
        yield target_path


def show_attribute(prim, target_group, collapse_non_inputs=True):
    """Populate a group with typed editable params for all attributes and relationships on a prim.

    This is the shared utility for dynamically building attribute editor UIs.
    Used by the Dynamic chunk, Light chunk, and any chunk that needs to expose
    a prim's attributes as interactive widgets.

    Args:
        prim: The Usd.Prim whose attributes to display.
        target_group: A ChunkGroupBase to populate with params. Existing children
            are cleared before populating.
        collapse_non_inputs: If True, namespace groups other than 'inputs' start
            collapsed. Keeps the UI tidy for prims with many attributes.
    """
    # Late imports to avoid circular dependency (chunk imports chunk_utils)
    from usd_chunks import chunk
    from usd_chunks import chunk_core

    # Clean existing params
    for grp in target_group.get_my_params():
        grp.request_delete()

    if not prim or not prim.IsValid():
        return

    # Generate params from attributes
    attributes = prim.GetAttributes()
    groups = {}
    for attr in attributes:
        if not (param := chunk.param_from_attr(attr)):
            continue

        if not isinstance(param, chunk.ChunkGroupBase):
            param.set_flag(chunk_core.ChunkFlags.ParamFlags.LABELED)

        ns = attr.GetNamespace()
        tgt_grp = target_group if not ns else groups.get(ns)

        if not tgt_grp:
            tgt_grp = chunk.Group(
                ns,
                flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
            )
            if collapse_non_inputs and ns and ns != "inputs":
                tgt_grp.set_state(chunk_core.ChunkFlags.State.COLLAPSED)

            target_group.add_param(tgt_grp)
            groups[ns] = tgt_grp

        if param:
            tgt_grp.add_param(param)

    # Generate params from relationships
    relationships = prim.GetRelationships()
    for rel in relationships:
        if not (param := chunk.param_from_rel(rel)):
            continue

        if not isinstance(param, chunk.ChunkGroupBase):
            param.set_flag(chunk_core.ChunkFlags.ParamFlags.LABELED)

        ns = rel.GetNamespace()
        tgt_grp = target_group if not ns else groups.get(ns)

        if not tgt_grp:
            tgt_grp = chunk.Group(
                ns,
                flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
            )
            target_group.add_param(tgt_grp)
            groups[ns] = tgt_grp

        if param and tgt_grp:
            tgt_grp.add_param(param)


# ---------------------------------------------------------------------------
# stable xformOp identity
# ---------------------------------------------------------------------------

#: Where a chunk records the op suffix it owns, so op -> chunk is a lookup rather than a
#: string match, and so the name survives a reload unchanged.
XFORM_OP_ID_ATTR = "chunk:xformOpId"

_XFORM_OP_PREFIX = "xformOp:transform:"
_SANITISE_RE = re.compile(r"[^A-Za-z0-9_]")


def existing_xform_op_name(node):
    """This chunk's op name if it already has one, without minting one.

    For asking "does this chunk own an op" while building someone else's order -- minting
    on that question would give an op to every chunk that was merely looked at.
    """
    suffix = getattr(node, "_xform_op_suffix", None)
    if suffix:
        return _XFORM_OP_PREFIX + suffix
    stage = node.get_stage() if hasattr(node, "get_stage") else None
    prim = _chunk_prim_of(stage, node) if stage is not None else None
    if prim is None:
        return None
    attr = prim.GetAttribute(XFORM_OP_ID_ATTR)
    if attr and attr.IsValid() and attr.Get():
        return _XFORM_OP_PREFIX + attr.Get()
    return None


def xform_op_name(node) -> str:
    """The xformOp suffix this chunk owns: readable, unique, and stable across renames.

    Chunks used to author `xformOp:transform` -- the same attribute on every chunk -- so
    two chunks placing one prim competed for one slot and USD discarded the loser. Owning a
    distinctly named op each is what lets them compose instead.

    The name carries two things because it has two jobs. The id is what identity rests on:
    generated once, recorded on the chunk's prim, and never regenerated, so renaming a
    chunk does not orphan the op it already authored. The readable prefix is there because
    the point of all this is to select a prim, look at its ops, and see which chunk each one
    belongs to -- `xformOp:transform:a3f2c1` answers nothing at a glance.

        xformOp:transform:Transform1_a3f2c1

    The prefix is taken from the chunk's name at first use and deliberately not kept in
    sync afterwards. Renaming the op would be possible -- unlike before, a chunk owns its
    attribute outright and nobody else writes it -- but it rewrites the file for a cosmetic
    gain, and anything that needs the true mapping should read XFORM_OP_ID_ATTR.
    """
    return _XFORM_OP_PREFIX + _stable_suffix(node)


def _readable(node) -> str:
    name = _SANITISE_RE.sub("_", str(getattr(node, "name", "") or "Transform"))
    return name or "Transform"


def _stable_suffix(node) -> str:
    """Read the chunk's op suffix, minting one on first use.

    The WHOLE suffix is stored, readable part included, not just the id. Recomputing the
    readable part from the chunk's current name on every call would change the op name on a
    rename -- orphaning the op the chunk had already authored, which is precisely what the
    stable id exists to prevent. The name is a snapshot from first use, by design.

    Stored on the chunk's own prim rather than derived from anything, because everything it
    could be derived from can change: the name is editable, the prim path moves between
    contexts, and the in-memory hash does not survive a reload.
    """
    cached = getattr(node, "_xform_op_suffix", None)
    if cached:
        return cached

    stage = node.get_stage() if hasattr(node, "get_stage") else None
    prim = _chunk_prim_of(stage, node) if stage is not None else None

    suffix = None
    if prim is not None:
        attr = prim.GetAttribute(XFORM_OP_ID_ATTR)
        if attr and attr.IsValid():
            suffix = attr.Get()

    if not suffix:
        suffix = f"{_readable(node)}_{uuid.uuid4().hex[:8]}"
        if prim is not None:
            # Into the chunk's own bookkeeping layer, like every other bit of chunk
            # bookkeeping -- not wherever the ambient edit target happens to point.
            with Usd.EditContext(stage, get_bookkeeping_layer(stage, node)):
                written = prim.CreateAttribute(
                    XFORM_OP_ID_ATTR, Sdf.ValueTypeNames.String, custom=True
                )
                written.SetVariability(Sdf.VariabilityUniform)
                written.Set(suffix)

    object.__setattr__(node, "_xform_op_suffix", suffix)
    return suffix


def _chunk_prim_of(stage, node):
    path = getattr(node, "_exec_prim_path", None) or getattr(node, "prim_path", None)
    if not path:
        return None
    prim = stage.GetPrimAtPath(Sdf.Path(str(path)))
    return prim if prim and prim.IsValid() else None
