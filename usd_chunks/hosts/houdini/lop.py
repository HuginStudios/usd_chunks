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
4. merges the chunk's layer into Houdini's active layer, the chunk's edits winning, as any
   Solaris node authors (see layers). A Layer chunk node starts a new active layer first,
   through the Configure Layer inside the node type, and names it.

What the cook changed on the chunk -- outputs, hidden groups -- goes back to the parms
afterwards (parms.pull), since a node's parms cannot be written while it cooks.
"""
from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager

import hou
import loptoolutils
from pxr import Sdf, Usd

from usd_chunks import chunk_core as core, chunk_registry
from usd_chunks.chunk_naming import CHUNKS_ROOT
from usd_chunks.hosts.houdini import layers, parms, space
from usd_chunks.hosts.houdini.scene import HoudiniScene, ensure_scene

logger = logging.getLogger(__name__)

#: The node type every chunk node is.
HDA_TYPE = "usdchunks::chunk"
#: The Python Script LOP inside the node type.
COOK_NODE = "cook"
#: Inside the node type: the Configure Layer a Layer chunk starts its new layer with, and
#: the switch that sends only a Layer chunk through it.
NEW_LAYER_NODE = "new_layer"
SWITCH_NODE = "layer_switch"
COOK_CODE = "from usd_chunks.hosts.houdini import lop\nlop.cook(hou.pwd())\n"

#: node session id -> its chunk, for the session.
_chunks: dict[int, object] = {}
#: node session id -> a copy of the chunk's layer as its last cook merged it.
_outputs: dict[int, Sdf.Layer] = {}
#: node session id -> saved values import could not set yet, by param path: params the
#: chunk builds in its own cook (a Light's attributes). Set by the first cook that has them.
_pending: dict[int, dict[str, object]] = {}
#: Node user data holding the chunk's transform op id (chunk_utils.XFORM_OP_ID_ATTR). usdview
#: keeps it on the chunk's prim, which a node's stage does not hold; here it saves with the .hip.
XFORM_OP_KEY = "usdchunks_xform_op_id"


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
    entry = _entry(module_name, node.evalParm(parms.VARIANT_PARM))
    if entry is None:
        raise hou.NodeError(
            f"No chunk module '{module_name}'. Is its folder on USD_CHUNK_DIRS?")
    created = entry.create()
    if not created:
        raise hou.NodeError(f"Chunk module '{module_name}' did not create a chunk; see the console.")
    chunk = created[0]
    with _no_stage(scene):
        scene.add_chunk(chunk)
    object.__setattr__(chunk, "_xform_op_suffix", _xform_op_id(node))
    _chunks[key] = chunk
    return chunk


def _xform_op_id(node: hou.Node) -> str:
    """The node's transform op id: the one it recorded, else one minted from its name and path.

    Minted as chunk_utils does from a chunk's prim path, so two nodes never share an op and
    the same network names its ops the same way each time it is built.
    """
    if saved := node.userData(XFORM_OP_KEY):
        return saved
    name = re.sub(r"\W+", "_", node.name()).strip("_") or "Transform"
    return f"{name}_{hashlib.sha1(node.path().encode()).hexdigest()[:8]}"


def set_xform_op_id(node: hou.Node, op_id: str) -> None:
    """Give the node the transform op id a saved network recorded for its chunk."""
    node.setUserData(XFORM_OP_KEY, op_id)
    object.__setattr__(chunk_for(node), "_xform_op_suffix", op_id)


def defer_value(node: hou.Node, path: str, value) -> None:
    """Set param `path` to `value` once a cook has built it."""
    _pending.setdefault(node.sessionId(), {})[path] = value


def _apply_pending(node: hou.Node, chunk) -> None:
    pending = _pending.get(node.sessionId())
    for path in list(pending or ()):
        if (param := chunk.get_param(path)) is not None:
            param.value = pending.pop(path)


def _drive_attributes(chunk, stage: Usd.Stage, layer: Sdf.Layer) -> None:
    """Write each param tied to another prim's attribute onto it, in the chunk's layer.

    A param show_attribute or connect_usd_attr tied to a prim's attribute (a Light's
    intensity, a Camera's focal length) drives it elsewhere through a connection from the
    chunk's prim. A node's stage holds no chunk prims, so the value is written itself,
    where it differs from what the attribute already holds.
    """
    for _, param in chunk.iter_hierarchy():
        if (not isinstance(param, core.ChunkUsdAttributeSupport) or isinstance(param, core.ChunkGroupBase)
                or param.get_chunk() is not chunk):
            continue
        value = getattr(param, "value", None)
        if value is None:
            continue
        paths = list(param._driven_attr_paths)
        if (own := param._param_attr_path) and not own.startswith(CHUNKS_ROOT + "/"):
            paths.append(own)
        for path in paths:
            attr = stage.GetAttributeAtPath(Sdf.Path(path))
            if not (attr and attr.IsValid()):
                continue
            try:
                current = attr.Get()
                if parms.same(current, value) or (current is None and not value):
                    continue  # unchanged, or empty where the attribute holds nothing at all
                with Usd.EditContext(stage, layer):
                    attr.Set(value)
            except Exception as exc:  # a value the attribute's type does not take
                logger.debug("could not write %s = %r: %s", path, value, exc)


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
def _on_stage(scene: HoudiniScene, node: hou.Node):
    """The scene's stage, for one cook: a private stage over the node's input.

    ChunkLayerManager serves a single stage, and a Solaris network has one per node, so it
    is pointed at this one for the duration. Every chunk layer it hands out is cleared by
    the cook that uses it, so forgetting the last node's is not losing anything.

    Inside a chunkgroup the stage has the group's up axis and units rather than the
    input's: the chunks cook in the space their network was authored in (see space).
    Outside one it always authors both, Houdini's where the input sets none, so a chunk
    reading the stage's units sees Houdini's metres rather than USD's centimetre fallback.
    """
    input_stage = _input_stage(node)
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
    up, mpu = space.group_space(node) or space.stage_space(input_stage)
    root.pseudoRoot.SetInfo("upAxis", up)
    root.pseudoRoot.SetInfo("metersPerUnit", mpu)
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
        before = parms.names(chunk)
        with _on_stage(scene, node) as stage:
            chunk._cook()
            with _no_stage(scene):
                # Params the cook built (a Light's attributes) take their parms' values, or
                # the values import saved for them.
                parms.push(node, chunk, skip=before)
                _apply_pending(node, chunk)
            layer = chunk_registry.ChunkLayerManager().get_layer(chunk)
            out = Sdf.Layer.CreateAnonymous(f"usdchunks_{node.name()}")
            if layer is not None:
                _drive_attributes(chunk, stage, layer)
                out.TransferContent(layer)
    except hou.Error:
        raise
    except Exception as exc:
        logger.exception("chunk node %s failed to cook", node.path())
        raise hou.NodeError(f"{type(exc).__name__}: {exc}") from exc

    module = node.evalParm(parms.TYPE_PARM)
    if module == layers.EDIT_TARGET_MODULE:
        param = chunk.get_param("edit_target")
        problem = layers.check_edit_target(str(param.value or "") if param else "", _input_stage(node))
        if problem:
            raise hou.NodeError(problem)
    active = lop.editableLayer()
    layers.merge(active, out)
    if module == layers.LAYER_MODULE:
        param = chunk.get_param("layer_name")
        layers.tag(active, (str(param.value or "").strip() if param else "") or node.name())
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
    if node.userData(XFORM_OP_KEY) is None:
        node.setUserData(XFORM_OP_KEY, chunk._xform_op_suffix)  # kept through renames and reloads


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
    with _on_stage(scene, node):
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
            with _on_stage(ensure_scene(), node):
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
    node.parm(parms.VARIANT_PARM).set("/".join(entry.namespace) if entry.presets is not None else "")
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
