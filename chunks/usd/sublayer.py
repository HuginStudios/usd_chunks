from __future__ import annotations
import logging

from pxr import Sdf

from usd_chunks import chunk, chunk_stage, chunk_utils

logger = logging.getLogger(__name__)


def _apply_sublayers(owner: chunk.Chunk) -> None:
    """Collect every non-empty path in `imports` and assign them wholesale to the chunk's
    own edit layer's subLayerPaths, replacing whatever was there before in one step.

    A file that holds a chunk network is not sublayered: it is hosted on this chunk's own
    prim (chunk_stage.host_networks), so the node becomes the network and can be dived
    into, exactly as a Reference pointed at one does. Several such files stack under the
    node in list order, first strongest, the same order the sublayers take.
    """
    list_group = owner.get_param("imports", chunk.is_inst(chunk.ListGroup))
    paths = []

    # assert isinstance(list_group, chunk.ListGroup)

    for i in range(list_group.size):
        p = list_group.get_indexed_param("sublayer_path", i, chunk.is_param_t(str))
        if p and p.value:
            paths.append(p.value)

    # Opened once here, for the network check and the sublayering below.
    chunk_stage.hold_asset_layers(owner, paths)
    networks = [(p, target) for p in paths if (target := chunk_stage.network_target(p))]
    hosted = {p for p, _ in networks}
    paths = [p for p in paths if p not in hosted]
    chunk_stage.host_networks(owner, networks)

    # Since we expect to only have one sublayer per chunk
    # We clear the old sublayer paths with the new one
    stage = owner.get_stage()
    if stage:
        # Each chunk has it's sublayer were all edigts go
        # By grabbing the current edittarget we get the chunks layer
        # in which we can add in our sublayer path.
        if edit_layer := owner.get_edit_context():
            edit_layer.subLayerPaths = paths
            logger.debug("Updated stage with sublayer. %s", paths)

    # Load Payloads: the files' own root prims, so unchecked unloads every payload in what
    # they bring in -- for a shot, the fast stage of proxies only.
    if (load := owner.get_param("load_payloads")) is not None and (scene_stage := owner.get_scene_stage()):
        roots = [f"/{root.name}" for p in paths if (layer := Sdf.Layer.FindOrOpen(p)) for root in layer.rootPrims]
        chunk_utils.set_payloads_loaded(owner, scene_stage, roots, load.value)


def execute(node: chunk.Chunk) -> None:
    """Reserved top-level reactive hook (chunk_notice.py calls `module.execute(node)`, one
    positional arg, on every DAG solve for any module that defines it) -- load_chunks (the
    real "reload a saved scene/chunkgroup" path) explicitly calls this once per chunk after
    bulk-restoring its param values, specifically to make a freshly-loaded chunk take real
    effect again. Needed here because export_chunks only copies prim ATTRIBUTES (imports'
    own values), never a layer's own subLayerPaths -- without this, a reloaded Sublayer
    chunk would show the correct path in its field but never actually bring the layer back
    in, until the field was touched again live.
    """
    _apply_sublayers(node)


def _on_attribute_changed(owner: chunk.Chunk, notice, sender):
    """Live-editing callback for changing sublayer paths -- add_callback's 3-arg form, NOT
    the reserved execute(node) hook above (which does the same real work, for the reload
    case). Keeping this a separate, differently-named function is deliberate: naming a
    callback `execute` collides with the reserved hook's 1-arg calling convention and
    crashed the very first time a Sublayer chunk got created on a live stage.
    """
    if notice.data.name not in ["size", "value"]:
        return
    # Set while the chunk must not cook: a value shown on a dropped, still-unwired node
    # (graph.engine.set_param_visibly), or values restored before a cook. execute() applies
    # the paths once it does cook.
    if getattr(owner, "_executing", False):
        return
    _apply_sublayers(owner)


def main():
    sublayerChunk = chunk.Chunk("Sublayer")

    sublayer_fb = chunk.FileBrowser("sublayer_path", extensions=[".usd", ".usda", ".usdc", ".usdz"])
    sublayer_fb.set_flag(chunk.ChunkFlags.ParamFlags.FILE_EXISTING)
    button = chunk.Button("import", "import")

    listGroup = chunk.ListGroup("imports")
    listGroup.unset_flag(chunk.ChunkFlags.GroupFlags.LABELED)
    listGroup.add_param(sublayer_fb)

    sublayerChunk.add_param(listGroup)
    sublayerChunk.add_param(button)
    sublayerChunk.add_param(chunk.Checkbox(
        "load_payloads", "Load Payloads", default_value=True,
        tooltip="Unchecked: every payload in what these files bring in is unloaded, so only "
        "what sits outside payloads (proxies) is loaded -- a fast stage. Checked loads back "
        "what this unloaded; it never loads a payload unloaded by hand.",
    ))

    # Because the names of the ListGroup is modified when an item in the list is removed, listening to ATTRIBUTE_CHANGED
    # will result in double execution of the callback when removing an index from the ListGroup.
    # Although, we'd like to listen to ATTRIBUTE_CHANGED to catch 'size' changes, and 'value' changes. We dont have a nice way to
    # filter the senders, so doing it inside the callback by checking the name of the notice data

    sublayerChunk.add_callback(_on_attribute_changed, notice_alias=chunk.ChunkNoticeAlias.ATTRIBUTE_CHANGED)
    return sublayerChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="layers",
        description="""
        Adds external USD files as sublayers, under everything else in the scene.

        Sublayer chunk: Lets the user add one or more external USD files as sublayers
        on the chunk's own edit layer, making those files compose underneath all
        other opinions on the stage.

        Parameters:
          imports (ListGroup of sublayer_path FileBrowsers)
            A dynamic list where each entry is a file browser constrained to existing
            USD files. Add rows to stack multiple sublayers; remove rows to drop them.
          import (Button)
            Provides an explicit trigger in the UI; the underlying rebuild is driven
            by the ATTRIBUTE_CHANGED callback, not the button directly.

        A file that holds a saved chunk network is hosted rather than sublayered: the node
        becomes that network -- dive in to see and tweak it, as with a chunkgroup. Tweaks
        are overrides in the current file; the network file is never written to. Several
        network files stack under the node in list order, first strongest.

        On every change, the shared _apply_sublayers helper collects all non-empty path
        values from the list, then assigns them wholesale to edit_layer.subLayerPaths,
        replacing the previous list in one step. Reached two ways: live, via the
        ATTRIBUTE_CHANGED callback below (guarded with `notice.data.name not in ["size",
        "value"]` so it only fires for real path edits and list-size changes -- per-param
        callbacks are deliberately avoided here, since removing a list item renames internal
        params and would fire the callback twice, producing a spurious intermediate state);
        and on reload, via the reserved execute(node) hook that load_chunks calls once per
        chunk after restoring its saved values (needed because export_chunks only copies the
        imports param's own values, never the layer's own subLayerPaths).
        """)
