from __future__ import annotations
import ast
import hashlib
import logging
import re
import uuid
from contextlib import contextmanager, ExitStack, nullcontext
from pxr import Sdf, Usd

from usd_chunks import chunk_naming
from usd_chunks import chunk_registry
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

_EXPR_RE = re.compile(r'@(\w+)\.(\w+)@')


_PATH_LIST_SPLIT = re.compile(r"([,\s]+)")


def apply_namespace(namespace: str, paths: str) -> str:
    """Put every absolute prim path in `paths` (one, or a comma/space-separated list, globs
    included) under `namespace`. A path already under it, or a relative one, is left
    alone, and so is the separators' spelling.
    """
    if not namespace:
        return paths
    root = namespace.rstrip("/")
    out = []
    for token in _PATH_LIST_SPLIT.split(paths):
        if token.startswith("/") and not (token == root or token.startswith(root + "/")):
            token = root + token
        out.append(token)
    return "".join(out)


def strip_namespace(namespace: str, paths: str) -> str:
    """The inverse of apply_namespace: every path under `namespace` made relative to it
    again -- the form a param inside a namespaced host stores.
    """
    if not namespace:
        return paths
    root = namespace.rstrip("/")
    out = []
    for token in _PATH_LIST_SPLIT.split(paths):
        if token == root:
            token = "/"
        elif token.startswith(root + "/"):
            token = token[len(root):]
        out.append(token)
    return "".join(out)


def get_edit_layer(stage: Usd.Stage, owner=None) -> Sdf.Layer:
    """Return the layer a chunk should author into for its exec-chain position: the layer
    published by the nearest Edit Target chunk reachable by walking the exec chain upstream
    from `owner`, or the session layer if there is no upstream Edit Target.

    A pure function of graph topology (see chunk_stage.nearest_upstream_edit_layer) -- resolution
    never depends on which chunk happened to execute last.

    Use this instead of hardcoding stage.GetSessionLayer() for override/redirect-style
    opinions (variant selections, shader input redirects, etc.) so they follow whatever
    Edit Target applies at the authoring chunk's place in the graph, same as chunk prim
    bookkeeping does.

    owner is the authoring chunk. When None, falls back to the chunk whose execute() is
    currently running (chunk_notice._executing_chunk) -- so callers invoked from inside a
    chunk's execute() (author_in_variant_context, wire_shader_input, …) resolve correctly
    without threading owner through every call site. With no owner at all, returns the
    session layer.

    The fallback is safe despite being ambient state because its sole writer
    (chunk_notice.py:1171-1185) save/restores it around each module run -- set to the running
    module's own chunk right before module.execute(), popped back to the prior value in the
    finally -- so it can never transiently point at the wrong chunk, even when a reconcile
    re-executes a chunk from inside another chunk's execute(). Anything running outside a
    module execute() (e.g. reconcile_edit_targets) passes owner explicitly rather than
    relying on this fallback.

    Local imports: chunk_stage imports this module at module scope, so importing it
    back at module scope here would fail depending on which of the two loads first.
    """
    if stage is None:
        return None
    if owner is None:
        from usd_chunks import chunk_notice
        owner = chunk_notice._executing_chunk
    if owner is None:
        return stage.GetSessionLayer()
    from usd_chunks.chunk_stage import nearest_upstream_edit_layer, get_chunk_prim_path
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(owner)))
    return nearest_upstream_edit_layer(stage, prim)


def get_bookkeeping_layer(stage: Usd.Stage, owner) -> Sdf.Layer:
    """Return where a chunk's own bookkeeping (its node prim's storage attrs, wiring,
    node-graph position) should be authored: get_edit_layer(stage, owner) for a regular
    chunk, or always the session layer for a CONTEXT chunk (Tab containers, groups),
    regardless of what target is currently active.

    CONTEXT chunks are structural scaffolding for the graph itself, not content --
    letting their own bookkeeping follow whatever target applies at their position would
    split a single container's own prim across layers over its lifetime, recreating the
    exact per-layer graph scatter get_edit_layer avoids for content. Regular (non-CONTEXT)
    chunks have no such risk, so they follow get_edit_layer like everything else.

    owner may be None (caller couldn't resolve an owning chunk) -- treated as non-CONTEXT.
    """
    if owner is not None and owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
        return stage.GetSessionLayer()
    if owner is not None:
        # A chunk composed in from a sequence file has an Edit Target chain of its own,
        # and it is the sequence's: what the shot writes on such a chunk is an override,
        # and it goes where the host's edit target points (host_edit_layer), not down the
        # sequence's chain. Same import shape as get_edit_layer above, same reason.
        from usd_chunks.chunk_stage import get_chunk_prim_path
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(owner)))
        if is_composed_in(prim):
            return host_edit_layer(stage)
    return get_edit_layer(stage, owner)


def host_edit_layer(stage: Usd.Stage) -> Sdf.Layer:
    """Where an override of composed-in content lands: the stage's own edit target.

    That is the layer the host has open for editing -- the root layer, i.e. the file the
    user opened, unless the host set another -- and deliberately not the chunk's Edit
    Target (get_edit_layer): a chunk brought in from a sequence file sits in the
    sequence's exec chain, so its Edit Target is the sequence's, locked at that level,
    and a shot-level tweak has no business going down it.

    The chunk system never leaves the stage's edit target changed, but it does enter
    Usd.EditContext blocks on a chunk's own procedural layer around a cook and around a
    callback. A write from inside one of those (a callback setting a sibling param) sees
    that layer as the ambient target, and it is Clear()ed on the next cook -- so an
    internal layer is never the answer, and the root layer stands in for it.
    """
    layer = stage.GetEditTarget().GetLayer()
    if layer is None or chunk_registry.is_chunk_internal_layer(layer):
        return stage.GetRootLayer()
    return layer


def is_composed_in(prim: Usd.Prim) -> bool:
    """Whether this prim is defined only by layers the chunk system does not own: every
    `def` for it is in a foreign layer (is_foreign_layer) or reached across a reference or
    payload, so any local opinion on it is an override. A prim that is not defined at all
    yet (mid-creation) is not composed in; neither is one with a `def` in an owned layer,
    however many foreign opinions sit beneath it.
    """
    if prim is None or not prim.IsValid():
        return False
    stage = prim.GetStage()
    defined = False
    for spec in prim.GetPrimStack():
        if spec.specifier != Sdf.SpecifierDef:
            continue
        defined = True
        if spec.path == prim.GetPath() and not is_foreign_layer(spec.layer, stage):
            return False
    return defined


def composed_in_from(prim: Usd.Prim) -> Sdf.Layer | None:
    """The layer a composed-in prim's strongest `def` comes from, or None when the prim is
    defined locally (or not at all). The layer a UI names when it says where a locked node
    came from, and the one is_composed_in is really asking about.
    """
    if prim is None or not prim.IsValid():
        return None
    stage = prim.GetStage()
    for spec in prim.GetPrimStack():
        if spec.specifier != Sdf.SpecifierDef:
            continue
        if spec.path == prim.GetPath() and not is_foreign_layer(spec.layer, stage):
            return None
        return spec.layer
    return None


def is_foreign_layer(layer: Sdf.Layer, stage: Usd.Stage) -> bool:
    """Whether the chunk system reads this layer but must not rewrite it.

    A file-backed layer that is not the stage's own root layer got onto the stage by
    composition -- a sequence network sublayered into a shot, a store chunkgroup
    referenced in, a scene file a Sublayer chunk pulled in -- and belongs to whoever
    wrote that file. Everything the chunk system makes for itself is anonymous (the
    session layer, graph and per-chunk procedural layers, Create Layer's layers), and the
    root layer is the file the user opened to edit, so those two tests are the whole rule.

    What "must not rewrite" buys: a shot that composes a sequence tweaks it with sparse
    `over`s in the host's edit target (host_edit_layer) and never touches the sequence
    file, the same discipline USD has for every other kind of content. A user who wants
    to edit a foreign file directly says so by making it the stage's edit target; the
    override then lands there of its own accord, and this predicate does not need to know.
    """
    return not layer.anonymous and layer != stage.GetRootLayer()


def get_attr_home_layer(attr: Usd.Attribute) -> Sdf.Layer | None:
    """Return the layer to update attr in: the one holding its strongest existing
    property spec, or -- when that spec is composed in from somewhere the chunk system
    does not write -- the host's edit target, where the override belongs. None only when
    the attr has no spec anywhere yet.

    Use this to update an attribute that already exists (value, connections, a rename),
    rather than re-resolving "the current Edit Target" on every change -- a chunk's own
    bookkeeping attr can now live in whatever layer was active when it was first
    authored (get_bookkeeping_layer), and Edit Target may have moved on to somewhere
    else by the time it's edited again. Always writing new edits back to the attr's own
    existing home avoids splitting one attribute's opinions across multiple layers.

    Two kinds of strongest spec are not a home, and answer host_edit_layer instead:

    - one in a foreign layer (is_foreign_layer): a sublayered sequence file. Writing
      there would edit the sequence from the shot.
    - one whose path is not the attr's stage path: reached through a reference or
      payload, so the spec's layer is not even in the local layer stack and
      Usd.EditContext on it raises "not in the local LayerStack".
    """
    stack = attr.GetPropertyStack(Usd.TimeCode.Default())
    if not stack:
        return None
    spec = stack[0]
    stage = attr.GetStage()
    if spec.path != attr.GetPath() or is_foreign_layer(spec.layer, stage):
        return host_edit_layer(stage)
    return spec.layer


def chunk_internal_layer_ids() -> set:
    """Identifiers of layers owned by the chunk graph itself -- every per-graph and
    per-chunk procedural layer, wherever they're currently sublayered (session layer
    by default, or an Edit-Target-chosen real layer if one is active).

    These are implementation details managed and Clear()ed by the chunk execution
    system; utility chunks that let a user pick a layer (e.g. edit-target or export
    pickers) should exclude them.

    Reads ChunkLayerManager's own incrementally-maintained internal_layer_ids set
    (same source of truth as chunk_registry.is_chunk_internal_layer) instead of
    deriving it by walking every tracked layer's live subLayerPaths -- a callback
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
               a membership expression) -- resolved via the collection's own query.

    A glob may follow a variant selection: /asset{color=red}/**/*Body* matches under
    /asset with "red" selected, and every match comes back carrying the selection --
    /asset{color=red}/geo/Body_L -- so whatever authors to it lands inside that variant
    (author_in_variant_context). The pattern is matched with the variant selected, since
    a prim that only exists in "red" is not on the stage until "red" is; the selection
    is put back afterwards. A glob cannot precede a selection: /a*{x=1}/b has no anchor.
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
        _anchor, pattern, selections = split_variant_path(token)
        rx = _to_re(pattern)
        matched = []
        with author_in_variant_context(stage, token) if selections else nullcontext():
            for prim in stage.Traverse():
                if rx.match(prim.GetPath().pathString):
                    matched.append(prim.GetPath().pathString)
        paths.extend(_with_variant_selections(path, selections) for path in matched)
    # Order-preserving de-dup: a prim matched by both an explicit path and a
    # glob/collection should only be targeted once.
    return list(dict.fromkeys(paths))


def _with_variant_selections(path: str, selections) -> str:
    """Put a glob's variant selections back onto a path it matched.

    /asset/geo/Body_L with [("/asset", "color", "red")] → /asset{color=red}/geo/Body_L.
    Deepest anchor first, so an earlier insertion never shifts a later anchor.
    """
    for anchor, vset, variant in sorted(selections, key=lambda s: -len(s[0])):
        if path == anchor or path.startswith(anchor.rstrip("/") + "/"):
            path = f"{anchor}{{{vset}={variant}}}{path[len(anchor):]}"
    return path


def parse_expression(text: str) -> list[tuple[str, str]]:
    """Return (node_name, param_name) pairs for every @node.param@ token in text."""
    return _EXPR_RE.findall(text)


def has_expression(text: str) -> bool:
    return bool(_EXPR_RE.search(text))


# The attribute types a Python str is a legitimate value for. Not just String: a token
# takes one (every schema enum -- visibility, purpose, a light's shaderId) and so does an
# asset (a texture file). The guards that keep unresolved expression text out of a typed
# attr must test against all three; testing `!= String` alone would drop every real string
# bound for a token or asset, such as a dome light's texture.
_TEXT_TYPES = (
    Sdf.ValueTypeNames.String,
    Sdf.ValueTypeNames.Token,
    Sdf.ValueTypeNames.Asset,
)


def attr_holds_text(attr: Usd.Attribute) -> bool:
    """Whether a str can be Set() on attr: its type is String, Token or Asset."""
    return attr.GetTypeName() in _TEXT_TYPES


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


def read_expression_text(attr: Usd.Attribute) -> str | None:
    """Return the expression text authored on attr, from whichever place it was kept.

    Accepts either form. A @Node.param@ wire always comes with connections on the attr;
    a ${VAR} stage expression never does, since there is no other param to point at. So
    the two never arrive together and one reader serves both call sites.
    """
    if attr.GetTypeName() == Sdf.ValueTypeNames.String:
        val = attr.Get(0)
    else:
        val = attr.GetCustomDataByKey(EXPR_TEXT_KEY)
    return val if isinstance(val, str) and is_expression_text(val) else None


def is_expression_text(value) -> bool:
    """True if value is a @Node.param@ wire, whose text must be preserved verbatim rather
    than stored as the typed value it resolves to.

    A ${VAR} does not need this and no longer counts: it only ever sits in an asset path,
    which is a string param, so its text IS its stored value -- and usd_chunks never
    resolves it in the first place. USD does, during composition.
    """
    return isinstance(value, str) and has_expression(value)


def clear_expression_text(attr: Usd.Attribute) -> None:
    """Drop any stored expression text -- the param no longer holds an expression."""
    if attr.GetTypeName() != Sdf.ValueTypeNames.String and attr.HasCustomDataKey(EXPR_TEXT_KEY):
        attr.ClearCustomDataByKey(EXPR_TEXT_KEY)


# A lofted param's label is a generated default (chunk_stage._lofted_label) -- unlike the
# published attribute name, it isn't addressed by anything, so there is nowhere else it has
# to be recorded once a user renames it (chunk_qt_widgets' "Rename..."). Same customData
# pattern as EXPR_TEXT_KEY: a label is display-only bookkeeping, not a value, so it never
# competes with whatever the attribute's own type actually stores.
LOFTED_LABEL_KEY = "chunk:loftedLabel"


def store_lofted_label(attr: Usd.Attribute, label: str) -> None:
    """Record a user's rename of a lofted param so it survives a reload.

    _restore_lofted_params reads this back and, if present, uses it verbatim instead of
    re-deriving the generated "{Chunk}: {label}" default -- a custom name is exactly what
    it has to override.
    """
    attr.SetCustomDataByKey(LOFTED_LABEL_KEY, label)


def read_lofted_label(attr: Usd.Attribute) -> str | None:
    """The label a user gave a lofted param, or None if it still has its generated default."""
    val = attr.GetCustomDataByKey(LOFTED_LABEL_KEY)
    return val if isinstance(val, str) and val else None


# --------------------------------------------------------------------------------------
# Stage expression variables
#
# USD's own variable mechanism, not the @Node.param@ one above. A layer carries an
# `expressionVariables` dict and composition substitutes `${NAME}` from it -- but *only*
# in asset paths and variant selections, and only from the root layer stack (a sublayer's
# dict is ignored entirely). Chunk layers are sublayers, so a chunk authoring its own
# expressionVariables would have no effect at all.
#
# Two consequences shape everything below:
#
# 1. The variables are read from the composed stage (root layer + session layer, session
#    winning), so variables a studio already authored in the file they opened are visible
#    and usable exactly as they are. usd_chunks adds to that set rather than owning it.
# 2. Prim paths are not one of the fields USD substitutes into, so ${NAME} in a chunk's
#    Prim Path param has to be expanded here, at param-read time, alongside @Node.param@.
#    That is what makes "use it in a LineEdit like a prim path" work at all.

#: A bare ${NAME} reference anywhere inside a string. USD proper only accepts a `${NAME}`
#: expression as the *whole* field, delimited by backticks; this looser form is what makes
#: a prim path like "/World/${SHOT}/geo" -- a field USD never substitutes into -- usable.
#: Both are supported: resolve_stage_expression dispatches on which one it is looking at.
_STAGE_VAR_RE = re.compile(r"\$\{(\w+)\}")

#: Layer metadata key holding the variable dict. Named by USD, not by us.
EXPRESSION_VARIABLES = "expressionVariables"


def stage_expression_variables(stage: Usd.Stage) -> dict:
    """The expression variables in effect on stage, session layer winning over root.

    Read off the stage's composed metadata rather than by merging the two layers by hand,
    so the precedence matches what Pcp actually applies during composition -- verified
    equal, including a session opinion overriding a root one of the same name.

    Args:
        stage (Usd.Stage): The stage to read from

    Returns:
        dict: variable name -> value, empty when nothing is authored anywhere
    """
    if stage is None:
        return {}
    value = stage.GetMetadata(EXPRESSION_VARIABLES)
    return dict(value) if value else {}


def has_stage_expression(text) -> bool:
    """True if text carries a ${NAME} reference in either accepted form."""
    if not isinstance(text, str):
        return False
    return bool(_STAGE_VAR_RE.search(text)) or Sdf.VariableExpression.IsExpression(text)


def resolve_stage_expression(text, stage: Usd.Stage):
    """Substitute stage expression variables into text.

    A fully-delimited USD expression (`${SHOT}` or `"./${ASSET}.usda"` in backticks) goes
    through Sdf.VariableExpression, so the studio syntax already in their files evaluates
    by USD's own rules -- including the typed and function forms this module makes no
    attempt to reimplement. Anything else is treated as text with bare ${NAME} references
    substituted into it, which is the form a prim path needs.

    An unknown variable is left as written rather than blanked. A path that silently
    became "/World//geo" would author into the wrong place and look like a typo in the
    field; leaving ${SHOT} visible in the value says which variable is missing.

    Args:
        text (str): The value to resolve
        stage (Usd.Stage): The stage whose variables apply

    Returns:
        The evaluated value -- typed for a full USD expression, a string otherwise
    """
    if not isinstance(text, str):
        return text
    variables = stage_expression_variables(stage)

    if Sdf.VariableExpression.IsExpression(text):
        result = Sdf.VariableExpression(text).Evaluate(variables)
        if result.errors:
            logger.warning("Stage expression %r: %s", text, "; ".join(result.errors))
            return text
        return text if result.value is None else result.value

    def sub(match):
        return str(variables.get(match.group(1), match.group(0)))

    return _STAGE_VAR_RE.sub(sub, text)


def set_stage_expression_variables(stage: Usd.Stage, variables: dict) -> None:
    """Author variables into the stage's session layer, replacing what is there.

    The session layer rather than the root layer, and that choice is load-bearing in both
    directions. It is where usd_chunks already keeps its own bookkeeping, so a chunk
    setting a variable does not dirty the file the user opened -- and composition takes
    the session opinion over the root layer's, so a variable set here overrides a
    studio-authored default of the same name instead of fighting it.

    **Changing** a variable recomposes the stage live. **Introducing** one that did not
    exist when the stage was opened does not: an arc whose ${NAME} failed to resolve
    records no dependency on NAME, so defining it later leaves that arc unresolved until
    the stage is reopened (true of the root layer too -- it is a property of Pcp's
    dependency tracking, not of the layer chosen here). Params resolved through
    resolve_stage_expression are unaffected; they read the dict on every access.

    Args:
        stage (Usd.Stage): The stage to author on
        variables (dict): The complete set this call is responsible for
    """
    if stage is None:
        return
    session = stage.GetSessionLayer()
    if variables:
        session.expressionVariables = dict(variables)
    elif session.HasExpressionVariables():
        session.ClearExpressionVariables()


def get_unique_path(stage, prim_path):
    """Return prim_path if unused on stage, else a unique variant with a numeric suffix.

    Also accepts a ChunkParamBase in place of a string.  If the param's raw value is an
    expression the resolved path is returned as-is -- expressions define an explicit derived
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

    Writes redirects into the current edit layer (get_edit_layer -- the user's chosen Edit
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

    Author inside the loop body, never after it. The variant context is open only while
    the generator is suspended at its yield: `list(iter_prim_targets(...))` walks every
    path and leaves every context, so anything authored to those paths afterwards lands
    outside the variant. Reading a prim after the loop is fine; binding, defining or
    setting on it is not -- that is how Bind Material put "/asset{color=red}/geo" bindings
    on /asset/geo for every colour. A caller that has to collect first re-enters with
    author_in_variant_context per path string when it writes.
    """
    for path_str in param.resolve_paths():
        with author_in_variant_context(stage, path_str) as target_path:
            yield target_path


def define_with_ancestors(stage, path, type_name="Xform", ancestor_type="Xform"):
    """Define `path` with `type_name`, giving every ancestor along the way a type too.

    Usd.Stage.DefinePrim types only the prim it is asked for; the ancestors it has to
    create to get there come out as typeless `def`s. That stays invisible until someone
    wants to move the result -- a typeless prim is not Xformable, so a network dropped at
    /world/house/bedroom has no transform to grab and nothing for the gizmo system to
    anchor to, even though every prim underneath it is a well-formed Xform.

    An ancestor that already carries a type keeps it; one that does not is given
    `ancestor_type`, which is the repair as much as the prevention: the chunks inside a
    hosted network author their own prims deep under the host's namespace, so the
    typeless ancestors may already be on the stage by the time the host gets here.

    The prim at `path` itself is always defined as `type_name`, whatever was there before.
    That is the difference between this and a plain "create it if missing": a path that
    already holds a `Scope`, or an `over` carrying a typeName, would otherwise be left as
    it is and the caller would get back a root it cannot transform.

    Returns the prim at `path`.
    """
    sdf_path = Sdf.Path(str(path))
    prim = None
    for prefix in sdf_path.GetPrefixes():
        if prefix != sdf_path:
            # An ancestor that is already really there keeps whatever type it has; this
            # function is only here to stop one arriving typeless. IsDefined, not IsValid:
            # an `over` is valid, so IsValid would skip `over "World" (typeName = "Xform")`
            # and leave it undefined -- which is precisely the shape chunk_layers
            # .define_ancestors exists to repair, so it must not be created here.
            ancestor = stage.GetPrimAtPath(prefix)
            if ancestor and ancestor.IsDefined() and ancestor.GetTypeName():
                prim = ancestor
                continue
            prim = stage.DefinePrim(prefix, ancestor_type)
            continue

        # The leaf is what the caller asked for, so it is always defined as `type_name`.
        # Skipping it when it happens to be typed already would let importing onto an
        # existing Scope hand back a root nothing can be transformed by. DefinePrim is
        # idempotent and will retype, which is the point.
        prim = stage.DefinePrim(prefix, type_name)
    return prim


@contextmanager
def author_in_variant_context(stage, path_str):
    """Context manager that enters any variant edit contexts encoded in path_str.

    Yields target_path (variant specs stripped) inside the appropriate edit contexts.
    Supports variant selections at multiple levels of the path hierarchy.

    with author_in_variant_context(stage, '/prim{LOD=high}/geom/asset{season=summer}') as t:
        stage.DefinePrim(t, 'Xform')  # authored inside both variant contexts
    """
    _, target_path, var_selections = split_variant_path(path_str)
    with author_in_variant_selections(stage, var_selections):
        yield target_path


#: The variant scopes this process currently holds an edit context open for, as
#: {(id(stage), anchor_prim_path, vs_name): variant}.
#:
#: GetVariantEditContext() builds its path mapping on top of whichever edit target is
#: already active, so entering {color=Black} while already inside it maps /Porsche_911 to
#: /Porsche_911{color=Black}{color=Black}/... -- a spec path nothing composes, so the edit
#: silently goes nowhere and the file grows a variantSet nested inside its own variant.
#:
#: Re-entry is the normal case here, not an exotic one: the Property Editor holds one scope
#: open across show_attribute, and the PARAM_ADDED that add_param fires SYNCHRONOUSLY runs
#: the whole _sync_chunk_inputs -> _drive_from_storage -> connect_usd_attr ->
#: _author_driven_connections path, which re-enters the same recorded selections to author
#: its connection. Every entry is popped in the ExitStack teardown below, so an id(stage)
#: key cannot outlive its stage and be wrongly reused.
_ACTIVE_VARIANT_SCOPES: dict = {}


@contextmanager
def author_in_variant_selections(stage, var_selections):
    """The lower half of author_in_variant_context, split out for a caller that already
    holds a parsed selections list -- (anchor_prim_path, vs_name, variant) tuples, as
    split_variant_path returns them -- rather than a path string to parse.

    A param bound to a real prim's attribute while a variant was selected (show_attribute,
    called from inside author_in_variant_context) is the case that needs this directly: it
    remembers the selections active when it was built, and re-enters them here on every
    later edit. get_attr_home_layer cannot stand in for this -- a variant's content is
    keyed by a different internal Sdf path, invisible to a plain layer-stack walk for the
    attribute's composed path, so it never finds a spec there and the edit falls through to
    the session layer, landing outside the variant entirely.

    Re-entrant: a scope already open (_ACTIVE_VARIANT_SCOPES) is left alone rather than
    entered twice, which would nest the variant inside itself and send the write nowhere.
    """
    edit_layer = get_edit_layer(stage)
    edit_target = Usd.EditTarget(edit_layer)

    with ExitStack() as stack:
        for anchor_prim_path, vs_name, variant in var_selections:
            scope = (id(stage), str(anchor_prim_path), vs_name)
            if scope in _ACTIVE_VARIANT_SCOPES:
                if _ACTIVE_VARIANT_SCOPES[scope] != variant:
                    # Authoring into a DIFFERENT variant of a set an enclosing scope already
                    # holds open. Nesting it would write nowhere and skipping it writes into
                    # the enclosing variant instead -- both wrong, so say so rather than let
                    # one of them look like it worked.
                    logger.warning(
                        "Variant scope %s{%s=%s} is already open as %r; authoring inside the "
                        "open one instead of nesting",
                        anchor_prim_path, vs_name, variant, _ACTIVE_VARIANT_SCOPES[scope],
                    )
                continue

            anchor_prim = stage.GetPrimAtPath(anchor_prim_path)
            if not anchor_prim.IsValid():
                anchor_prim = stage.OverridePrim(anchor_prim_path)
            vset = anchor_prim.GetVariantSets().GetVariantSet(vs_name)

            # Record only the edit-layer opinion -- not the composed selection.
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

            _ACTIVE_VARIANT_SCOPES[scope] = variant
            # LIFO teardown: leave the variant edit context, restore the selection, then
            # drop the scope -- so a nested re-entry still sees it open until we are out.
            stack.callback(_ACTIVE_VARIANT_SCOPES.pop, scope, None)
            stack.callback(_restore)
            stack.enter_context(vset.GetVariantEditContext())
        yield


def edit_context_for(stage, var_selections, fallback_layer=None):
    """The one edit context for authoring an attribute's value: inside var_selections if
    given (re-entering the recorded variant via author_in_variant_selections), otherwise
    fallback_layer -- or the ambient edit target if fallback_layer is None.

    var_selections cannot be resolved from the attribute alone the way get_attr_home_layer
    resolves a plain layer: composition only shows the CURRENTLY selected variant's specs,
    so by the time a later edit fires the stage's active selection has usually already
    moved on (author_in_variant_context restores it on exit, same as a param built from one
    variant is often edited while a different one is showing) -- walking the attribute's
    live composition at write time would silently resolve the wrong variant, or a variant
    that isn't the one this write is supposed to land in. var_selections is what a caller
    recorded at the moment it resolved the RIGHT one (chunk_utils.split_variant_path /
    show_attribute), and carrying it forward explicitly is the only way to still have it.

    This is the shared shape behind every "inside a variant, or the ordinary case" branch
    the chunk-prim/driven-attr write paths need (ChunkUsdAttributeSupport.data_changed,
    _write_driven_attrs, _author_driven_connections, ChunkVectorBase.data_changed,
    chunk_stage._push_driven_values) -- collapsing them into one call is what keeps a future
    write site's fallback from quietly drifting from the others'.
    """
    if var_selections:
        return author_in_variant_selections(stage, var_selections)
    if fallback_layer is None:
        return nullcontext()
    return Usd.EditContext(stage, fallback_layer)


def show_attribute(prim, target_group, collapse_non_inputs=True, var_selections=()):
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
        var_selections: The (anchor_prim_path, vs_name, variant) tuples active when `prim`
            was resolved (split_variant_path / author_in_variant_context), if any. Recorded
            on each generated attribute param so a later edit re-enters the same variant
            (author_in_variant_selections) instead of authoring outside it -- see
            ChunkUsdAttributeSupport.data_changed. Pass () for a prim reached outside any
            variant context, which is the ordinary case and costs nothing extra.
    """
    # Late imports to avoid circular dependency (chunk imports chunk_utils)
    from usd_chunks import chunk
    from usd_chunks import chunk_core

    if not prim or not prim.IsValid():
        # Clean existing params: no prim means nothing to show.
        for grp in target_group.get_my_params():
            grp.request_delete()
        return

    # Read the prim FIRST, and build every param, before touching target_group at all.
    #
    # Installing a param fires notices; a cook reached that way can recompose the stage; and
    # a resync EXPIRES this prim handle along with every attribute already read off it, so
    # the next read raises. For example, a Stage Expression Switch writes the record of
    # what it muted, that write cascades into cooking a light on the branch it just
    # suppressed, and the light's own prim is gone by the time this loop reaches its
    # second attribute.
    #
    # Raising there is not a local failure -- the exception would escape into
    # gate_to_branches and leave the gate half done, with the branch it was suppressing
    # still composed. Collecting first turns that into a lost cook rather than a
    # half-emptied chunk: nothing has been deleted yet, the params the chunk already has
    # stay exactly as they were, and the next cook rebuilds them.
    prim_path = prim.GetPath()
    specs = []
    try:
        for attr in prim.GetAttributes():
            specs.append((attr.GetNamespace(),
                          chunk.param_from_attr(attr, var_selections=var_selections),
                          collapse_non_inputs))
        for rel in prim.GetRelationships():
            specs.append((rel.GetNamespace(), chunk.param_from_rel(rel), False))
    except RuntimeError:
        logger.debug("show_attribute: %s went away while it was being read; leaving this "
                     "chunk's params alone for the next cook", prim_path)
        return

    # Clean existing params, now that there is something to replace them with.
    for grp in target_group.get_my_params():
        grp.request_delete()

    groups = {}
    for ns, param, collapse in specs:
        if not param:
            continue

        if not isinstance(param, chunk.ChunkGroupBase):
            param.set_flag(chunk_core.ChunkFlags.ParamFlags.LABELED)

        tgt_grp = target_group if not ns else groups.get(ns)
        if not tgt_grp:
            tgt_grp = chunk.Group(
                ns,
                flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
            )
            if collapse and ns and ns != "inputs":
                tgt_grp.set_state(chunk_core.ChunkFlags.State.COLLAPSED)
            target_group.add_param(tgt_grp)
            groups[ns] = tgt_grp

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

    If every chunk authored the same `xformOp:transform`, two chunks placing one prim would
    compete for one slot and USD would discard the loser. Owning a distinctly named op each
    is what lets them compose instead.

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

    Deterministic, not uuid4: a random id would mint different names each time the same
    network is built, so a pipeline that regenerates its scene would author
    differently-named ops on every run over identical transforms. Nothing downstream can hold an opinion on an attribute
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
        digest = hashlib.sha1(f"{seed}#{salt}".encode()).hexdigest()[:8]
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
        if other == prim or other.GetTypeName() != chunk_naming.CHUNK_PRIM_TYPENAME:
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
