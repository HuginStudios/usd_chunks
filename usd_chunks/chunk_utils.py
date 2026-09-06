import ast
import hashlib
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


# Where the authored expression text is kept for an attr that cannot hold it as its own
# value. Connections carry only the @refs@; the operators and literals between them live
# nowhere else, so a Float attr storing just the resolved number loses "* 2" on reload.
EXPR_TEXT_KEY = "chunk:expression"

# Arithmetic only. Names, calls, attribute access, subscripts and comparisons are all
# absent by construction, so a substituted expression can never reach anything but numbers
# and operators -- this is why the evaluation below can use compile()/eval() safely.
_ARITH_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub,
)

# A literal exponent above this is refused rather than evaluated: 2**10**9 is a few
# characters to type and would hang the UI thread computing a number nobody wants.
_MAX_EXPONENT = 64


def eval_arithmetic(text: str):
    """Evaluate text as a pure arithmetic expression, or return None if it isn't one.

    Used on an expression string whose @node.param@ refs have already been substituted for
    their values, so "20.0 * 3.0" arrives here and 60.0 goes back. None means "not
    arithmetic" -- a substituted prim path like "/world/geo/cube" or a label like
    "cube_01" lands there and keeps its string form.

    Args:
        text (str): The substituted expression string

    Returns:
        int | float | None: The computed number, or None if text is not arithmetic
    """
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None

    for node in ast.walk(tree):
        if not isinstance(node, _ARITH_NODES):
            return None
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            return None
        # bool is an int subclass, so True/False would otherwise pass the check above.
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            exp = node.right
            if not (isinstance(exp, ast.Constant) and abs(exp.value) <= _MAX_EXPONENT):
                return None

    try:
        result = eval(compile(tree, "<chunk expression>", "eval"), {"__builtins__": {}}, {})
    except (ArithmeticError, ValueError, TypeError):
        return None  # division by zero, overflow -- keep the string rather than raise
    return result if isinstance(result, (int, float)) and not isinstance(result, bool) else None


def store_expression_text(attr: Usd.Attribute, raw: str) -> None:
    """Keep the authored expression text with attr, wherever that type can hold it.

    A String attr holds the text as its own value (which is what lets _patch_expressions
    rewrite it on rename). Every other type stores the *resolved* value -- a Float input
    must read back as a number for anything downstream in USD -- so the text goes to
    customData under EXPR_TEXT_KEY instead.
    """
    if attr.GetTypeName() == Sdf.ValueTypeNames.String:
        attr.Set(raw)
    else:
        attr.SetCustomDataByKey(EXPR_TEXT_KEY, raw)


def read_expression_text(attr: Usd.Attribute) -> "str | None":
    """Return the expression text authored on attr, from whichever place it was kept."""
    if attr.GetTypeName() == Sdf.ValueTypeNames.String:
        val = attr.Get(0)
    else:
        val = attr.GetCustomDataByKey(EXPR_TEXT_KEY)
    return val if isinstance(val, str) and has_expression(val) else None


def clear_expression_text(attr: Usd.Attribute) -> None:
    """Drop any stored expression text -- the param no longer holds an expression."""
    if attr.GetTypeName() != Sdf.ValueTypeNames.String and attr.HasCustomDataKey(EXPR_TEXT_KEY):
        attr.ClearCustomDataByKey(EXPR_TEXT_KEY)


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

    Stored on the chunk's own prim, because everything it could be resolved from later can
    change: the name is editable, the prim path moves between contexts, and the in-memory
    hash does not survive a reload. Recording it is what makes it survive all three.

    Minting is a separate question from resolving, and the seed only has to be right at the
    single moment a chunk first needs an op -- see _mint_suffix.
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
        suffix = _mint_suffix(node, prim, stage)
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


def _mint_suffix(node, prim, stage) -> str:
    """Mint a suffix from what the chunk *is*, not from when it happened to run.

    This used to be uuid4: unique, but arbitrary. Building the same network twice minted two
    different ids, so a pipeline that regenerates its scene authored differently-named ops
    on every run -- `xformOp:transform:SimDrop_3ab48045` one time and `SimDrop_1b9f959b` the
    next, over identical transforms. Nothing downstream can hold an opinion on an attribute
    whose name is re-rolled each run, which is exactly what a re-runnable pipeline needs to
    be able to do.

    The chunk's prim path is what identifies it at the one moment a suffix is minted, so
    hashing that makes a whole network reproducible: same graph in, same op names out, on
    any machine. Stability under later change is unaffected, because this is only ever
    consulted to mint and never to resolve -- a chunk renamed or moved afterwards keeps the
    id it already recorded, which is what _stable_suffix's docstring is about.

    A derived id can hit one collision a random one cannot: a chunk created at a path an
    earlier chunk has since vacated. Rename T1 and make a new T1 and the second one seeds
    off the same path, while the first is still holding the suffix it recorded -- putting
    two chunks back on one op, the precise failure this naming exists to prevent. So salt
    until the suffix is free.
    """
    readable = _readable(node)
    seed = str(prim.GetPath()) if prim is not None else readable
    taken = _recorded_suffixes(stage, prim)
    for salt in range(1000):
        digest = hashlib.sha1(f"{seed}#{salt}".encode("utf-8")).hexdigest()[:8]
        candidate = f"{readable}_{digest}"
        if candidate not in taken:
            return candidate
    # Unreachable short of a thousand chunks sharing one name and path; a random id is a
    # better answer here than knowingly handing back a colliding one.
    return f"{readable}_{uuid.uuid4().hex[:8]}"


def _recorded_suffixes(stage, prim) -> set:
    """Every op suffix already recorded on a chunk prim in this stage, bar this chunk's."""
    if stage is None:
        return set()
    suffixes = set()
    for other in stage.TraverseAll():
        if other == prim or other.GetTypeName() != "Chunk":
            continue
        attr = other.GetAttribute(XFORM_OP_ID_ATTR)
        value = attr.Get() if attr and attr.IsValid() else None
        if value:
            suffixes.add(value)
    return suffixes


def _chunk_prim_of(stage, node):
    path = getattr(node, "_exec_prim_path", None) or getattr(node, "prim_path", None)
    if not path:
        return None
    prim = stage.GetPrimAtPath(Sdf.Path(str(path)))
    return prim if prim and prim.IsValid() else None



# NOT used, and kept as a signpost rather than deleted: the obvious fix for the ordering
# race is to give xformOpOrder a single writer in a layer shared by the graph, and it does
# fix it -- the chair case below passes. It also breaks four things, because the order then
# no longer mutes with its chunk: a muted or downstream chunk's op stays in the list, which
# breaks upstream isolation (test_scatter_is_reactive_and_ignores_downstream_transform,
# test_recook_for_view_reveals_upstream_incrementally,
# test_get_upstream_stage_preserves_real_relative_order_among_included_chunks) and leaves a
# deleted chunk's op named after it is gone (test_deleting_the_chunk_gives_the_asset_back).
#
# So the two placements each break something: per-chunk races, shared loses muting. A real
# fix has to satisfy both -- most likely by re-cooking every chunk that touches a prim when
# the participant set changes, so the per-chunk copies cannot go stale in the first place.
def _xform_order_layer_unused(stage, node):

    """The layer a chunk should author `xformOpOrder` into: shared, not its own.

    The VALUES belong in each chunk's own layer -- that is what makes them compose and what
    makes muting a chunk drop its contribution. The ORDER cannot work that way. It is a
    single attribute, so per-chunk copies compete, and USD keeps whichever layer is
    strongest rather than whichever is most recent. A chunk that cooked before its
    neighbour existed holds a stale list, and if its layer happens to outrank the others,
    that stale list wins and the neighbour's op is silently never read.

    Building every copy from the same source does not fix that, which was the previous
    attempt: identical copies only help if everyone re-cooks after every change, and they do
    not. Reported twice from real graphs -- a second Transform contributing nothing, then a
    baked simulation disappearing the moment its upstream Transform was nudged.

    So there is one copy, in the layer shared by the chunks of a graph, and the most recent
    cook wins. Each cook rebuilds the list from what is currently on the prim, so a stale
    entry survives at most until the next one.
    """
    manager = chunk_registry.ChunkLayerManager()
    own = node.get_edit_context() if hasattr(node, "get_edit_context") else None
    if own is not None:
        for parent in (getattr(manager, "graph_layers", None) or {}).values():
            if own.identifier in parent.subLayerPaths:
                return parent
    return own
