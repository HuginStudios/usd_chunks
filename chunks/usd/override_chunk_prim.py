"""Override Chunk Prim -- change another chunk's params from a chunk of your own.

The target is a chunk prim, usually one composed in from a weaker layer: a department's
network sublayered into a shot, say /chunks/Layout/Scatter. Pointing at it gives this chunk
the target module's params, as its own, each defaulting to the target's value. Change one
and it is saved like any param, on this chunk's prim; the params changed are the log of
what this chunk overrides. Every cook writes them onto the target as an `over` in this
chunk's own layer, so the target's file is never touched:

- delete or bypass this chunk and its layer goes, so the target is back to its own values;
- two of these on the same target are two layers, ordered like any other chunks;
- a value the target's file changes later shows through, unless it is one changed here.

The copied params run none of the target module's callbacks: they are a view of its values,
and the target does whatever its module does when it cooks with them.
"""
from __future__ import annotations

import copy
import logging

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk, chunk_registry, chunk_stage, chunk_utils

# Parented under "usd_chunks": a chunk module is imported under its own file name.
logger = logging.getLogger("usd_chunks.override_chunk_prim")

_INPUTS = "inputs:"
_CHUNK_TYPE = "info:chunkType"
_TARGET = "chunk_path"


# ---------------------------------------------------------------------------
# The target's params, as this chunk's own
# ---------------------------------------------------------------------------


def _param_path(param, node) -> str:
    """param's dotted path below node: translate_grp.tx."""
    names = []
    while param is not None and param is not node:
        if name := getattr(param, "name", None):  # not the containers between
            names.append(name)
        param = param.get_owner()
    return ".".join(reversed(names))


def _iter_values(node):
    """(dotted path, param) for every copied param holding a value: scalars and whole
    vectors, not a vector's parts or a group."""
    for name in getattr(node, "_ocp_built", ()):
        top = node.get_param(name)
        if top is None:
            continue
        stack = [top]
        while stack:
            param = stack.pop()
            if isinstance(param, chunk.Vector) or not chunk.is_group(param):
                yield _param_path(param, node), param
            else:
                stack.extend(param.get_my_params())


def _base_value(prim: Usd.Prim, name: str, own: Sdf.Layer | None):
    """The target's value for an input, leaving out what this chunk's own layer says."""
    attr = prim.GetAttribute(_INPUTS + name)
    if not attr:
        return None
    for spec in attr.GetPropertyStack(Usd.TimeCode.Default()):
        if spec.layer != own and spec.HasDefaultValue():
            return spec.default
    return None


def _as_param_value(value):
    """A USD value in the form a param holds: Gf vectors become tuples."""
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    try:
        return tuple(value)
    except TypeError:
        return value


def _build(node) -> None:
    """Give node the target module's params, defaulting to the target's values.

    The params of a previous target go first. Run when the target changes, and by the first
    cook after a load, which restores the target path without telling anyone: the load then
    reads the saved values onto them like any params made during a cook.
    """
    for name in getattr(node, "_ocp_built", ()):
        if (old := node.get_param(name)) is not None:
            old.request_delete()
    node._ocp_built = ()

    stage = node.get_scene_stage()
    path = node.get_param(_TARGET).value
    # Only the first build reads this chunk's saved changes: after a re-point they were for
    # the old target.
    first = not hasattr(node, "_ocp_built_for")
    node._ocp_built_for = path
    if not stage or not path:
        return
    prim = stage.GetPrimAtPath(Sdf.Path(path))
    type_attr = prim.GetAttribute(_CHUNK_TYPE) if prim.IsValid() else None
    module_name = type_attr.Get() if type_attr else None
    module = chunk_registry.ChunkRegistry().get_chunk_module(module_name) if module_name else None
    if module is None:
        logger.warning("Override Chunk Prim %s: %s is not a chunk prim", node.name, path)
        return
    template = next(iter(module.create() if module.loaded else module.load()), None)
    if template is None:
        return

    own = chunk_registry.ChunkLayerManager().get_layer(node)
    built = []
    for param in template.get_my_params():
        if isinstance(param, chunk.Chunk) or node.get_param(param.name) is not None:
            continue  # a nested chunk is its own, and this chunk's own params win a name
        copied = copy.copy(param)
        nested = copied.iter_hierarchy() if hasattr(copied, "iter_hierarchy") else ()
        for _, part in [(0, copied), *nested]:
            # A view of the target's values: its module's callbacks belong to the target.
            part._callbacks = type(part._callbacks)()
        node.add_param(copied)
        built.append(copied.name)
    node._ocp_built = tuple(built)

    # This chunk's own saved changes, read here rather than left to the load: the load reads
    # them only after the cook that built the params, and that cook would write nothing.
    own_prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(node)))
    for name, param in _iter_values(node):
        attr_name = name.replace(".", ":")
        base = _as_param_value(_base_value(prim, attr_name, own))
        saved_attr = own_prim.GetAttribute(_INPUTS + attr_name) if first and own_prim.IsValid() else None
        saved = _as_param_value(saved_attr.Get()) if saved_attr else None
        try:
            if base is not None:
                param.default_value = base
                param.value = base
            if saved is not None:
                param.value = saved
        except (TypeError, ValueError):
            logger.debug("Override Chunk Prim %s: could not show %s = %r", node.name, name, base)


def _on_target_changed(owner, notice, sender):
    if (node := owner.get_chunk()) and getattr(node, "_ocp_built_for", None) != owner.value:
        _build(node)


def edits(node) -> dict:
    """What this chunk overrides: {input name: value} for every param changed from the
    target's value."""
    found = {}
    for name, param in _iter_values(node):
        default = getattr(param, "default_value", None)
        if param.value != default:
            found[name.replace(".", ":")] = param
    return found


# ---------------------------------------------------------------------------
# Cook
# ---------------------------------------------------------------------------


def execute(node: chunk.Chunk) -> None:
    """Write every change onto the target chunk prim. The framework has already cleared the layer.

    That is all: the target's params are what its prim composes, so it follows this layer as
    it does any other -- as it changes, and when it goes because this chunk is deleted,
    disabled or re-pointed. The framework reads the target's prim after each.
    """
    stage = node.get_stage()
    if not stage:
        return
    target = node.get_param(_TARGET).value
    if getattr(node, "_ocp_built_for", None) != target:
        _build(node)
    _apply(node, stage, target)


def _apply(node, stage: Usd.Stage, target: str) -> None:
    """Author every change onto the target chunk prim, in the layer being cooked."""
    changed = edits(node)
    if not target or not changed:
        return
    prim = stage.GetPrimAtPath(Sdf.Path(target))
    if not prim.IsValid():
        logger.warning("Override Chunk Prim %s: no chunk prim at %s", node.name, target)
        return
    for name, param in changed.items():
        attr = prim.GetAttribute(_INPUTS + name)
        if not attr:
            # A chunk prim only carries the inputs that differ from their defaults, so a
            # change to one still at its default makes the attribute, typed from the param.
            sdf_type = chunk_stage.param_sdf_type(param)
            if sdf_type is None:
                logger.warning("Override Chunk Prim %s: no USD type for %s", node.name, name)
                continue
            attr = prim.CreateAttribute(_INPUTS + name, sdf_type)
        value = param.value
        try:
            attr.Set(chunk_utils.as_attr_value(attr, value) if isinstance(value, tuple) else value)
        except (Tf.ErrorException, TypeError, ValueError) as exc:
            logger.warning("Override Chunk Prim %s: could not set %s to %r: %s", node.name, attr.GetPath(), value, exc)


def main():
    """Build the chunk: the target chunk prim. Its params arrive when one is set."""
    myChunk = chunk.Chunk("Override Chunk Prim")

    target = chunk.LineEdit(
        _TARGET,
        "Chunk Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The chunk prim to override, e.g. /chunks/Layout/Scatter.",
    )
    myChunk.add_param(target)
    target.add_callback(_on_target_changed)

    return myChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.PROPERTIES,
        icon="tag",
        description="""
        Override Chunk Prim -- change another chunk's params from a chunk of your own.

        Parameters:
          - Chunk Prim: the chunk prim to override, usually one from a weaker layer such
            as another department's network.

        Setting it brings in that chunk's params, showing its values. Change one and it
        is written onto the target as an override in this chunk's own layer, so the
        target's file is untouched and deleting this chunk drops the change. The target
        cooks with the new values, and the chunks below it follow.
        """,
    )
