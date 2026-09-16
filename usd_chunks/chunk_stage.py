"""
The chunk graph as it lives on the stage: the API for chunk scripts and graph frontends.

A chunk script that needs to know what is wired into it, compose what is upstream of it,
or author onto its own chunk prim does it through this module. So does a frontend that
drives view-driven cooking.

Import from here, not from `usd_chunks.plugins.chunk_usd`. Most of these functions are
still implemented there and only forwarded from here. The names in this module are the
stable ones: the implementations are due to move into core, and this module is what keeps
that move from breaking any chunk that uses them.

The `is_*_chunk` predicates are implemented here already. They only read a chunk's config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from usd_chunks.chunk_flags import ChunkFlags

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from pxr import Sdf, Usd

    from usd_chunks.chunk_core import ChunkBase
    from usd_chunks.chunk_scene import SceneSender


def _usd():
    # Deferred: plugins.chunk_usd imports this module at load time.
    from usd_chunks.plugins import chunk_usd

    return chunk_usd


# ---------------------------------------------------------------------------
# What kind of chunk is this
# ---------------------------------------------------------------------------


def _module_name(chunk: "ChunkBase") -> "str | None":
    cfg = chunk.get_config()
    return cfg.module if cfg else None


def is_view_chunk(chunk: "ChunkBase") -> bool:
    """Whether chunk is a view: the marker that decides what cooks."""
    return _module_name(chunk) == "view"


def is_group_chunk(chunk: "ChunkBase") -> bool:
    """Whether chunk owns child chunks -- a CONTEXT chunk with a module.

    This is about *containment*: whose children are hidden until entered, cook as a unit,
    and share a layer. It is true of a chunkgroup, and equally true of a chunk-in-chunk
    like AMDMaterialXLibrary, whose children are an implementation detail. For the
    narrower "is this a subnet the user may navigate into", see is_chunkgroup.
    """
    return bool(chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT) and _module_name(chunk))


def is_chunkgroup(chunk: "ChunkBase") -> bool:
    """Whether chunk is a chunkgroup proper -- the `chunkgroup` chunk type.

    A chunkgroup is defined in USD and is a subnet you navigate into; a chunk is defined in
    python, and one that owns children (AMDMaterialXLibrary creating `reference` and
    `bind_material`) is reusing them as an implementation detail, not exposing a subnet to
    edit. Both carry GroupFlags.CONTEXT, so CONTEXT cannot tell them apart.

    Navigation, and the UI that advertises it, keys on this. Containment and cooking key on
    is_group_chunk, so a chunk-in-chunk keeps hiding its children and running their code.
    """
    return _module_name(chunk) == "chunkgroup"


def is_merge_chunk(chunk: "ChunkBase") -> bool:
    """Whether chunk is a Merge -- the only chunk type that takes more than one exec input."""
    return _module_name(chunk) == "merge"


def is_edit_target_chunk(chunk: "ChunkBase") -> bool:
    """Whether chunk is an Edit Target (scripts/usd/edit_target.py)."""
    return _module_name(chunk) == "edit_target"


# ---------------------------------------------------------------------------
# Where a chunk is, and what is wired to it
# ---------------------------------------------------------------------------


def get_chunk_prim_path(chunk: "ChunkBase") -> str:
    """The path of chunk's prim on the stage."""
    return _usd().get_chunk_prim_path(chunk)


def get_chunk_prim(chunk: "ChunkBase") -> "Usd.Prim | None":
    """chunk's prim on the live stage, or None if the chunk has none yet."""
    sync = _usd().ChunkStageSync()
    if not getattr(sync, "initialized", False) or not getattr(sync, "_stage", None):
        return None
    prim_path = sync._chunk_prim_paths.get(id(chunk))
    if not prim_path:
        return None
    from pxr import Sdf

    prim = sync._stage.GetPrimAtPath(Sdf.Path(prim_path))
    return prim if prim and prim.IsValid() else None


def exec_dependencies(chunk: "ChunkBase") -> "list[ChunkBase]":
    """The chunks wired into chunk's exec inputs, one per connected slot.

    Only exec wires. `chunk.get_dependencies()` also holds param wires, which mean a value
    flows rather than that one chunk runs before another.
    """
    return _usd().exec_dependencies(chunk)


def upstream_chunks(chunk: "ChunkBase") -> "set[ChunkBase]":
    """chunk and everything upstream of it on the exec chain."""
    return _usd().upstream_chunks(chunk)


def get_exec_upstream_chunks(chunk: "ChunkBase", stage: "Usd.Stage", scene: "SceneSender") -> "list[ChunkBase]":
    """Everything feeding chunk through any wire, exec or param, roots first.

    For a view this is its cook set: exactly what view-driven execution runs. chunk itself
    is not included.
    """
    return _usd().get_exec_upstream_chunks(chunk, stage, scene)


def get_exec_downstream_chunks(chunk: "ChunkBase", stage: "Usd.Stage", scene: "SceneSender") -> "set[ChunkBase]":
    """Everything chunk feeds, following exec wires forward."""
    return _usd().get_exec_downstream_chunks(chunk, stage, scene)


def find_group_active_view(group: "ChunkBase", stage: "Usd.Stage", scene: "SceneSender") -> "ChunkBase | None":
    """The view active inside group -- what decides the group's output -- or None."""
    return _usd().find_group_active_view(group, stage, scene)


def get_upstream_stage(stage: "Usd.Stage", scene: "SceneSender", chunk: "ChunkBase") -> "Usd.Stage":
    """A throwaway stage of everything except chunk and what is downstream of it.

    For reading input that a downstream chunk editing the same prim must not change.
    """
    return _usd().get_upstream_stage(stage, scene, chunk)


# ---------------------------------------------------------------------------
# Changing the graph
# ---------------------------------------------------------------------------


def connect_exec(stage: "Usd.Stage", scene: "SceneSender", src_prim_path: str, dst_prim_path: str) -> "Usd.Attribute | None":
    """Wire src's exec output into dst's exec input, for a chunk that builds its own children.

    Does not cook. The next recook of the context picks the wire up.
    """
    return _usd().connect_exec(stage, scene, src_prim_path, dst_prim_path)


def set_chunk_muted(stage: "Usd.Stage", chunk: "ChunkBase", muted: bool) -> bool:
    """Block or unblock chunk and mute or unmute its layer. True if anything changed.

    The engine's lever, not the user's: it leaves the DISABLED state alone.
    """
    return _usd().set_chunk_muted(stage, chunk, muted)


def recook_for_view(stage: "Usd.Stage", scene: "SceneSender", context: "ChunkBase | None",
                    view: "ChunkBase | None") -> None:
    """Cook view's upstream tree in context, and block and mute everything else in it.

    With view=None nothing in context cooks. A context with no view cooks nothing at all,
    so a frontend opening a graph should activate one.
    """
    _usd().recook_for_view(stage, scene, context, view)


def publish_edit_target(chunk: "ChunkBase", stage: "Usd.Stage", layer: "Sdf.Layer | None") -> None:
    """Record the layer an Edit Target chunk targets, and re-home everything downstream to it."""
    _usd().publish_edit_target(chunk, stage, layer)


def export_chunks(scene: "SceneSender", path: "str | Path") -> None:
    """Write the chunk graph to one standalone USD file."""
    _usd().export_chunks(scene, path)


# ---------------------------------------------------------------------------
# Authoring on a chunk's own prim
# ---------------------------------------------------------------------------


def chunk_prim_edit_context(chunk: "ChunkBase | None" = None) -> "AbstractContextManager":
    """An edit context for authoring on chunk's prim: ports, connections, new child chunks.

    Targets the layer chunk's bookkeeping belongs in, not the layer its cooked output goes
    to, so what is authored inside it is saved with the scene.
    """
    return _usd().ChunkStageSync()._chunk_prim_edit_context(chunk)


def clear_attr_connections(attr: "Usd.Attribute") -> None:
    """Remove attr's connections from every layer that has one, not just the edit target."""
    _usd().ChunkStageSync().clear_attr_connections(attr)


def delete_attr_specs(prim: "Usd.Prim", attr_name: str) -> None:
    """Remove attr_name from prim in every layer that has it, not just the edit target."""
    sync = _usd().ChunkStageSync()
    if getattr(sync, "initialized", False) and getattr(sync, "_stage", None):
        sync._delete_stale_attr_specs(prim, attr_name)
