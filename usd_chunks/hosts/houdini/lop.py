"""The ``usdchunks::chunk`` LOP: one chunk per node, cooked when Houdini says.

The node type is a subnet holding one Python Script LOP whose code is a call to cook().
Which chunk a node is lives in two hidden parms, the module name and the variant (a TAB
entry under a module, see ChunkConfig.variants); the chunk object itself is made on the
node's first cook and kept for the session, keyed by the node's session id. Everything else
about the chunk is in the node's parms (see parms), so a .hip carries all of it.

A cook:

1. pushes the parms into the chunk, with the scene holding no stage -- so a param change
   does not cook on its own (chunk_cook.cook refuses without a stage);
2. opens a private stage over the node's input stage and makes it the scene's for the
   duration, with the chunk's layer above the input as ChunkLayerManager places it;
3. runs the chunk through ChunkBase._cook, the same path every host cooks with;
4. hands Houdini a copy of the chunk's layer as a sublayer of the node's output. A copy,
   because the chunk's own layer is cleared by its next cook, and Houdini may still be
   holding the stage it built from this one.

What the cook changed on the chunk -- outputs, hidden groups -- goes back to the parms
afterwards (parms.pull), since a node's parms cannot be written while it cooks.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager

import hou
import loptoolutils
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.hosts.houdini import parms
from usd_chunks.hosts.houdini.scene import HoudiniScene, ensure_scene

logger = logging.getLogger(__name__)

#: The node type every chunk node is.
HDA_TYPE = "usdchunks::chunk"
#: Hidden parm holding the variant's menu path ("Materials/standard_surface/Gold"), or "".
VARIANT_PARM = "usdchunks_variant"
#: The Python Script LOP inside the node type.
COOK_NODE = "cook"
COOK_CODE = "from usd_chunks.hosts.houdini import lop\nlop.cook(hou.pwd())\n"

#: node session id -> its chunk, for the session.
_chunks: dict[int, object] = {}
#: node session id -> the layer its last cook handed Houdini. Houdini holds it too
#: (addHeldLayer); this is so the anonymous layer is not dropped between the two.
_outputs: dict[int, Sdf.Layer] = {}


def node_type() -> hou.NodeType | None:
    return hou.nodeType(hou.lopNodeTypeCategory(), HDA_TYPE)


def _entry(module_name: str, variant: str = ""):
    """The menu entry a node was made from: the module's own, or one of its variants."""
    reg = chunk_registry.ChunkRegistry()
    namespace = tuple(p for p in variant.split("/") if p)
    fallback = None
    for entry in reg.get_menu_entries(sort_result=False):
        if entry.module.module_name != module_name:
            continue
        if namespace and entry.namespace == namespace:
            return entry
        if entry.presets is None:
            fallback = entry
    if fallback is None and (module := reg.get_chunk_module(module_name)) is not None:
        fallback = chunk_registry.MenuEntry(module=module, namespace=tuple(module.get_menu_path()))
    return fallback


def chunk_for(node: hou.Node):
    """The chunk behind a chunk node, creating it the first time the node asks.

    Raises:
        hou.NodeError: The node names a module no chunk directory provides.
    """
    key = node.sessionId()
    if (chunk := _chunks.get(key)) is not None:
        return chunk
    scene = ensure_scene()
    module_name = node.evalParm(parms.TYPE_PARM)
    entry = _entry(module_name, node.evalParm(VARIANT_PARM))
    if entry is None:
        raise hou.NodeError(
            f"No chunk module '{module_name}'. Is its folder on USD_CHUNK_DIRS?")
    created = entry.create()
    if not created:
        raise hou.NodeError(f"Chunk module '{module_name}' did not create a chunk; see the console.")
    chunk = created[0]
    with _no_stage(scene):
        scene.add_chunk(chunk)
    _chunks[key] = chunk
    return chunk


@contextmanager
def _no_stage(scene: HoudiniScene):
    """Hold the scene stage-less: whatever a param change dispatches cannot cook."""
    previous = scene.get_stage()
    scene.set_stage(None)
    try:
        yield
    finally:
        scene.set_stage(previous)


def _input_stage(node: hou.Node) -> Usd.Stage | None:
    inputs = node.inputs()
    source = inputs[0] if inputs else None
    return source.stage() if source is not None else None


@contextmanager
def _on_stage(scene: HoudiniScene, input_stage: Usd.Stage | None):
    """The scene's stage, for one cook: a private stage over the node's input.

    ChunkLayerManager serves a single stage, and a Solaris network has one per node, so it
    is pointed at this one for the duration. Every chunk layer it hands out is cleared by
    the cook that uses it, so forgetting the last node's is not losing anything.
    """
    root = Sdf.Layer.CreateAnonymous("usdchunks_input")
    if input_stage is not None:
        source = input_stage.GetRootLayer()
        root.subLayerPaths.append(source.identifier)
        # Stage metadata is read from the root layer only, so the input's is copied up.
        info = source.pseudoRoot
        for key in ("startTimeCode", "endTimeCode", "timeCodesPerSecond", "framesPerSecond",
                    "upAxis", "metersPerUnit", "defaultPrim"):
            if info.HasInfo(key):
                root.pseudoRoot.SetInfo(key, info.GetInfo(key))
    stage = Usd.Stage.Open(root)
    layers = chunk_registry.ChunkLayerManager()
    layers.reset()
    layers.initialize(stage)
    previous = scene.get_stage()
    scene.set_stage(stage)
    try:
        yield stage
    finally:
        scene.set_stage(previous)


def cook(lop: hou.LopNode) -> None:
    """Cook the chunk node lop sits in. The Python Script LOP inside the type calls this."""
    node = lop.parent()
    scene = ensure_scene()
    try:
        chunk = chunk_for(node)
        with _no_stage(scene):
            parms.push(node, chunk)
        with _on_stage(scene, _input_stage(node)):
            chunk._cook()
            layer = chunk_registry.ChunkLayerManager().get_layer(chunk)
            out = Sdf.Layer.CreateAnonymous(f"usdchunks_{node.name()}")
            if layer is not None:
                out.TransferContent(layer)
    except hou.Error:
        raise
    except Exception as exc:
        logger.exception("chunk node %s failed to cook", node.path())
        raise hou.NodeError(f"{type(exc).__name__}: {exc}") from exc

    lop.editableStage()  # Houdini takes held layers and sublayers only after this
    lop.addHeldLayer(out.identifier)
    lop.addSubLayer(out.identifier)
    _outputs[node.sessionId()] = out
    scene.call_later(lambda: _after_cook(node, chunk))


def _alive(node: hou.Node) -> bool:
    try:
        node.path()
    except hou.ObjectWasDeleted:
        return False
    return True


def _after_cook(node: hou.Node, chunk) -> None:
    """Show on the parms what the cook changed on the chunk, once the node may be edited."""
    if not _alive(node) or _chunks.get(node.sessionId()) is not chunk:
        return
    if not parms.interface_matches(node, chunk):
        parms.build_interface(node, chunk)
    parms.pull(node, chunk)


def press_button(kwargs) -> None:
    """A Button parm's callback: click the chunk Button, on the node's input stage.

    The chunk's own layer is cleared by the cook this click causes, so a callback's lasting
    effect is what it sets on the chunk's params -- shown on the parms here, which recooks.
    """
    node = kwargs["node"]
    chunk = chunk_for(node)
    param = parms.find(chunk, kwargs["parm_name"])
    if param is None:
        return
    scene = ensure_scene()
    with _no_stage(scene):
        parms.push(node, chunk)
    with _on_stage(scene, _input_stage(node)):
        param.click()
    parms.pull(node, chunk)


def menu_items(node: hou.Node, parm_name: str) -> list[str]:
    """A menu parm's items as Houdini wants them, token and label alternating."""
    try:
        chunk = chunk_for(node)
        param = parms.find(chunk, parm_name)
        if param is None:
            return []
        if hasattr(param, "refresh_items"):
            with _on_stage(ensure_scene(), _input_stage(node)):
                param.refresh_items()
        items = [str(i) for i in (getattr(param, "menu_items", None) or [])]
    except Exception:
        logger.exception("menu items for %s/%s", node.path(), parm_name)
        return []
    return [x for item in items for x in (item, item)]


def on_deleted(kwargs) -> None:
    """The node type's OnDeleted event: forget the node's chunk."""
    key = kwargs["node"].sessionId()
    _outputs.pop(key, None)
    chunk = _chunks.pop(key, None)
    if chunk is None:
        return
    scene = ensure_scene()
    with _no_stage(scene):
        try:
            scene.remove_chunk(chunk.get_hash())
        except Exception:
            logger.exception("removing chunk %s", chunk.name)


def _node_name(entry) -> str:
    name = re.sub(r"\W+", "_", entry.namespace[-1]).strip("_").lower()
    return name or "chunk"


def setup(node: hou.Node, entry) -> object:
    """Make node the chunk entry describes: name the module, build the parms."""
    node.parm(parms.TYPE_PARM).set(entry.module.module_name)
    node.parm(VARIANT_PARM).set("/".join(entry.namespace) if entry.presets is not None else "")
    _chunks.pop(node.sessionId(), None)
    chunk = chunk_for(node)
    parms.build_interface(node, chunk)
    parms.pull(node, chunk)
    return chunk


def create(parent: hou.Node, module_name: str, variant: str = "", name: str | None = None) -> hou.LopNode:
    """Create a chunk node in a LOP network, from a script.

    Args:
        parent: The LOP network, e.g. hou.node("/stage").
        module_name: The chunk module, as ChunkModule.module_name ("mesh").
        variant: A variant's menu path, or "" for the module itself.
        name: The node's name; the entry's label when omitted.
    """
    ensure_scene()
    entry = _entry(module_name, variant)
    if entry is None:
        raise hou.OperationFailed(f"No chunk module '{module_name}'")
    node = parent.createNode(HDA_TYPE, name or _node_name(entry))
    setup(node, entry)
    return node


def create_from_tool(kwargs, module_name: str, variant: str = "") -> hou.LopNode | None:
    """A TAB menu entry's script: put a chunk node down where the user is working."""
    scene = ensure_scene()
    entry = _entry(module_name, variant)
    if entry is None:
        raise hou.OperationFailed(f"No chunk module '{module_name}'")
    node = loptoolutils.genericTool(kwargs, HDA_TYPE, nodename=_node_name(entry))
    if node is not None:
        scene.asking = True  # a chunk needing packages may ask for them now, and only now
        try:
            setup(node, entry)
        finally:
            scene.asking = False
    return node
