from pxr import Sdf

from usd_chunks import chunk, chunk_utils


_INSERT_MODES = ["Append", "Prepend", "Index", "After Layer"]


def _refresh_after_layer(owner, notice, sender):
    """Rebuild the after-layer menu from the current parent layer's sublayers.

    Registered with listen_globally=True — safe with any number of Create Layer
    instances since NoticeConnection now shares one real Tf.Notice.RegisterGlobally()
    per alias across all of them (see chunk_notice._register_global).
    """
    _chunk = owner.get_chunk()
    stage = _chunk.get_stage() if _chunk else None
    if not stage:
        return

    internal_ids = chunk_utils.chunk_internal_layer_ids()
    parent_layer = chunk_utils.get_edit_layer(stage, _chunk)

    after_items = ["(none)"] + [p for p in parent_layer.subLayerPaths if p not in internal_ids]
    after_menu = _chunk.get_param("after_layer")
    after_menu.menu_items = after_items
    if after_menu.value not in after_items:
        after_menu.value = after_items[0]


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


def execute(node):
    """Find or (re-)create the chunk's anon sublayer, and reposition it in the
    current parent layer's subLayerPaths per the current insert settings.

    The parent is chunk_utils.get_edit_layer(stage): the session layer by default,
    or whichever layer an upstream Edit Target chunk has explicitly pointed at — so
    a Create Layer downstream of an Edit Target nests its new layer inside that
    target instead of always sublayering directly under the session layer.

    Anon layers don't survive a stage reload, so the stored identifier can go
    stale between sessions; if Find() misses, a fresh layer is created and the
    stored id is overwritten, matching the regenerate-on-load behavior other
    procedural chunks rely on.

    The anon layer's tag (Layer Name) is user-facing — it's what shows up for this
    layer in e.g. Export USD's layer picker, or an Edit Target chunk's dropdown. Sdf
    identifiers are immutable once created, so renaming means creating a fresh anon
    layer under the new tag and transferring the old one's content across before
    swapping it into subLayerPaths.
    """
    # menu_items/value assignments in _refresh_after_layer, and the id_param/name_param/
    # parent_param writes below, can reactively re-trigger this same chunk's execute()
    # before this call returns. We must NOT suppress that reactivity at the notice
    # level (e.g. via set_blocked()) — that also blocks the notice the node-graph
    # widget needs to know it should repaint, silently breaking the UI. Guard the
    # reentrant *call* directly instead, so widgets still refresh normally.
    if getattr(node, "_create_layer_executing", False):
        return
    node._create_layer_executing = True
    try:
        stage = node.get_stage()
        if not stage:
            return

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
            # The current edit target is this very layer — either its new identity, or
            # (mid-rename) its about-to-be-replaced previous one, e.g. because a
            # downstream Edit Target chunk had already locked onto it. Nesting the new
            # layer inside either would corrupt the layer stack (self-reference, or
            # nesting inside a layer we're about to orphan). Fall back to the session
            # layer rather than either.
            parent_layer = stage.GetSessionLayer()
        previous_parent_id = parent_param.value or None
        if previous_parent_id and previous_parent_id != parent_layer.identifier:
            # The effective edit target changed since the last run (e.g. an upstream
            # Edit Target's selection changed, or one was added later) — drop the stale
            # entry from the OLD parent so the layer doesn't end up sublayered twice.
            if old_parent := Sdf.Layer.Find(previous_parent_id):
                old_paths = [p for p in old_parent.subLayerPaths if p not in (previous_id, layer.identifier)]
                if len(old_paths) != len(old_parent.subLayerPaths):
                    old_parent.subLayerPaths = old_paths

        paths = [p for p in parent_layer.subLayerPaths if p not in (previous_id, layer.identifier)]

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
    createLayerChunk = chunk.Chunk("Create Layer")

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
        tooltip="Where the new layer is inserted among the session layer's sublayers.",
    )
    insert_mode.add_callback(_insert_mode_changed, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)

    insert_index = chunk.Slider(
        "insert_index", "Index", is_int=True, min_value=0, default_value=0,
    )
    after_layer = chunk.Menu(
        "after_layer", "After Layer",
        menu_items=["(none)"], default_value="(none)",
    )

    new_layer_id = chunk.LineEdit("_new_layer_id")
    new_layer_id.set_state(chunk.ChunkFlags.State.HIDDEN)

    layer_name_used = chunk.LineEdit("_layer_name_used")
    layer_name_used.set_state(chunk.ChunkFlags.State.HIDDEN)

    parent_layer_id = chunk.LineEdit("_parent_layer_id")
    parent_layer_id.set_state(chunk.ChunkFlags.State.HIDDEN)

    createLayerChunk.add_params([
        layer_name,
        insert_mode,
        insert_index,
        after_layer,
        new_layer_id,
        layer_name_used,
        parent_layer_id,
    ])

    _update_visibility(createLayerChunk)

    createLayerChunk.add_callback(
        _refresh_after_layer,
        notice_alias=chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED,
        listen_globally=True,
        send_on_create=True,
    )

    return createLayerChunk


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Create Layer chunk — authors a fresh anonymous layer and sublayers it into the
        session layer, or into whichever layer an upstream Edit Target chunk has
        explicitly pointed at — so chaining Create Layer → Edit Target → Create Layer
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
