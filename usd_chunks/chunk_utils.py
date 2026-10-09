"""USD helpers shared by the chunk system and by chunk modules.

Which layer a write belongs in (a chunk's Edit Target, its bookkeeping layer, or the
host's edit target for an override), prim path patterns and namespaces, expression text
kept beside typed values, variant-aware authoring, and per-chunk xformOp names.
"""
from __future__ import annotations
import ast
import bisect
import hashlib
import logging
import os
import re
import uuid
from collections.abc import Iterable
from contextlib import contextmanager, ExitStack, nullcontext
from pxr import Ar, Gf, Pcp, Sdf, Usd, UsdGeom, UsdShade

from usd_chunks import chunk_naming
from usd_chunks import chunk_notice
from usd_chunks import chunk_registry
from usd_chunks.chunk_topology import (
    get_chunk_prim_path, nearest_upstream_edit_layer, upstream_edit_target_layer,
)
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

# A list-group row has no name of its own (every row shares its template's), so it is
# named by index: @Node.param[1]@ is row 1 of the list holding param.
_EXPR_RE = re.compile(r'@(\w+)\.(\w+(?:\[\d+\])?)@')
_ROW_REF_RE = re.compile(r'(\w+)\[(\d+)\]')


_PATH_LIST_SPLIT = re.compile(r"([,\s]+)")


def apply_namespace(namespace: str, paths: str) -> str:
    """Put every absolute prim path in `paths` under `namespace`.

    A path already under it, or a relative one, is left alone, and so is the separators'
    spelling.

    Args:
        namespace: The prim path to put paths under; "" returns `paths` unchanged.
        paths: One path, or a comma/space-separated list, globs included.

    Returns:
        `paths` with each absolute path prefixed.
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
    """The inverse of apply_namespace: every path under `namespace` made relative to it again.

    That is the form a param inside a namespaced host stores.

    Args:
        namespace: The prim path to strip; "" returns `paths` unchanged.
        paths: One path, or a comma/space-separated list.

    Returns:
        `paths` with the namespace removed from each path under it.
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
    """The layer a chunk should author into for its exec-chain position.

    That is the layer published by the nearest Edit Target chunk reachable by walking the
    exec chain upstream from `owner`, or the session layer if there is no upstream Edit
    Target. A pure function of graph topology (see chunk_stage.nearest_upstream_edit_layer)
    -- resolution never depends on which chunk happened to execute last.

    Use this instead of hardcoding stage.GetSessionLayer() for override/redirect-style
    opinions (variant selections, shader input redirects, etc.) so they follow whatever
    Edit Target applies at the authoring chunk's place in the graph. These are cook output,
    so with no Edit Target upstream they stay in the session layer; a chunk's own prim
    falls back to the stage's edit target instead (get_bookkeeping_layer).

    The fallback for a missing `owner` is safe despite being ambient state because its sole
    writer (ChunkNoticePropagator.execute) save/restores it around each module run -- set to
    the running module's own chunk right before module.execute(), popped back to the prior
    value in the finally -- so it can never transiently point at the wrong chunk, even when a
    reconcile re-executes a chunk from inside another chunk's execute(). Anything running
    outside a module execute() (e.g. reconcile_edit_targets) passes owner explicitly rather
    than relying on this fallback.

    Args:
        stage: The stage being authored.
        owner: The authoring chunk. When None, falls back to the chunk whose execute() is
            currently running (chunk_notice._executing_chunk) -- so callers invoked from
            inside a chunk's execute() (author_in_variant_context, wire_shader_input, ...)
            resolve correctly without threading owner through every call site.

    Returns:
        The layer to author into; the session layer when there is no owner at all, and None
        when there is no stage.
    """
    if stage is None:
        return None
    if owner is None:
        owner = chunk_notice._executing_chunk
    if owner is None:
        return stage.GetSessionLayer()
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(owner)))
    return nearest_upstream_edit_layer(stage, prim)


def get_bookkeeping_layer(stage: Usd.Stage, owner) -> Sdf.Layer:
    """Where a chunk's own bookkeeping belongs: its node prim's storage attrs, wiring and position.

    In order:

    - a chunk composed in from a file the chunk system does not own: the stage's edit
      target (host_edit_layer) -- what is written on it is an override.
    - a regular chunk with an Edit Target chunk upstream: the layer that Edit Target
      publishes, so a network can route its own branches (render geo, proxy geo) in one go.
    - otherwise the layer the chunk's prim is already defined in, so a chunk never splits
      across layers, or for one not defined yet, the stage's edit target (host_edit_layer)
      -- the same place USD puts any other new prim, whatever the host defaults it to
      (usdview: the session layer; Blender, Flet, ImGui: the root). With the target on a
      department sublayer, a new chunk is saved in that department's file.

    CONTEXT chunks (Tab containers, groups) take the last rule: they are structural
    scaffolding, so an Edit Target chunk inside never moves them, but they live in a file
    like the chunks they hold.

    Args:
        stage: The stage being authored.
        owner: The chunk; None falls back to the chunk whose execute() is running
            (chunk_notice._executing_chunk), and is otherwise treated as a new chunk.

    Returns:
        The layer to author bookkeeping into, or None without a stage.
    """
    if stage is None:
        return None
    if owner is None:
        owner = chunk_notice._executing_chunk
    if owner is None:
        return host_edit_layer(stage)
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(owner)))
    if prim and prim.IsValid():
        # A chunk composed in from a sequence file has an Edit Target chain of its own,
        # and it is the sequence's: what the shot writes on such a chunk is an override,
        # and it goes where the host's edit target points (host_edit_layer), not down the
        # sequence's chain.
        if is_composed_in(prim):
            return host_edit_layer(stage)
        if not owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            routed = upstream_edit_target_layer(stage, prim)
            if routed is not None:
                return routed
        if (home := chunk_def_layer(prim)) is not None:
            return home
    return host_edit_layer(stage)


def chunk_def_layer(prim: Usd.Prim) -> Sdf.Layer | None:
    """The strongest owned layer holding a `def` for this chunk prim.

    That is where the chunk lives, and where anything first authored on it later goes, so
    one chunk's specs stay in one layer however the stage's edit target moves afterwards.

    Args:
        prim: The chunk prim.

    Returns:
        The layer, or None when no owned layer defines the prim (it is composed in, or not
        defined yet).
    """
    path = prim.GetPath()
    stage = prim.GetStage()
    for spec in prim.GetPrimStack():
        if spec.path != path or spec.specifier != Sdf.SpecifierDef:
            continue
        # A def in a file the chunk system does not own is only ever overridden, never
        # written into (is_foreign_layer), and an internal layer is cook output.
        if is_foreign_layer(spec.layer, stage) or chunk_registry.is_chunk_internal_layer(spec.layer):
            continue
        return spec.layer
    return None


def _index_nodes(node) -> Iterable:
    """node and everything below it in a prim index, strongest first."""
    yield node
    for child in node.children:
        yield from _index_nodes(child)


def _def_node(prim: Usd.Prim):
    """The composition node a prim's `def` comes from: the weakest one holding a def spec,
    which is where the prim was introduced. None for a prim no layer defines."""
    found = None
    for node in _index_nodes(prim.GetPrimIndex().rootNode):
        if not node.hasSpecs:
            continue
        for layer in node.layerStack.layers:
            spec = layer.GetPrimAtPath(node.path)
            if spec and spec.specifier == Sdf.SpecifierDef:
                found = node
                break
    return found


def arc_def_node(prim: Usd.Prim):
    """The prim-index node an arc brings prim's `def` in by, or None for a prim defined in
    the stage's own layer stack (or not at all).

    Its layerStack is the asset's layers, and its mapToRoot the arc's map from the asset's
    namespace to the stage's -- what a cook needs to land in the asset (ChunkLayerManager
    .settle_cook), the same node arc_source reads where the chunk came from.
    """
    node = _def_node(prim)
    if node is None or node.arcType == Pcp.ArcTypeRoot:
        return None
    return node


def arc_source(prim: Usd.Prim) -> tuple[Sdf.Path, Sdf.Layer] | None:
    """Where a prim came into the stage by an arc, read from USD's own prim index.

    Any arc -- reference, payload, inherit, specialize, variant, relocate -- because it asks
    composition which node the prim's `def` comes from, rather than looking for a list of
    metadata fields.

    Args:
        prim: A prim.

    Returns:
        (the nearest prim an arc is introduced at, as a stage path; the layer of the stage's
        own stack that brings the outermost arc in -- the weakest one with a spec at that
        arc's prim, where it was introduced), or None for a prim defined in the stack itself.
        A chair a referenced house references comes back as (house's furniture prim, the
        shot layer that references the house).
    """
    node = _def_node(prim)
    if node is None or node.arcType == Pcp.ArcTypeRoot:
        return None
    nearest = node.parent.mapToRoot.MapSourceToTarget(node.GetIntroPath())
    outer = node
    while outer.parent and outer.parent.arcType != Pcp.ArcTypeRoot:
        outer = outer.parent
    outer_path = outer.parent.mapToRoot.MapSourceToTarget(outer.GetIntroPath())
    stack = prim.GetPrimIndex().rootNode.layerStack.layers
    authoring = None
    for layer in stack:  # strongest first: the last one found is the weakest
        if layer.GetPrimAtPath(outer_path) and not chunk_registry.is_chunk_internal_layer(layer):
            authoring = layer
    if authoring is None or nearest.isEmpty:
        return None
    return nearest, authoring


def composition_sources(prim: Usd.Prim) -> tuple:
    """A fingerprint of where a prim's opinions come from: its prim index's nodes.

    Changes when what composes it changes -- a variant switched, an arc retargeted, a
    payload loaded -- and stays the same when only values change. Compared before and after
    a resync, it says whose params need reading again, with no list of what could cause it.
    """
    if not (prim and prim.IsValid()):
        return ()
    return tuple(
        (str(node.arcType), node.layerStack.identifier.rootLayer.identifier, str(node.path))
        for node in _index_nodes(prim.GetPrimIndex().rootNode) if node.hasSpecs
    )


def chunk_pass(prim: Usd.Prim) -> Sdf.Layer | None:
    """The layer whose turn a chunk cooks in: the weakest layer of the stack that `def`s it.

    A shot sublayers its departments, and the stack is the cook order: the weakest layer's
    chunks cook first, and every stronger layer cooks on their result. The weakest `def` is
    where the chunk was introduced; a `def` in a stronger layer is an override like any
    other opinion, and so is an `over`, which never moves a chunk to another turn.

    Unlike chunk_def_layer, a layer the chunk system does not own still counts: that is
    exactly a department file sublayered into a shot.

    Args:
        prim: The chunk prim.

    Returns:
        The layer, or None when no layer of the stage's layer stack defines the prim (it
        came in through a reference, payload or inherit).
    """
    stage = prim.GetStage()
    stack = {layer.identifier for layer in stage.GetLayerStack(includeSessionLayers=True)}
    path = prim.GetPath()
    found = None
    for spec in prim.GetPrimStack():  # strongest first
        if spec.path != path or spec.specifier != Sdf.SpecifierDef:
            continue
        if spec.layer.identifier not in stack or chunk_registry.is_chunk_internal_layer(spec.layer):
            continue
        found = spec.layer
    return found


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

    Args:
        stage: The stage being authored.

    Returns:
        The stage's edit target layer, or its root layer when the target is internal.
    """
    layer = stage.GetEditTarget().GetLayer()
    if layer is None or chunk_registry.is_chunk_internal_layer(layer):
        return stage.GetRootLayer()
    return layer


def is_composed_in(prim: Usd.Prim) -> bool:
    """Whether this prim is defined only by layers the chunk system does not own.

    Every `def` for it is in a foreign layer (is_foreign_layer) or reached across a
    reference or payload, so any local opinion on it is an override. A prim that is not
    defined at all yet (mid-creation) is not composed in; neither is one with a `def` in an
    owned layer, however many foreign opinions sit beneath it.

    Args:
        prim: The prim to check.

    Returns:
        True if every `def` for the prim is foreign.
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
    """The layer a composed-in prim's strongest `def` comes from.

    The layer a UI names when it says where a locked node came from, and the one
    is_composed_in is really asking about.

    Args:
        prim: The prim to check.

    Returns:
        The layer, or None when the prim is defined locally (or not at all).
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
    session layer, graph and per-chunk procedural layers, Layer's layers), and the
    root layer is the file the user opened to edit, so those two tests are the whole rule.

    What "must not rewrite" buys: a shot that composes a sequence tweaks it with sparse
    `over`s in the host's edit target (host_edit_layer) and never touches the sequence
    file, the same discipline USD has for every other kind of content. A user who wants
    to edit a foreign file directly says so by making it the stage's edit target; the
    override then lands there of its own accord, and this predicate does not need to know.

    Args:
        layer: The layer to check.
        stage: The stage it is on.

    Returns:
        True for a file-backed layer other than the root layer.
    """
    return not layer.anonymous and layer != stage.GetRootLayer()


def chunk_inputs_hash(prim: Usd.Prim) -> str:
    """A digest of what a chunk prim's inputs say: every `inputs:` value and connection,
    composed. Two cooks of one module with the same digest were handed the same params.

    For a bake's stamp (ChunkLayerManager.save_cook_layers): an override in a stronger
    layer, or an edit never saved, changes it, and the bake made before no longer matches.
    The exec wire is left out -- what feeds a chunk is not one of its params.

    Args:
        prim: The chunk prim.

    Returns:
        A hex digest, or "" for an invalid prim.
    """
    if not (prim and prim.IsValid()):
        return ""
    digest = hashlib.sha256()
    for attr in sorted(prim.GetAttributes(), key=lambda a: a.GetName()):
        name = attr.GetName()
        if (not name.startswith("inputs:") or name == chunk_naming.EXEC_INPUT
                or chunk_naming.EXEC_INPUT_NUMBERED.match(name)):
            continue
        digest.update(f"{name}={attr.Get()!r};{attr.GetConnections()!r}\n".encode())
    return digest.hexdigest()


def stamp_module_hash(stage: Usd.Stage, chunk) -> None:
    """Record on the chunk's prim which module source it just cooked with.

    Called by the cook, not by a save: a chunk that loaded without re-running has output
    made by the code the file was saved with, and stamping it at save time would relabel that
    output with whatever code is loaded now. Written where the rest of the chunk's
    bookkeeping lives (get_bookkeeping_layer) -- never the cook layer, which is cleared on
    the next cook -- and updated in place when it already exists, like any other
    bookkeeping attribute. Skipped when the value is already right, so a re-cook of
    unchanged code writes nothing.

    Args:
        stage: The stage.
        chunk: The chunk that cooked; nothing is written for one whose module has no source
            hash (a module built without a file).
    """
    module = chunk.get_chunk_module()
    digest = module.source_hash if module is not None else None
    if not digest:
        return
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not prim.IsValid():
        return
    attr = prim.GetAttribute(chunk_naming.MODULE_HASH_ATTR)
    if attr and attr.IsValid() and attr.Get() == digest:
        return
    home = get_attr_home_layer(attr) if attr and attr.IsValid() else None
    target = home or get_bookkeeping_layer(stage, chunk)
    with Usd.EditContext(stage, target):
        attr = prim.CreateAttribute(
            chunk_naming.MODULE_HASH_ATTR, Sdf.ValueTypeNames.String, custom=False)
        attr.SetVariability(Sdf.VariabilityUniform)
        attr.Set(digest)
    logger.debug("%s cooked with %s %s", prim.GetPath(), module.module_name, digest)


def recorded_module_hash(prim: Usd.Prim) -> str | None:
    """The module hash a chunk prim was saved with (MODULE_HASH_ATTR), or None if it has none."""
    attr = prim.GetAttribute(chunk_naming.MODULE_HASH_ATTR)
    return (attr.Get() or None) if attr and attr.IsValid() else None


def module_changed_since_cook(prim: Usd.Prim, module) -> bool:
    """Whether a chunk prim's recorded module hash differs from the module now registered.

    True means the result this prim was saved with came from other code than the code that
    would run it now. No opinion is a no, not a yes: a prim that never cooked, one from a file
    older than the attribute, and a module with no source hash all have nothing to compare.

    Args:
        prim: The chunk prim.
        module: The registered ChunkModule its info:chunkType names.

    Returns:
        True if both hashes exist and differ.
    """
    saved = recorded_module_hash(prim)
    return bool(saved and module.source_hash and saved != module.source_hash)


def is_layer_locked(layer: Sdf.Layer | None) -> bool:
    """Whether an edit may not go into layer: its edit permission is off, or its file is
    read-only on disk -- the two ways a pipeline locks a published file.

    Cooking never needs this: a cook only reads such a layer. It is what a param edit that
    would land there is refused by.
    """
    if layer is None:
        return False
    if not layer.permissionToEdit:
        return True
    if layer.anonymous:
        return False
    path = layer.realPath
    return bool(path) and os.path.exists(path) and not os.access(path, os.W_OK)


def param_storage_layer(stage: Usd.Stage, attr: Usd.Attribute, chunk) -> Sdf.Layer | None:
    """Where a write of a param's storage attribute goes: wherever its spec already lives,
    or for a new one, where this chunk's bookkeeping goes (get_bookkeeping_layer)."""
    return get_attr_home_layer(attr) or get_bookkeeping_layer(stage, chunk)


def get_attr_home_layer(attr: Usd.Attribute) -> Sdf.Layer | None:
    """The layer to update an existing attribute in.

    That is the layer holding its strongest existing property spec, or -- when that spec is
    composed in from somewhere the chunk system does not write -- the host's edit target,
    where the override belongs.

    Use this to update an attribute that already exists (value, connections, a rename),
    rather than re-resolving "the current Edit Target" on every change -- a chunk's own
    bookkeeping attr lives in whatever layer was active when it was first authored
    (get_bookkeeping_layer), and Edit Target may have moved on to somewhere else by the
    time it's edited again. Always writing new edits back to the attr's own existing home
    avoids splitting one attribute's opinions across multiple layers.

    Two kinds of strongest spec are not a home, and answer host_edit_layer instead:

    - one in a foreign layer (is_foreign_layer): a sublayered sequence file. Writing
      there would edit the sequence from the shot.
    - one whose path is not the attr's stage path: reached through a reference or
      payload, so the spec's layer is not even in the local layer stack and
      Usd.EditContext on it raises "not in the local LayerStack".

    And on a chunk prim, a chunk's procedural layer is passed over altogether: what one
    holds there is another chunk's cook output (an Override Chunk Prim's `over`), cleared
    and rewritten on its next cook. An edit written into it would show until then and
    vanish; written beneath it, it is kept and shows once the override goes.

    Args:
        attr: The attribute to update.

    Returns:
        The layer, or None only when the attr has no spec anywhere yet.
    """
    stack = attr.GetPropertyStack(Usd.TimeCode.Default())
    if stack and attr.GetPrim().GetTypeName() == chunk_naming.CHUNK_PRIM_TYPENAME:
        stack = [s for s in stack if not chunk_registry.is_chunk_internal_layer(s.layer)]
    if not stack:
        return None
    spec = stack[0]
    stage = attr.GetStage()
    if spec.path != attr.GetPath() or is_foreign_layer(spec.layer, stage):
        return host_edit_layer(stage)
    return spec.layer


def user_layers(stage: Usd.Stage) -> list[tuple[str, Sdf.Layer]]:
    """The layers a user picks or exports on stage, by name, strongest first.

    Asks the host's scene (SceneSender.user_layers): in usdview that is the whole layer
    stack, in Houdini only the layers a Layer chunk named, and files.

    Args:
        stage: The stage.

    Returns:
        (name, layer) pairs, session layer included.
    """
    hook = getattr(chunk_registry.ChunkRegistry().scene, "user_layers", None)  # a bare test scene has none
    if hook is not None:
        return hook(stage)
    return [(layer.GetDisplayName() or layer.identifier, layer)
            for layer in stage.GetLayerStack(includeSessionLayers=True)]


def export_layer(layer: Sdf.Layer) -> Sdf.Layer:
    """The layer to write to a file for layer, as the host's scene has it (SceneSender.export_layer).

    Args:
        layer: The layer about to be written.

    Returns:
        layer, or a copy without the host's own bookkeeping.
    """
    hook = getattr(chunk_registry.ChunkRegistry().scene, "export_layer", None)
    return hook(layer) if hook is not None else layer


def chunk_internal_layer_ids() -> set:
    """Identifiers of layers owned by the chunk graph itself.

    That is every per-graph and per-chunk procedural layer, wherever it is currently
    sublayered (the session layer by default, or an Edit-Target-chosen real layer if one is
    active). These are implementation details managed and Clear()ed by the chunk execution
    system; utility chunks that let a user pick a layer (e.g. edit-target or export pickers)
    should exclude them.

    Reads ChunkLayerManager's own incrementally-maintained internal_layer_ids set (same
    source of truth as chunk_registry.is_chunk_internal_layer) instead of deriving it by
    walking every tracked layer's live subLayerPaths. This is called per stage edit, once per
    live Layer/Edit Target/Export USD chunk (see those modules' listen_globally
    refresh handlers), so a full tree-walk here would make chunk deletion cost scale with
    the total number of live chunks, not just the ones being deleted.

    Returns:
        The layer identifiers, empty before the layer manager is initialized.
    """
    lm = chunk_registry.ChunkLayerManager()
    if not lm.initialized:
        return set()
    return set(lm.internal_layer_ids)


def find_collection(stage: Usd.Stage, name: str):
    """Find a UsdCollectionAPI named `name` anywhere on the stage, or None.

    Collections are looked up by name across the whole stage (no namespacing), matching
    Houdini's %collectionName resolution in primpattern fields.

    Args:
        stage: The stage to search.
        name: The collection name.

    Returns:
        The first collection with that name, or None.
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


def split_patterns(text: str) -> list[str]:
    """Split a field of paths or glob patterns on whitespace and commas, dropping empties."""
    return [token for token in re.split(r"[\s,]+", text.strip()) if token]


def glob_to_regex(pattern: str) -> re.Pattern:
    """Compile a path glob to a regex that matches the whole path.

    - ``*`` matches any characters within one path component (no slash crossing)
    - ``**`` matches any characters across any number of path components
    - ``?`` matches one character within one path component

    Shared by prim paths (resolve_prim_paths) and layer paths (the Mute Layer chunk), so
    a pattern means the same in every field that takes one.

    Args:
        pattern: The glob.

    Returns:
        The compiled regex. Use .match: it is anchored at the end, and match anchors the start.
    """
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


def _glob_root(pattern: str) -> str:
    """The deepest path a glob's matches all lie under: its components before the first glob.

    /World/objects/cube_* -> /World/objects; mesh* -> /.
    """
    fixed = []
    for part in pattern.split("/")[1:]:
        if "*" in part or "?" in part:
            break
        fixed.append(part)
    return "/" + "/".join(fixed)


def resolve_prim_paths(stage, location_str):
    """Split location_str on whitespace/commas and expand glob patterns and collection refs.

    Globs follow glob_to_regex. For example:

    - ``/geo/*`` -- direct children of /geo only
    - ``/geo/**`` -- everything recursively under /geo
    - ``/**`` -- every prim on the stage
    - ``mesh*`` -- root-level prims whose name starts with "mesh" (/ prepended)
    - ``%name`` -- every prim in the UsdCollectionAPI named "name" (see find_collection),
      however it was authored (explicit includes/excludes, expansion rule, or a membership
      expression) -- resolved via the collection's own query.

    A glob may follow a variant selection: /asset{color=red}/**/*Body* matches under
    /asset with "red" selected, and every match comes back carrying the selection --
    /asset{color=red}/geo/Body_L -- so whatever authors to it lands inside that variant
    (author_in_variant_context). The pattern is matched with the variant selected, since
    a prim that only exists in "red" is not on the stage until "red" is; the selection
    is put back afterwards. A glob cannot precede a selection: /a*{x=1}/b has no anchor.

    Args:
        stage: The stage to resolve against.
        location_str: The paths, patterns and collection refs.

    Returns:
        The resolved prim paths.
    """
    paths = []
    for token in split_patterns(location_str):
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
        rx = glob_to_regex(pattern)
        matched = []
        with author_in_variant_context(stage, token) if selections else nullcontext():
            # Walked from the pattern's fixed part, not from /: every match is under it, so
            # the rest of the stage is neither read nor paid for -- and a chunk whose target
            # is /World/objects/cube_* reads /World/objects, not the whole scene (skip_cook).
            root = stage.GetPrimAtPath(_glob_root(pattern))
            for prim in Usd.PrimRange(root) if root else ():
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


def split_row_ref(param_name: str) -> tuple[str, int | None]:
    """Split a param name from parse_expression into (name, row index or None).

    "item[1]" is row 1 of the list holding item; "item" is item itself.
    """
    if match := _ROW_REF_RE.fullmatch(param_name):
        return match.group(1), int(match.group(2))
    return param_name, None


def parse_expression_attrs(text: str) -> list[tuple[str, str]]:
    """parse_expression, naming the attribute a USD connection for each ref points at.

    A row has no attribute of its own, so a wire from one names its template's: the
    connection records which node feeds which, and the expression says which row.
    """
    return [(node, split_row_ref(name)[0]) for node, name in _EXPR_RE.findall(text)]


def renumber_row_refs(text: str, node_name: str, names: Iterable[str],
                      removed: Iterable[int]) -> tuple[str, bool]:
    """Rewrite @node.name[i]@ refs for rows that were just removed from a list.

    Rows are named by position, so removing one moves every row after it down. A ref to a
    later row follows it to its new index; a ref to a removed row is left as it was.

    Args:
        text: The expression text.
        node_name: The chunk holding the list.
        names: The list's template param names.
        removed: The indices the rows had before they were removed.

    Returns:
        The rewritten text, and whether it names a removed row.
    """
    names, removed = set(names), sorted(set(removed))
    hit = False

    def renumber(match: re.Match) -> str:
        nonlocal hit
        name, index = split_row_ref(match.group(2))
        if match.group(1) != node_name or index is None or name not in names:
            return match.group(0)
        if index in removed:
            hit = True
            return match.group(0)
        return f"@{node_name}.{name}[{index - bisect.bisect_left(removed, index)}]@"

    return _EXPR_RE.sub(renumber, text), hit


def clear_connections_everywhere(stage: Usd.Stage, attr: Usd.Attribute) -> None:
    """Clear attr's connections in every layer that has one, not just the edit target.

    A connection may have been authored under a different Edit Target; clearing only the
    current one would silently leave the original composed in.
    """
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        spec = layer.GetAttributeAtPath(attr.GetPath())
        if spec and spec.connectionPathList.explicitItems:
            with Usd.EditContext(stage, layer):
                attr.ClearConnections()


def has_expression(text: str) -> bool:
    """Whether `text` contains a @Node.param@ reference."""
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


#: The attribute type a param value of each Python type is stored as.
SDF_TYPE_MAP = {
    bool: Sdf.ValueTypeNames.Bool,
    int: Sdf.ValueTypeNames.Int,
    float: Sdf.ValueTypeNames.Float,
    str: Sdf.ValueTypeNames.String,
    Gf.Matrix4d: Sdf.ValueTypeNames.Matrix4d,
}


def retype_attr(attr: Usd.Attribute, type_name: Sdf.ValueTypeName) -> None:
    """Change attr's type in every layer that has a spec for it, dropping the old value.

    Its connections stay: they name where a value comes from, whatever its type.

    Args:
        attr: The attribute.
        type_name: Its new type.
    """
    for spec in attr.GetPropertyStack():
        if spec.typeName != type_name:
            spec.SetInfo("typeName", str(type_name))
            spec.ClearDefaultValue()


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

    Args:
        attr: The attribute the expression drives.
        raw: The expression text.
    """
    if attr.GetTypeName() == Sdf.ValueTypeNames.String:
        attr.Set(raw)
    else:
        attr.SetCustomDataByKey(EXPR_TEXT_KEY, raw)


def read_expression_text(attr: Usd.Attribute) -> str | None:
    """The expression text authored on attr, from whichever place it was kept.

    Accepts either form. A @Node.param@ wire always comes with connections on the attr;
    a ${VAR} stage expression never does, since there is no other param to point at. So
    the two never arrive together and one reader serves both call sites.

    Args:
        attr: The attribute to read.

    Returns:
        The expression text, or None if the attr holds none.
    """
    if attr.GetTypeName() == Sdf.ValueTypeNames.String:
        val = attr.Get(0)
    else:
        val = attr.GetCustomDataByKey(EXPR_TEXT_KEY)
    return val if isinstance(val, str) and is_expression_text(val) else None


def is_expression_text(value) -> bool:
    """Whether value is a @Node.param@ wire, whose text must be kept verbatim.

    Such text is preserved as written rather than stored as the typed value it resolves to.
    A ${VAR} does not need this and does not count: it only ever sits in an asset path,
    which is a string param, so its text IS its stored value -- and usd_chunks never
    resolves it in the first place. USD does, during composition.

    Args:
        value: A param value.

    Returns:
        True for a string holding a @Node.param@ reference.
    """
    return isinstance(value, str) and has_expression(value)


def is_wire_locked(param) -> bool:
    """Whether a wire into param owns its value, so its widget must not take input.

    A dragged slider would otherwise replace the wire with a plain number, and it would
    win every time a hand landed on it. Text fields are the exception: their expression IS
    what the field shows and edits, so typing into one is how a path is built on a wire
    (@Box.primpath_location@/lid).

    Args:
        param: The param, or a list row.

    Returns:
        True if param holds a @Node.param@ wire and is not a text field.
    """
    try:
        raw, param_type = param.raw_value, param.get_type()
    except (AttributeError, ReferenceError):
        return False
    if not is_expression_text(raw):
        return False
    return not (ChunkFlags.ParamType.LINEEDIT in param_type or ChunkFlags.ParamType.FILE in param_type)


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

    Args:
        attr: The lofted param's attribute.
        label: The user's label.
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
# 2. Nothing here substitutes into other fields. A ${NAME} in a param that is not an asset
#    path stays literal (ChunkParamBase warns); a Stage Expression Switch is how a chunk
#    graph branches on a variable anywhere else.

#: A bare ${NAME} reference anywhere inside a string. USD proper only accepts a `${NAME}`
#: expression as the *whole* field, delimited by backticks; has_stage_expression spots
#: both forms, so a param can say when one will stay literal.
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


#: The spelling people reach for first, and it is backwards. Worth catching by name: it
#: looks close enough to right that the value simply arrives on disk with the braces still
#: in it, and a file called "{$SEQ}_{$SHOT}.png" reads as a broken renderer.
_REVERSED_VAR_RE = re.compile(r"\{\$(\w+)\}")


def resolve_output_path(text, stage: Usd.Stage, log=None):
    """Substitute ${NAME} into a path this toolkit is about to WRITE to.

    Deliberately not the same thing as a USD expression, and named apart from one. USD
    substitutes expression variables into asset paths and variant selections; a render
    product's `productName` is a `token`, and an output filename is not a composition input
    at all -- it is a file we open. So `${SHOT}` there resolves because usd_chunks resolves
    it, which is a mechanism of ours in one named place rather than a claim about OpenUSD.

    That is the whole reason it is worth having: an output path is the one string in a shot
    graph that has to differ per shot, and without this the only way to render ten shots is
    to retype the filename ten times.

    Both spellings work. A path in backticks (`"C:/out/${SHOT}.png"`, or an `if(...)`) is a
    real USD expression and is evaluated by Sdf.VariableExpression, so it reads the same as
    in an asset path. Anything else has bare ${NAME} references substituted into it.

    An unknown variable is left as written, so a missing SHOT shows up in the filename
    instead of silently collapsing every shot onto one image. For a backtick expression that
    means the whole path is left as typed: USD evaluates an unset variable to its own name
    without reporting an error, which would turn `"${TAKE}.png"` into "TAKE.png".

    Args:
        text (str): The path as typed
        stage (Usd.Stage): The stage whose variables apply
        log (logging.Logger): Where to report a reversed ${} spelling or an expression left
            unresolved; module logger if None

    Returns:
        str: The path with its variables substituted
    """
    if not isinstance(text, str) or not text:
        return text
    if match := _REVERSED_VAR_RE.search(text):
        (log or logger).warning(
            "Output path %r: %s is written the other way round -- ${%s}, not {$%s}. "
            "Left as it is, so the braces end up in the filename.",
            text, match.group(0), match.group(1), match.group(1),
        )
    variables = stage_expression_variables(stage)

    if Sdf.VariableExpression.IsExpression(text):
        # A backslash is an escape inside a USD string literal, so a Windows path typed the
        # usual way evaluates without error to "C:out${SHOT}.png" -- separators gone and
        # \$ escaping the variable. Only \\, \" and \` are meant as escapes in a path.
        if re.search(r'\\[^\\"`]', text.replace("\\\\", "")):
            (log or logger).warning(
                "Output path %r left as typed: a backslash is an escape inside a USD "
                "expression. Use / or \\\\ as the separator.", text,
            )
            return text
        result = Sdf.VariableExpression(text).Evaluate(variables)
        missing = sorted(set(result.usedVariables) - set(variables))
        if result.errors or missing or not isinstance(result.value, str):
            problem = "; ".join(result.errors) or (
                "no value for " + ", ".join(missing) if missing
                else f"it evaluates to {result.value!r}, not a path"
            )
            (log or logger).warning("Output path %r left as typed: %s", text, problem)
            return text
        return result.value

    return _STAGE_VAR_RE.sub(lambda m: str(variables.get(m.group(1), m.group(0))), text)


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
    dependency tracking, not of the layer chosen here).

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


def get_unique_path(stage, prim_path, own_layer=None):
    """prim_path if it is unused on the stage, else a unique variant with a numeric suffix.

    A pure lookup. A chunk's own prim from its last cook must not count against it, or the
    suffix walks up on every cook. In a procedural chunk's layer it is already gone -- the
    framework clears that layer before execute() -- but a layer the framework leaves alone
    (a context's file layer) still holds it, which is what own_layer is for.

    Args:
        stage: The stage to check.
        prim_path: A path, or a ChunkParamBase holding one. If the param's raw value is an
            expression the resolved path is returned as-is -- expressions define an explicit
            derived path, so uniqueness bumping would be wrong.
        own_layer: The layer the asking chunk authors into, if any. A prim that only this
            layer defines is the chunk's own, not a collision. With it, only a `def` that
            lands in the same real layer counts as taken: another chunk behind the same Edit
            Target, or that layer itself. The same path defined in another file (a
            department's publish) or behind another Edit Target is another opinion on the
            prim, the way USD layering works, and keeps its name.

    Returns:
        A prim path free on the stage.
    """
    raw_value = getattr(prim_path, 'raw_value', None)
    if raw_value is not None:
        if isinstance(raw_value, str) and has_expression(raw_value):
            resolved = getattr(prim_path, 'value', None)
            return str(resolved) if resolved is not None else ""

        prim_path = getattr(prim_path, 'value', prim_path)

    if isinstance(prim_path, str) and has_expression(prim_path):
        return prim_path

    if not stage:
        return prim_path

    layers = chunk_registry.ChunkLayerManager()
    # Where the asking chunk's content really lands: its Edit Target's layer (or the session
    # layer), or own_layer itself for a chunk with no layer of its own.
    own_target = (layers.target_of_layer(own_layer) or own_layer) if own_layer is not None else None

    def _is_taken(path):
        prim = stage.GetPrimAtPath(path)
        if not (prim.IsValid() and prim.GetTypeName()):
            return False
        if own_layer is None:
            return True
        return any(
            spec.layer != own_layer
            and spec.specifier == Sdf.SpecifierDef
            and (layers.target_of_layer(spec.layer) or spec.layer) == own_target
            for spec in prim.GetPrimStack()
        )

    if not _is_taken(prim_path):
        return prim_path

    m = re.search(r'\d+$', prim_path)
    base = prim_path[: m.start()] if m else prim_path
    count = int(m.group()) + 1 if m else 1
    while _is_taken(f"{base}{count}"):
        count += 1

    return f"{base}{count}"


def split_variant_path(path_str: str) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Split a path with variant selections into its anchor, target and selections.

    For example::

        /myprim{LOD=high}/geom
            -> ('/myprim', '/myprim/geom', [('/myprim', 'LOD', 'high')])

        /myprim{LOD=high}/geom/asset{season=summer}
            -> ('/myprim', '/myprim/geom/asset',
                [('/myprim', 'LOD', 'high'), ('/myprim/geom/asset', 'season', 'summer')])

        /myprim
            -> ('/myprim', '/myprim', [])

    Args:
        path_str: A prim path, optionally with {set=variant} selections at any level.

    Returns:
        (anchor_path, target_path, selections): the first prim before any {} (the outermost
        anchor); the full prim path with all variant selections stripped; and one
        (anchor prim, variant set, variant) tuple per selection.
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


def author_in_variant_contexts(stage, param, unique=False):
    """Every prim path resolved from `param`, each yielded inside its variant edit context.

    The one way a chunk authors to the prims its path param names, whether it creates one
    prim or modifies many::

        for path in author_in_variant_contexts(stage, primpath_param, unique=True):
            stage.DefinePrim(path, "Camera")

    A value of /asset{color=red}/geo yields /asset/geo with the "red" variant's edit
    context open, so everything the loop body authors under /asset lands inside that
    variant. Authoring elsewhere passes through unchanged. A path with no selections
    yields as-is, in the ordinary edit target.

    Author inside the loop body, never after it. The variant context is open only while
    the generator is suspended at its yield: `list(author_in_variant_contexts(...))` walks
    every path and leaves every context, so anything authored to those paths afterwards
    lands outside the variant. Reading a prim after the loop is fine; binding, defining or
    setting on it is not -- that would put "/asset{color=red}/geo" bindings on /asset/geo
    for every colour. A caller that only needs the paths, not the authoring scope, uses
    param.resolve_paths() with split_variant_path instead.

    Args:
        stage: The stage to author on.
        param: A LineEdit-like param with .resolve_paths().
        unique: Claim a free path first, for a chunk that creates its prim. Applies only to
            a single literal path with no variant selections, outside a load: the value is
            bumped through get_unique_path (which also clears the chunk's anonymous layer,
            so its own previous prim does not count as taken) and written back to the
            param, since the value is what tells two chunks of the same kind apart in a
            saved scene. A variant path, list, glob, collection or expression is left as it
            is, and so is every value while a load restores the path it saved.

    Yields:
        Each target path, with its variant selections stripped.
    """
    if unique:
        _claim_unique_path(stage, param)
    for path_str in param.resolve_paths():
        with author_in_variant_context(stage, path_str) as target_path:
            yield target_path


def target_paths(param):
    """Every prim path resolved from `param`, variant selections stripped, for reading.

    The read-only counterpart of author_in_variant_contexts: no edit context is entered, so
    nothing authored with these paths lands inside a variant. For a chunk that only looks
    at its targets -- a pivot, a relationship's target list, the surface being painted.

    Args:
        param: A LineEdit-like param with .resolve_paths().

    Returns:
        The resolved paths, each with its {set=variant} selections removed, in order and
        without duplicates -- two variants of one prim strip to the same path, and a
        relationship should not target it twice.
    """
    return list(dict.fromkeys(split_variant_path(path_str)[1] for path_str in param.resolve_paths()))


def _claim_unique_path(stage, param):
    """Bump a create chunk's single literal path to a free one and record it on the param."""
    value = (param.value or "").strip()
    if not value or chunk_notice._loading_stage:
        return
    if re.search(r"[\s,*?%{]", value):
        return
    # The chunk's own layer, not the stage's edit target: outside a cook that is the user's
    # file, and everything in it would pass for this chunk's own. A chunk with no layer of its
    # own (interactive) authors into the edit target, but only during its own cook is that
    # where its last result is -- and without counting it, every cook would move it one along.
    owner = getattr(param, "get_chunk", lambda: None)()
    own_layer = owner.get_edit_context() if owner is not None else None
    if own_layer is None and owner is not None and chunk_notice._executing_chunk is owner:
        own_layer = stage.GetEditTarget().GetLayer()
    unique_path = get_unique_path(stage, param, own_layer=own_layer)
    if unique_path and unique_path != param.value:
        param.value = unique_path


def set_payloads_loaded(node, stage: Usd.Stage, paths, load: bool) -> None:
    """Unload the payloads at and under `paths`, or load back the ones `node` unloaded.

    For a chunk's Load checkbox. Load state is the stage's, not something a layer holds, so
    the chunk applies it each time it cooks. Loading only puts back what this chunk
    unloaded: a checked box never loads a payload someone unloaded by hand.

    Args:
        node: The chunk, which remembers what it unloaded.
        stage: The scene's stage (not a cook stage).
        paths: Prim paths.
        load: The checkbox.
    """
    held: set = getattr(node, "_unloaded_by_me", set())
    if load:
        if held:
            stage.LoadAndUnload({Sdf.Path(p) for p in held}, set())
        node._unloaded_by_me = set()
        return
    wanted = {str(p) for p in paths}
    unload = {Sdf.Path(p) for p in wanted - held}
    reload = {Sdf.Path(p) for p in held - wanted}
    if unload or reload:
        stage.LoadAndUnload(reload, unload)
    node._unloaded_by_me = wanted


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

    Args:
        stage: The stage to author on.
        path: The prim to define.
        type_name: The type for the prim at `path`.
        ancestor_type: The type for ancestors that have none.

    Returns:
        The prim at `path`.
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
    """Enter every variant edit context encoded in path_str, at any level of the hierarchy.

    For example::

        with author_in_variant_context(stage, '/prim{LOD=high}/geom/asset{season=summer}') as t:
            stage.DefinePrim(t, 'Xform')  # authored inside both variant contexts

    Args:
        stage: The stage to author on.
        path_str: A prim path, optionally with {set=variant} selections.

    Yields:
        The target path with its variant selections stripped, inside the edit contexts.
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
def author_in_variant_selections(stage, var_selections, layer=None):
    """Enter already-parsed variant selections; the lower half of author_in_variant_context.

    For a caller that holds a parsed selections list rather than a path string to parse.
    A param bound to a real prim's attribute while a variant was selected (show_attribute,
    called from inside author_in_variant_context) is the case that needs this directly: it
    remembers the selections active when it was built, and re-enters them here on every
    later edit. get_attr_home_layer cannot stand in for this -- a variant's content is
    keyed by a different internal Sdf path, invisible to a plain layer-stack walk for the
    attribute's composed path, so it never finds a spec there and the edit falls through to
    the session layer, landing outside the variant entirely.

    Re-entrant: a scope already open (_ACTIVE_VARIANT_SCOPES) is left alone rather than
    entered twice, which would nest the variant inside itself and send the write nowhere.

    Args:
        stage: The stage to author on.
        var_selections: (anchor_prim_path, variant set, variant) tuples, as
            split_variant_path returns them.
        layer: The layer the variant's content is authored into -- the chunk's own layer
            for a chunk's edit, since a chunk's edits and results always go there. Used
            only when no variant scope is open on this stage yet; once one is, the new
            scopes compose on it, which is already built on the right layer. None builds
            on whichever edit target is active, which inside a cook is the chunk's layer
            already -- it is a UI edit, made outside any cook, that needs it passed, or
            the write lands in the stage's edit target (the user's file) instead.

    Yields:
        None, once every selection's edit context is entered.
    """
    edit_layer = get_edit_layer(stage)
    # Only the outermost scope is built on `layer`: one already open means an enclosing
    # author_in_variant_* call chose its layer, and resetting the target to a plain one
    # here would drop that enclosing variant.
    base_layer = layer if not any(key[0] == id(stage) for key in _ACTIVE_VARIANT_SCOPES) else None
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
            if base_layer is not None:
                stack.enter_context(Usd.EditContext(stage, vset.GetVariantEditTarget(base_layer)))
                base_layer = None
            else:
                stack.enter_context(vset.GetVariantEditContext())
        yield


def open_variant_selections(stage, prim_path):
    """The variant scopes open right now that `prim_path` is authored inside, outermost first.

    For code that records where a prim was authored so a later edit can go back there --
    show_attribute's generated params -- without being handed the path string it came from.
    Called inside author_in_variant_contexts' loop it returns exactly the selections that
    loop entered for this prim; outside any scope it returns [].

    Args:
        stage: The stage the scopes were opened on.
        prim_path: The prim, with its variant selections stripped.

    Returns:
        (anchor_prim_path, variant set, variant) tuples, as split_variant_path returns them.
    """
    path = Sdf.Path(str(prim_path))
    # Insertion order is entry order, and scopes are entered outermost first -- the order
    # author_in_variant_selections needs to re-enter them.
    return [
        (anchor, vs_name, variant)
        for (stage_id, anchor, vs_name), variant in _ACTIVE_VARIANT_SCOPES.items()
        if stage_id == id(stage) and path.HasPrefix(Sdf.Path(anchor))
    ]


def chunk_write_layer(stage, owner):
    """The chunk layer `owner`'s edits go into, or None when it has none or it is muted.

    A chunk's edits and results always go into its own layer, variant or not. None (USD
    refuses to target a muted layer) leaves the caller on the ambient edit target, as
    before there was a layer to pass.
    """
    layer = owner.get_edit_context() if owner is not None else None
    if layer is None or stage.IsLayerMuted(layer.identifier):
        return None
    return layer


def edit_context_for(stage, var_selections, fallback_layer=None):
    """The one edit context for authoring an attribute's value.

    That is fallback_layer -- or the ambient edit target if it is None -- and, if
    var_selections are given, inside the recorded variant of that same layer (re-entered
    via author_in_variant_selections).

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

    Args:
        stage: The stage to author on.
        var_selections: Recorded variant selections, or None.
        fallback_layer: The layer to author into, inside a variant or not, or None for the
            ambient edit target. For a chunk's edit this is the chunk's own layer: a chunk's
            edits and results always go there, variant or not.

    Returns:
        A context manager to author inside.
    """
    if var_selections:
        return author_in_variant_selections(stage, var_selections, fallback_layer)
    if fallback_layer is None:
        return nullcontext()
    return Usd.EditContext(stage, fallback_layer)


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

    Args:
        node: The chunk.

    Returns:
        The op name, or None.
    """
    suffix = getattr(node, "_xform_op_suffix", None)
    if suffix:
        return _XFORM_OP_PREFIX + suffix
    stage = node.get_scene_stage() if hasattr(node, "get_scene_stage") else None
    prim = _chunk_prim_of(stage, node) if stage is not None else None
    if prim is None:
        return None
    attr = prim.GetAttribute(XFORM_OP_ID_ATTR)
    if attr and attr.IsValid() and attr.Get():
        return _XFORM_OP_PREFIX + attr.Get()
    return None


def xform_op_name(node) -> str:
    """The xformOp name this chunk owns: readable, unique, and stable across renames.

    If every chunk authored the same `xformOp:transform`, two chunks placing one prim would
    compete for one slot and USD would discard the loser. Owning a distinctly named op each
    is what lets them compose instead.

    The name carries two things because it has two jobs. The id is what identity rests on:
    generated once, recorded on the chunk's prim, and never regenerated, so renaming a
    chunk does not orphan the op it already authored. The readable prefix is there because
    the point of all this is to select a prim, look at its ops, and see which chunk each one
    belongs to -- `xformOp:transform:a3f2c1` answers nothing at a glance::

        xformOp:transform:Transform1_a3f2c1

    The prefix is taken from the chunk's name at first use and deliberately not kept in
    sync afterwards. Renaming the op would be possible -- a chunk owns its attribute
    outright and nobody else writes it -- but it rewrites the file for a cosmetic gain, and
    anything that needs the true mapping should read XFORM_OP_ID_ATTR.

    Args:
        node: The chunk.

    Returns:
        The op name, minted on first use.
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

    stage = node.get_scene_stage() if hasattr(node, "get_scene_stage") else None
    prim = _chunk_prim_of(stage, node) if stage is not None else None

    suffix = None
    if prim is not None:
        attr = prim.GetAttribute(XFORM_OP_ID_ATTR)
        if attr and attr.IsValid():
            suffix = attr.Get()

    if not suffix:
        suffix = _mint_suffix(node, prim, stage)
        # A chunk composed in from a file this one does not own (a department's layout, a
        # referenced house) keeps it in memory only. Minting is a function of the prim path,
        # so every open arrives at the same id, and recording it would write an override
        # into the shot just for opening it.
        if prim is not None and composed_in_from(prim) is None:
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


def texture_file_is_usable(path: str) -> bool:
    """Whether a UsdUVTexture authored with *path* will actually have an image to load.

    A texture whose file cannot be resolved is not merely a missing image: the shader
    network still carries the whole UV chain (primvar reader -> UsdTransform2d -> the
    texture's `st`), and a render delegate that drops the unloadable texture node while
    still translating what fed it is left holding a connection to a node it never built.
    hdCycles fails exactly that way -- "Failed to load 1 image files" followed by
    "Could not find node '/Mapping' to connect" out of BuildShaderGraph (its Cycles
    `mapping` node being the translation of our UsdTransform2d) -- and takes usdview down
    with it on a delegate switch. So the chain is only worth authoring when the image is
    really there.

    A path carrying a `<UDIM>` (or any other `<token>`) is left alone: those never resolve
    as a plain file and are meant not to, so treating them as missing would break a
    perfectly valid texture set.
    """
    if not path:
        return False
    if "<" in path and ">" in path:
        return True  # UDIM / token pattern -- not resolvable as a single file by design
    return bool(Ar.GetResolver().Resolve(path))


def bind_uv_checker(stage, prims, texture_file: str, scale=(1.0, 1.0)) -> None:
    """Build /materials/UVCheckerMaterial showing texture_file, and bind it to prims.

    A UsdPreviewSurface fed by a repeating UsdUVTexture, read through `st` and scaled by a
    UsdTransform2d. When the image does not resolve, a plain grey surface is bound
    instead -- see texture_file_is_usable for why the texture chain is left out then.

    Args:
        stage: The stage to author on.
        prims: The prims to bind the material to.
        texture_file: The image to show.
        scale: The UV scale (x, y).
    """
    material_path = "/materials/UVCheckerMaterial"
    stage.DefinePrim("/materials", "Scope")
    material = UsdShade.Material.Define(stage, material_path)
    preview = UsdShade.Shader.Define(stage, f"{material_path}/previewSurface")
    preview.CreateIdAttr("UsdPreviewSurface")
    material.GetOutput("surface").ConnectToSource(
        preview.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    )
    diffuse = preview.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f)

    if not texture_file_is_usable(texture_file):
        logger.warning("texture file does not resolve, binding a plain surface: %s", texture_file)
        diffuse.Set((0.5, 0.5, 0.5))
    else:
        texture = UsdShade.Shader.Define(stage, f"{material_path}/texture")
        texture.CreateIdAttr("UsdUVTexture")
        texture.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
        texture.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
        texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(texture_file)
        diffuse.ConnectToSource(texture.CreateOutput("rgb", Sdf.ValueTypeNames.Color3f))

        transform2d = UsdShade.Shader.Define(stage, f"{material_path}/transform2d")
        transform2d.CreateIdAttr("UsdTransform2d")
        transform2d.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(tuple(scale))
        texture.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
            transform2d.CreateOutput("result", Sdf.ValueTypeNames.Float2)
        )

        reader = UsdShade.Shader.Define(stage, f"{material_path}/reader")
        reader.CreateIdAttr("UsdPrimvarReader_float2")
        reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")
        transform2d.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(
            reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
        )

    for prim in prims:
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, UsdShade.Tokens.strongerThanDescendants)


def cwd_relative(path):
    """Resolve a relative output path against the working directory.

    The same base USD uses for a RenderProduct's productName, which is the point: when
    the two disagree, a render writes its image in one place while the product it
    declared is looked for in another, and only one of them is where the person who ran
    the command was looking.

    This deliberately replaces resolving against the scene file. Next to the scene reads
    like the tidier answer, and for usdview it often is -- but it makes `usdchunks run
    scene.usda -o out.exr` write beside scene.usda rather than beside the shell, which is
    not what a command line means by a relative path anywhere else.
    """
    if not path or os.path.isabs(path):
        return path

    return os.path.normpath(os.path.join(os.getcwd(), path))


def parse_frames(spec: str) -> list[float]:
    """Frame numbers named by a usdrecord-style frame spec.

    The spelling the Render chunk's Frames param takes, so a chunk that authors or reads
    something per frame can be wired to the same text and agree with it frame for frame:
    comma-separated entries, each a single frame, a "start:end" range, or a
    "start:endxstride" strided range. An empty spec is reported as an empty list.

    Args:
        spec: The frame spec, e.g. "1:100", "1,5,9" or "0:20x5".

    Returns:
        The frames, in the order the spec names them.

    Raises:
        ValueError: A stride that is not positive, or a range that ends before it starts.
    """
    # A lone number is a one-frame spec. It arrives as a number rather than text when the
    # param is wired to an integer, a Wedge's Index being the case that matters -- and index
    # 0 would otherwise read as "no frames" by being falsy.
    if isinstance(spec, (int, float)):
        return [float(spec)]
    if not spec or not spec.strip():
        return []

    frames = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue

        stride = 1.0
        if "x" in entry:
            entry, _, stride_text = entry.partition("x")
            stride = float(stride_text)
        if stride <= 0:
            raise ValueError(f"frame stride must be positive: {spec!r}")

        if ":" not in entry:
            frames.append(float(entry))
            continue

        start_text, _, end_text = entry.partition(":")
        start, end = float(start_text), float(end_text)
        if end < start:
            raise ValueError(f"frame range ends before it starts: {entry!r}")

        current = start
        while current <= end + 1e-9:
            frames.append(current)
            current += stride

    return frames


#: Integer vector types, which USD will not narrow a tuple of floats into.
_INT_VECTORS = (Gf.Vec2i, Gf.Vec3i, Gf.Vec4i)


def as_attr_value(attr, value):
    """`value` in the type `attr` holds, for writing a vector param into it.

    A Vector param reports its components as floats even when its sliders are integer, and
    USD will not narrow a float tuple into an int2/int3/int4 attribute: Set() raises a type
    mismatch instead, so an integer vector (Render Settings' resolution) could not be
    changed from its default at all. Other values pass through untouched.

    Args:
        attr: The attribute about to be written.
        value: The value to write.

    Returns:
        The value, as the attribute's integer vector type where that is what it holds.
    """
    if isinstance(value, (tuple, list)):
        gf_type = attr.GetTypeName().type.pythonClass
        if gf_type in _INT_VECTORS:
            return gf_type(*(int(round(v)) for v in value))
    return value


#: Purposes a selected prim's bound is measured with. Guides are left out: a light's or
#: camera's own guide geometry is not what anyone focuses on.
_FOCUS_PURPOSES = (UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy)


def focus_point_of(
    stage: Usd.Stage,
    prims: Iterable[Usd.Prim],
    time: Usd.TimeCode = Usd.TimeCode.Default(),
    exclude: Sdf.Path | None = None,
) -> Gf.Vec3d | None:
    """The average world-space centre of `prims`, or None when nothing is measurable.

    A prim with no extent counts from its origin. `exclude` is skipped (the camera itself).
    """
    cache = UsdGeom.BBoxCache(time, list(_FOCUS_PURPOSES), useExtentsHint=True)
    points = []
    for prim in prims:
        if not prim or not prim.IsValid() or (exclude is not None and prim.GetPath() == exclude):
            continue
        bound = cache.ComputeWorldBound(prim)
        if not bound.GetRange().IsEmpty():
            points.append(bound.ComputeCentroid())
        elif prim.IsA(UsdGeom.Xformable):
            points.append(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(time).ExtractTranslation())
    if not points:
        return None
    return sum(points, Gf.Vec3d(0, 0, 0)) / len(points)
