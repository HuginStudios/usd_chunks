from __future__ import annotations
from pxr import Sdf

from usd_chunks import chunk, chunk_utils
from usd_chunks import chunk_stage


_INSERT_MODES = ["Append", "Prepend", "Index", "After Layer"]

#: Set once CHUNK_LOADED has fired -- values are in, the after-layer menu may refresh.
_IN_SCENE = "_layer_in_scene"


def _refresh_after_layer(owner, notice, sender):
    """Rebuild the after-layer menu from the current parent layer's sublayers.

    Registered with listen_globally=True -- safe with any number of Layer
    instances since NoticeConnection now shares one real Tf.Notice.RegisterGlobally()
    per alias across all of them (see chunk_notice._register_global).
    """
    _chunk = owner.get_chunk()
    stage = _chunk.get_stage() if _chunk else None
    if not stage:
        return
    if not getattr(_chunk, _IN_SCENE, False):
        # Held off until CHUNK_LOADED: a saved After Layer choice is restored after main()
        # registers this, and resetting a value the list does not hold yet would drop it.
        return
    if _chunk.is_blocked():
        # This is listen_globally=True, so it fires for EVERY Layer instance on
        # ANY stage content change -- including another Layer's own subLayerPaths
        # write, or recook_for_view's own layer reorder. The after_menu.value write below
        # reactively re-triggers THIS chunk's own execute() (chunk_notice.py's DAG solve),
        # which must not happen for a blocked chunk (not part of the active view's tree --
        # e.g. not yet wired in, or genuinely hidden). Without this guard, a still-blocked
        # Layer could get dragged into a cascade of extra premature executes
        # triggered by its SIBLINGS' unrelated edits, each one reinserting it into
        # subLayerPaths and scrambling "closer to view wins" into "whichever cascade call
        # happened to land last".
        return
    insert_mode_param = _chunk.get_param("insert_mode")
    if insert_mode_param is None:
        # Same half-dismantled-chunk case as the after_menu guard below: deleting this
        # chunk tears its params down before this global handler (still firing for the
        # stage edits teardown itself makes) gets a chance to run for it again.
        return
    if insert_mode_param.value != "After Layer":
        # after_layer is hidden/unused in every other mode (_update_visibility). Refreshing
        # it anyway writes menu_items -- a plain attrs field whose content is "the current
        # subLayerPaths list", which differs on essentially every sibling Layer's
        # edit -- and that write unconditionally re-triggers THIS chunk's own execute() as
        # a side effect (chunk_core.py value_changed -> self.execute()), regardless of
        # whether anything about THIS chunk actually changed. Live-confirmed: this alone
        # cascades into hundreds of spurious re-executions across just a few sibling Create
        # Layer chunks during ordinary graph construction, each one blindly reinserting
        # this chunk via raw Prepend/Append (no view is reachable yet to rank against
        # during a build) -- whichever cascade call happens to land last wins the strongest
        # position, an accident of notice dispatch order having nothing to do with graph
        # position. That scrambled order then freezes in place, since nothing re-executes a
        # Layer chunk in response to exec topology changing once it's wired to a view.
        return

    internal_ids = chunk_utils.chunk_internal_layer_ids()
    parent_layer = chunk_utils.get_edit_layer(stage, _chunk)

    after_items = ["(none)"] + [p for p in parent_layer.subLayerPaths if p not in internal_ids]
    after_menu = _chunk.get_param("after_layer")
    if after_menu is None:
        # Deleting this chunk tears its params down, but the layer-stack edits that teardown
        # makes are themselves stage content changes -- so this global handler still fires for
        # the half-dismantled chunk. Nothing left to refresh.
        return
    # Only on a change: every stage change round lands here, for every Layer chunk.
    if list(after_menu.menu_items or []) != after_items:
        after_menu.menu_items = after_items
    if after_menu.value not in after_items:
        after_menu.value = after_items[0]


def _chunk_loaded(owner, notice, sender):
    setattr(owner.get_chunk(), _IN_SCENE, True)
    _refresh_after_layer(owner, notice, sender)


def _update_visibility(owner):
    mode = owner.get_param("insert_mode").value

    def _set_hidden(name, hidden):
        param = owner.get_param(name)
        if hidden:
            param.set_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            param.unset_state(chunk.ChunkFlags.State.HIDDEN)

    _set_hidden("insert_index", mode != "Index")
    _set_hidden("after_layer", mode != "After Layer")


def _insert_mode_changed(owner, notice, sender):
    _update_visibility(owner.get_chunk())
    if owner.get_chunk().get_param("insert_mode").value == "After Layer":
        # _refresh_after_layer now skips every mode but this one (see its own guard), so
        # switching INTO "After Layer" needs one explicit refresh here -- otherwise the
        # menu stays stale (whatever it last showed before this chunk existed/switched
        # modes) until some unrelated sibling edit happens to trigger the global listener.
        _refresh_after_layer(owner, notice, sender)


def _view_ranked_index(node, stage, paths: list) -> int | None:
    """Where this chunk's layer belongs among `paths`, ranked by exec-chain proximity to
    the active downstream view.

    Applies REGARDLESS of Insert mode: two Layer chunks that ARE comparable
    (reachable from a common view) landing in the wrong relative order isn't something
    Prepend/Index/After Layer should be able to override -- "closer to the view wins" is
    a project-wide invariant (recook_for_view enforces it for its own ChunkLayerManager-
    tracked layers), not a per-chunk style choice. Insert mode only matters when this
    returns None -- see execute()'s fallback.

    Ranks this chunk's own CONTAINER (its immediate context -- typically a ChunkGroup, or
    this chunk itself if it has none) as one atomic unit against sibling Layer
    chunks' containers, using the nearest reachable downstream view's exec-upstream
    order. Ranking the container rather than the chunk itself is what makes two
    ChunkGroups (each owning an independent Layer) compare correctly as wholes.

    Returns None -- defer entirely to Insert mode -- whenever there's no view to rank
    against yet (e.g. right after a fresh paste, before anything is wired in) or this
    chunk isn't inside any container at all.
    """
    scene = node.get_scene()
    if not scene:
        return None
    container = node.get_context()
    ranking_unit = container if container is not None else node
    outer = ranking_unit.get_context()
    view_chunk = chunk_stage.find_group_active_view(outer, stage, scene) if outer is not None else None
    if view_chunk is None:
        # No enclosing group context to read a chunk:activeView slot from -- e.g. several
        # sibling ChunkGroups chained directly under a tab, not nested inside another
        # group (find_group_active_view has nowhere to look: the root tab's own active
        # view is tracked only in the Qt widget layer, not on any group prim this pure-
        # topology function can read) -- or the enclosing group just has no recorded
        # active view yet. Fall back to whatever real View chunk this unit's own exec
        # output actually reaches downstream, and rank directly against that view's cook
        # list instead -- same "closer to the view wins" signal, just found a different
        # way.
        downstream = chunk_stage.exec_downstream(ranking_unit, stage, scene)
        view_chunk = next((c for c in downstream if chunk_stage.is_view_chunk(c)), None)
    if view_chunk is None:
        return None
    cook_list = chunk_stage.view_cook_list(view_chunk, stage, scene)
    if ranking_unit not in cook_list:
        return None
    my_rank = cook_list.index(ranking_unit)

    # Map each existing sublayer identifier back to whichever OTHER Layer chunk (if
    # any) produced it, so its own container can be ranked the same way.
    id_to_chunk = {}
    for c in scene.get_chunks():
        cfg = c.get_config()
        if not cfg or cfg.module != "layer":
            continue
        lid = c.get_param("_new_layer_id")
        if lid and lid.value:
            id_to_chunk[lid.value] = c

    def sibling_rank(path_id: str) -> int:
        sibling = id_to_chunk.get(path_id)
        if sibling is None:
            return -1
        sib_container = sibling.get_context()
        sib_unit = sib_container if sib_container is not None else sibling
        return cook_list.index(sib_unit) if sib_unit in cook_list else -1

    # subLayerPaths[0] is the strongest sublayer; keep entries ordered by descending rank
    # (closer to the view = higher rank = stronger = lower index) and insert at the first
    # spot whose existing entry is no stronger than this one. A resolved rank always
    # returns a real int here (never None) -- "nothing comparable in paths yet" just means
    # landing at the end, same as a fresh Append would, not "give up and use Insert mode".
    for i, path_id in enumerate(paths):
        if sibling_rank(path_id) <= my_rank:
            return i
    return len(paths)


def execute(node):
    """Find or (re-)create the chunk's anon sublayer, and reposition it in the
    current parent layer's subLayerPaths per the current insert settings.

    The parent is chunk_utils.get_edit_layer(stage): the session layer by default,
    or whichever layer an upstream Edit Target chunk has explicitly pointed at -- so
    a Layer downstream of an Edit Target nests its new layer inside that
    target instead of always sublayering directly under the session layer.

    Anon layers don't survive a stage reload, so the stored identifier can go
    stale between sessions; if Find() misses, a fresh layer is created and the
    stored id is overwritten, matching the regenerate-on-load behavior other
    procedural chunks rely on.

    The anon layer's tag (Layer Name) is user-facing -- it's what shows up for this
    layer in e.g. Export USD's layer picker, or an Edit Target chunk's dropdown. Sdf
    identifiers are immutable once created, so renaming means creating a fresh anon
    layer under the new tag and transferring the old one's content across before
    swapping it into subLayerPaths.
    """
    # menu_items/value assignments in _refresh_after_layer, and the id_param/name_param/
    # parent_param writes below, can reactively re-trigger this same chunk's execute()
    # before this call returns. We must NOT suppress that reactivity at the notice
    # level (e.g. via set_blocked()) -- that also blocks the notice the node-graph
    # widget needs to know it should repaint, silently breaking the UI. Guard the
    # reentrant *call* directly instead, so widgets still refresh normally.
    if getattr(node, "_create_layer_executing", False):
        return
    node._create_layer_executing = True
    try:
        stage = node.get_stage()

        id_param = node.get_param("_new_layer_id")
        name_param = node.get_param("_layer_name_used")
        parent_param = node.get_param("_parent_layer_id")
        desired_name = node.get_param("layer_name").value.strip() or node.name

        previous_id = id_param.value or None
        layer = Sdf.Layer.Find(previous_id) if previous_id else None

        if layer is None:
            layer = Sdf.Layer.CreateAnonymous(desired_name)
        elif name_param.value != desired_name:
            renamed = Sdf.Layer.CreateAnonymous(desired_name)
            renamed.TransferContent(layer)
            layer = renamed

        parent_layer = chunk_utils.get_edit_layer(stage, node)
        if parent_layer.identifier in (layer.identifier, previous_id):
            # The current edit target is this very layer -- either its new identity, or
            # (mid-rename) its about-to-be-replaced previous one, e.g. because a
            # downstream Edit Target chunk had already locked onto it. Nesting the new
            # layer inside either would corrupt the layer stack (self-reference, or
            # nesting inside a layer we're about to orphan). Fall back to the session
            # layer rather than either.
            parent_layer = stage.GetSessionLayer()
        previous_parent_id = parent_param.value or None
        if previous_parent_id and previous_parent_id != parent_layer.identifier:
            # The effective edit target changed since the last run (e.g. an upstream
            # Edit Target's selection changed, or one was added later) -- drop the stale
            # entry from the OLD parent so the layer doesn't end up sublayered twice.
            if old_parent := Sdf.Layer.Find(previous_parent_id):
                old_paths = [p for p in old_parent.subLayerPaths if p not in (previous_id, layer.identifier)]
                if len(old_paths) != len(old_parent.subLayerPaths):
                    old_parent.subLayerPaths = old_paths

        paths = [p for p in parent_layer.subLayerPaths if p not in (previous_id, layer.identifier)]

        # "Closer to the view wins" always takes priority over Insert mode when it can be
        # determined -- Insert mode is a fallback for when there's nothing to rank against
        # yet (no view reachable, or this chunk isn't in a container), not a way to
        # override a real, resolvable view relationship. See _view_ranked_index.
        index = _view_ranked_index(node, stage, paths)
        if index is None:
            mode = node.get_param("insert_mode").value
            if mode == "Prepend":
                # subLayerPaths[0] is the strongest sublayer.
                index = 0
            elif mode == "Index":
                index = int(node.get_param("insert_index").value or 0)
                index = max(0, min(index, len(paths)))
            elif mode == "After Layer":
                after_id = node.get_param("after_layer").value
                index = paths.index(after_id) + 1 if after_id in paths else len(paths)
            else:
                index = len(paths)

        paths.insert(index, layer.identifier)
        # Insert into the composed stack BEFORE updating id_param/name_param/parent_param
        # below: those value assignments can reactively re-execute downstream dependents
        # (e.g. an Edit Target chunk tracking this one), and if that happens before the
        # layer is actually part of the stack, SetEditTarget on it fails with "not in
        # the local LayerStack".
        parent_layer.subLayerPaths = paths

        id_param.value = layer.identifier
        name_param.value = desired_name
        parent_param.value = parent_layer.identifier
    finally:
        node._create_layer_executing = False


def main():
    layerChunk = chunk.Chunk("Layer")

    layer_name = chunk.LineEdit(
        "layer_name", "Layer Name",
        tooltip="Display name/tag for the created layer (e.g. shown in an Edit Target chunk's "
        "dropdown, or Export USD's layer picker). Leave blank to use the chunk's own name. "
        "Changing this after creation renames the layer in place, carrying over anything "
        "already authored into it.",
    )

    insert_mode = chunk.Menu(
        "insert_mode", "Insert",
        menu_items=_INSERT_MODES, default_value="Append",
        tooltip="Where the new layer is inserted among the session layer's sublayers, "
        "when there's no view downstream to rank it against yet. Once this chunk is "
        "wired into a view, \"closer to the view wins\" always takes over instead.",
    )
    insert_mode.add_callback(_insert_mode_changed, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)

    insert_index = chunk.Slider(
        "insert_index", "Index", is_int=True, min_value=0, default_value=0,
    )
    after_layer = chunk.Menu(
        "after_layer", "After Layer",
        menu_items=["(none)"], default_value="(none)",
    )

    # All three are bookkeeping for the layer this chunk owns *in this session*: two of them
    # hold an anonymous layer's identifier, which names an in-memory object and nothing else.
    # TRANSIENT keeps them off the chunk prim, so a saved scene does not come back holding the
    # address of a layer that died with the session that made it -- a fresh one is created on
    # reload, which is what the empty value means.
    new_layer_id = chunk.LineEdit("_new_layer_id", flags=chunk.ChunkFlags.ParamFlags.TRANSIENT)
    new_layer_id.set_state(chunk.ChunkFlags.State.HIDDEN)

    layer_name_used = chunk.LineEdit("_layer_name_used", flags=chunk.ChunkFlags.ParamFlags.TRANSIENT)
    layer_name_used.set_state(chunk.ChunkFlags.State.HIDDEN)

    parent_layer_id = chunk.LineEdit("_parent_layer_id", flags=chunk.ChunkFlags.ParamFlags.TRANSIENT)
    parent_layer_id.set_state(chunk.ChunkFlags.State.HIDDEN)

    layerChunk.add_params([
        layer_name,
        insert_mode,
        insert_index,
        after_layer,
        new_layer_id,
        layer_name_used,
        parent_layer_id,
    ])

    _update_visibility(layerChunk)

    layerChunk.add_callback(_chunk_loaded, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)
    layerChunk.add_callback(
        _refresh_after_layer,
        notice_alias=chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED,
        listen_globally=True,
    )

    return layerChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_LAYER,
        description="""
        Layer chunk — authors a fresh anonymous layer and sublayers it into the
        session layer, or into whichever layer an upstream Edit Target chunk has
        explicitly pointed at — so chaining Layer → Edit Target → Layer
        nests each new layer inside the previous one instead of stacking them all as
        siblings under the session layer. Purely about creating/positioning the layer;
        use an Edit Target chunk downstream to actually redirect authoring into it.

        Layer Name sets the anon layer's tag — the human-readable name shown for it
        elsewhere (e.g. an Edit Target chunk's dropdown, or Export USD's layer picker);
        leave blank to use the chunk's own name.

        Insert controls where it lands among the parent layer's existing sublayers:
        Append (weakest), Prepend (strongest), Index (explicit position), or After
        Layer (just weaker than a chosen sublayer). After Layer's choices refresh
        automatically as the stage's layer stack changes. If the effective parent
        changes between runs (e.g. an upstream Edit Target's selection changes), the
        stale entry in the old parent is cleaned up automatically.

        The created anonymous layer is idempotent across re-executions: its identifier
        is cached on the chunk, and each run repositions the same layer rather than
        creating a new one. Anonymous layers don't survive a stage reload, so on the
        next load a new one is created automatically, matching the regenerate-on-load
        behavior of other procedural chunks. Changing Layer Name after the layer
        already exists renames it in place — a new anon layer is created under the
        new tag, the old layer's content is transferred across, and it swaps into
        subLayerPaths at the position Insert currently specifies.
        """,
    )
