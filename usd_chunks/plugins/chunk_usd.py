"""
USD I/O plugin for chunks.
Handles layer management, stage synchronization, and chunk serialization.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import math
import re
import weakref
from pathlib import Path
from typing import Generator, Iterable, Tuple

from pxr import Gf, Sdf, Usd, UsdGeom, UsdUI, UsdUtils, Vt

from usd_chunks import chunk as _chunk_mod
from usd_chunks import chunk_registry
from usd_chunks import chunk_core as core
from usd_chunks import chunk_gizmo
from usd_chunks import chunk_notice
from usd_chunks import chunk_scheduler
from usd_chunks import chunk_utils
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection, RegistrySingleton
from usd_chunks.chunk_registry import ChunkLayerManager  # noqa: F401  re-exported

logger = logging.getLogger(__name__)

_CHUNK_TYPE_ATTR = "info:chunkType"
_INPUTS_PREFIX = "inputs:"
# The ObjectsChanged field name for a connection edit; a value write reports "default".
_CONNECTION_PATHS_FIELD = "connectionPaths"
_EXEC_OUTPUT = "outputs:chunk"
_EXEC_INPUT = "inputs:chunk"
_CHUNK_PRIM_TYPENAME = "Chunk"
# Usd.Stage.Traverse()'s default predicate skips INACTIVE prims, and a disabled chunk is
# authored as exactly that (see _on_state_changed). Loading a file through the default
# predicate therefore never saw a disabled chunk at all -- it was in the file, with
# active=false recording why, and simply vanished on reopen.
_LOAD_INCLUDE_INACTIVE = Usd.PrimIsDefined & ~Usd.PrimIsAbstract
# /chunks is "bara ett scope" (Dan) -- like /lights or /render. Nothing lives at it: the
# default chunkgroup sits under it, and everything else under that. Typing it Chunk made the
# scope look like a chunk prim to anything reading the stage back.
_CHUNKS_ROOT_TYPENAME = "Scope"
_CHUNKS_ROOT = "/chunks"

# Merge chunks fan several exec branches into one node, so they need more than the single
# inputs:chunk every other chunk has. Extra slots are numbered from 1 -- slot 0 deliberately
# keeps the plain _EXEC_INPUT name so that any consumer which only ever looks up
# inputs:chunk (most of them: Edit Target resolution, exec healing, auto-chain on create)
# transparently sees the merge's PRIMARY input, which is the answer those consumers want
# anyway. Only code that genuinely needs every branch (edge drawing, tail detection,
# disconnect) has to iterate.
_EXEC_INPUT_NUMBERED = re.compile(r"^inputs:chunk([1-9]\d*)$")


def is_exec_input_name(name: str) -> bool:
    """Whether an attribute name is an exec input -- inputs:chunk, or a merge's
    inputs:chunk<N> for N >= 1."""
    return name == _EXEC_INPUT or bool(_EXEC_INPUT_NUMBERED.match(name))


def exec_input_index(name: str) -> int:
    """The slot number an exec input attribute name refers to (inputs:chunk -> 0)."""
    match = _EXEC_INPUT_NUMBERED.match(name)
    return int(match.group(1)) if match else 0


def exec_input_name(index: int) -> str:
    """The attribute name for exec slot `index`. Slot 0 is the plain inputs:chunk."""
    return _EXEC_INPUT if index == 0 else f"{_EXEC_INPUT}{index}"


def iter_exec_inputs(prim: "Usd.Prim") -> "list[Usd.Attribute]":
    """A prim's exec input attributes, ordered by slot number -- strongest branch first.

    Sorted by the PARSED index, never by attribute name: prim.GetAttributes() is
    alphabetical, which puts inputs:chunk10 before inputs:chunk2. Slot order decides which
    branch's opinion wins, so a string sort would silently mis-rank a merge with ten or
    more inputs.
    """
    if not (prim and prim.IsValid()):
        return []
    attrs = [a for a in prim.GetAttributes() if is_exec_input_name(a.GetName())]
    attrs.sort(key=lambda a: exec_input_index(a.GetName()))
    return attrs


def exec_input_connections(prim: "Usd.Prim") -> "list[Sdf.Path]":
    """Every exec connection target on prim, strongest branch first."""
    return [conn for attr in iter_exec_inputs(prim) for conn in attr.GetConnections()]

# An Edit Target chunk publishes the identifier of the layer it currently points at onto
# its own prim under this attribute. The presence of the attr marks an exec-chain boundary
# for chunk_utils.get_edit_layer's upstream walk; its value names the layer everything
# downstream of it authors into. Publishing it (rather than importing the Edit Target
# script's resolution logic into core) keeps core decoupled from the script — same
# published-marker pattern as chunk:pinned / set_chunk_pinned.
_EDIT_TARGET_MARKER = "chunk:editTargetLayer"

_NODE_STEP = 260  # horizontal spacing between nodes in the default auto-layout

# Set True by the Params-tab create path while adding a chunk, so _on_param_added inserts the new
# chunk before the active view (view-follows-tail). Node-graph creates leave it False — there the
# user wires manually.
_params_tab_create = False

# Auto-pin decision for the next chunk(s) about to be created, or None when no create/paste flow
# is active (idle — e.g. during a scene load). Tri-state, not a plain bool, to fix a real race:
# the Params-page widget's PARAM_ADDED listener can run BEFORE ChunkStageSync's own listener (both
# react to the same notice; delivery order isn't guaranteed), so the widget may ask is_chunk_pinned()
# before _define_prim has written chunk:pinned at all. is_chunk_pinned() falls back to this flag
# for a chunk with no authored value yet — but only distinguishing "mid-creation, going to author
# False" (this flag is True/False) from "not creating anything right now, this is a genuinely old
# chunk with no attribute" (this flag is None, so the absent-means-pinned migration default holds)
# requires the None state; a plain bool defaulting False can't tell those two apart.
# True: created via the Params tab's own create menu, or pasted/duplicated while Params is showing.
# False: created via the Graph page's create menu, or pasted/duplicated while Graph is showing.
# Kept separate from _params_tab_create — that flag also drives view-follows-tail auto-chaining,
# which paste must NOT pick up (it already copies the source's own wiring explicitly).
_pin_new_chunks: "bool | None" = None

# Set by the node graph to the USD-space (x, y) a new chunk node should be created at (e.g. the
# right-click / wire-drop point). _define_prim reads it for the node pos instead of the far-away
# index default. None → use the default. Cleared by the caller after the create.
_create_node_pos: "tuple[float, float] | None" = None

# Just above float32's ~1e-7 relative precision. A param holds a Python float (64-bit) but
# is stored in USD as a 32-bit float, so a value that made the round trip comes back
# differing in the 8th significant digit -- e.g. param -3.3492076146840537 vs USD
# -3.349207639694214, seen live. Comparing those exactly makes them permanently unequal, so
# every notice mentioning the attribute would write the param again; harmless per write, but
# it dirties the chunk and cooks it, and resyncs are frequent enough for that to matter.
_FLOAT_AGREEMENT = 1e-6


def _values_agree(usd_value, param_value) -> bool:
    """Whether a value read back from USD is the same as the param's, allowing for float32.

    Falls back to plain equality for anything not a pair of numbers -- strings, tokens,
    bools -- where exactness is the whole point.
    """
    # Strings first, and not merely for speed: a str is iterable, and iterating one yields
    # 1-character strs that are iterable in turn, so falling through to the sequence branch
    # below recurses until the stack runs out. Equal-length strings are the trigger --
    # different lengths raise from strict=True and land on plain equality, which is why a
    # test comparing "/World/Cube" with "/World/Sphere" passes while any prim path compared
    # against another of the same length brings down whatever is holding the notice.
    if isinstance(usd_value, (str, bytes)) or isinstance(param_value, (str, bytes)):
        return usd_value == param_value
    if isinstance(usd_value, bool) or isinstance(param_value, bool):
        return usd_value == param_value
    if isinstance(usd_value, (int, float)) and isinstance(param_value, (int, float)):
        return math.isclose(usd_value, param_value, rel_tol=_FLOAT_AGREEMENT, abs_tol=_FLOAT_AGREEMENT)
    # Vectors and other sequences: compare component-wise under the same rule.
    try:
        pairs = list(zip(usd_value, param_value, strict=True))
    except (TypeError, ValueError):
        return usd_value == param_value
    return all(_values_agree(a, b) for a, b in pairs)


_SDF_TYPE_MAP = {
    bool: Sdf.ValueTypeNames.Bool,
    int: Sdf.ValueTypeNames.Int,
    float: Sdf.ValueTypeNames.Float,
    str: Sdf.ValueTypeNames.String,
    Gf.Matrix4d: Sdf.ValueTypeNames.Matrix4d,
}

_VECTOR_TYPE_MAP: dict = {
    (float, 2): (Sdf.ValueTypeNames.Float2, Gf.Vec2f),
    (float, 3): (Sdf.ValueTypeNames.Float3, Gf.Vec3f),
    (float, 4): (Sdf.ValueTypeNames.Float4, Gf.Vec4f),
    (int, 2): (Sdf.ValueTypeNames.Int2, Gf.Vec2i),
    (int, 3): (Sdf.ValueTypeNames.Int3, Gf.Vec3i),
    (int, 4): (Sdf.ValueTypeNames.Int4, Gf.Vec4i),
}

_LIST_SDF_TYPE_MAP = {
    bool: Sdf.ValueTypeNames.BoolArray,
    int: Sdf.ValueTypeNames.IntArray,
    float: Sdf.ValueTypeNames.FloatArray,
    str: Sdf.ValueTypeNames.StringArray,
}

_LIST_VT_MAP = {
    bool: Vt.BoolArray,
    int: Vt.IntArray,
    float: Vt.FloatArray,
    str: Vt.StringArray,
}


def _sdf_vector_type(val: tuple) -> tuple:
    """Return (Sdf type, Gf value) for a vector tuple, or (None, None) if unsupported."""
    non_none = [v for v in val if v is not None]
    if not non_none:
        return None, None
    entry = _VECTOR_TYPE_MAP.get((type(non_none[0]), len(val)))
    if entry is None:
        return None, None
    sdf_type, gf_cls = entry
    return sdf_type, gf_cls(*[v if v is not None else 0 for v in val])


def _sdf_type_and_value(val):
    """Return (Sdf value type, USD-safe value) for val, or (None, None) if unsupported."""
    if isinstance(val, tuple):
        return _sdf_vector_type(val)
    if isinstance(val, list):
        elem_type = type(val[0]) if val else str
        sdf_type = _LIST_SDF_TYPE_MAP.get(elem_type)
        vt_cls = _LIST_VT_MAP.get(elem_type)
        if sdf_type is None or vt_cls is None:
            return None, None
        return sdf_type, vt_cls(val)
    sdf_type = _SDF_TYPE_MAP.get(type(val))
    return sdf_type, val


def sdf_type_for_param(param) -> "Sdf.ValueTypeName | None":
    """The USD type an attribute backing this param should have, or None if unmappable.

    Typed from the param's DEFAULT rather than whatever it currently holds. A live value
    can be an unresolved expression string, or something that arrived over a wire, and an
    attribute's type is fixed for good once it is created -- so typing from the value in
    hand risks pinning a float param to String and failing every numeric write afterwards.
    The default is the one value that always reflects what the param actually is.
    """
    if param is None:
        return None
    try:
        if ChunkFlags.ParamType.COLOR_PICKER in param.evaluate_type():
            # A ColorPicker holds an (r, g, b) tuple; _SDF_TYPE_MAP is keyed by Python type
            # and has no tuple entry, so it needs saying explicitly.
            return Sdf.ValueTypeNames.Color3f
    except Exception:
        pass
    type_source = getattr(param, "default_value", None)
    if type_source is None:
        type_source = getattr(param, "raw_value", None)
    sdf_type, _ = _sdf_type_and_value(type_source)
    return sdf_type


class ChunkStageSync(metaclass=RegistrySingleton):
    """
    Singleton that authors a Chunk prim on the stage whenever a Python chunk
    is added to the scene, then wires each inputs: attribute to the matching
    param via connect_usd_attr. Keeps the stage live so any USD tool can save
    and capture current param state.
    """

    initialized: bool = False
    # True only while reload_for_stage tears down the previous file's chunks — see
    # _on_chunk_deleted, which must not touch USD during that window.
    _reloading: bool = False

    # True while reconcile_exec_dependencies runs off an ObjectsChanged notice, i.e. while
    # the DAG is being mirrored FROM the stage. Stops the mirror writing back: adding the
    # edge fires DEPENDENCY_ADDED, whose handler would author inputs:chunk and re-home edit
    # targets, both of which edit the stage and notify straight back here.
    _reconciling_exec: bool = False

    def initialize(self, stage: Usd.Stage, scene: "core.SceneSender") -> None:
        self._stage = stage
        self._scene = scene
        self._reloading = False
        self._loaded_stage = None
        self._loaded_prim_paths: set[str] = set()
        self._chunk_prim_paths: dict[int, str] = {}  # id(chunk) → current prim path
        # What USD has said changed since the last scan for new chunk prims, used to scope
        # that scan — see _on_objects_changed and _scan_for_new_chunks. Resyncs are kept
        # apart from info-only changes because they justify different amounts of looking.
        self._pending_resyncs: "set[Sdf.Path]" = set()
        self._pending_info_changes: "set[Sdf.Path]" = set()
        self._attr_changed_listener = NoticeConnection(
            self._on_attr_changed, ChunkNoticeAlias.ATTRIBUTE_CHANGED, globally=True
        )
        self._param_added_listener = NoticeConnection(
            self._on_param_added, ChunkNoticeAlias.PARAM_ADDED, globally=True
        )
        self._dependency_added_listener = NoticeConnection(
            self._on_dependency_added, ChunkNoticeAlias.DEPENDENCY_ADDED, globally=True
        )
        self._objects_changed_listener = NoticeConnection(
            self._on_objects_changed, ChunkNoticeAlias.OBJECT_CHANGED, globally=True
        )
        self._chunk_deleted_listener = NoticeConnection(
            self._on_chunk_deleted, ChunkNoticeAlias.CHUNK_DELETED, globally=True
        )
        self._state_changed_listener = NoticeConnection(
            self._on_state_changed, ChunkNoticeAlias.STATE_CHANGED, globally=True
        )
        self._contents_changed_listener = NoticeConnection(
            self._on_contents_changed, ChunkNoticeAlias.STAGE_CONTENTS_CHANGED, globally=True
        )
        self._chunk_loaded_listener = NoticeConnection(
            self._on_chunk_loaded, ChunkNoticeAlias.CHUNK_LOADED, globally=True
        )

        saved_targets: "dict[str, str]" = {}
        if stage:
            self._loaded_stage = stage
            # Before load_chunks: rebuilding an Edit Target whose layer doesn't exist yet
            # overwrites its saved selection in USD (see snapshot_edit_target_selections).
            saved_targets = snapshot_edit_target_selections(stage)
            load_chunks(stage, scene, self._loaded_prim_paths)
        self.initialized = True
        if stage:
            restore_edit_target_selections(stage, scene, saved_targets)
            # Same reason as the in-load pass above: opening a file must not rewrite it.
            reconcile_edit_targets(stage, scene, protect_root_layer=True)

    def _chunk_prim_edit_context(self, owner: "core.ChunkBase | None" = None):
        """Return an EditContext for authoring a *new* chunk-prim bookkeeping attr spec
        (inputs:/outputs:/exec connections, node position, etc.).

        Targets chunk_utils.get_bookkeeping_layer(stage, owner) — the Edit Target resolved
        for the chunk's exec-chain position (nearest_upstream_edit_layer) for a regular
        chunk, or always the session layer for a CONTEXT chunk (Tab containers, groups)
        regardless of what's active, since letting a CONTEXT chunk's own bookkeeping follow
        a target would split its prim across layers over its lifetime. owner=None (caller
        couldn't resolve one) also falls back to session via get_bookkeeping_layer.

        Only for *first-time* attr/prim creation, where "where should this new content
        go" is the right question. Updating an *existing* attr's value must instead
        target wherever its spec already lives (see chunk_core.py's update_usd_prop /
        data_changed) — re-resolving the current target on every value change would
        scatter a single attr's opinions across layers as Edit Target moves on.
        """
        if not (self.initialized and self._stage):
            return contextlib.nullcontext()
        return Usd.EditContext(self._stage, chunk_utils.get_bookkeeping_layer(self._stage, owner))

    def clear_attr_connections(self, attr: Usd.Attribute) -> None:
        """Clear connections from a chunk prim attr in every layer where a connection
        opinion actually exists — not just the current edit layer.

        A connection may have been authored while a different Edit Target was active
        (or before any Edit Target chunk ran at all); clearing only the current edit
        layer would silently no-op, leaving that original opinion composed in as a stale,
        seemingly-undeletable connection.
        """
        if not (self.initialized and self._stage):
            return
        for layer in self._stage.GetLayerStack(includeSessionLayers=True):
            spec = layer.GetAttributeAtPath(attr.GetPath())
            if spec and spec.connectionPathList.explicitItems:
                with Usd.EditContext(self._stage, layer):
                    attr.ClearConnections()

    def _expr_connections(self, prim: Usd.Prim, raw: str) -> list:
        """Build Sdf.Path connection targets for an expression string, preferring outputs: over inputs:."""
        chunk_root = str(prim.GetParent().GetPath())
        return [
            Sdf.Path(
                f"{chunk_root}/{node}.outputs:{attr_name}"
                if self._stage.GetPrimAtPath(Sdf.Path(f"{chunk_root}/{node}")).GetAttribute(f"outputs:{attr_name}").IsValid()
                else f"{chunk_root}/{node}.inputs:{attr_name}"
            )
            for node, attr_name in chunk_utils.parse_expression(raw)
        ]

    def _clear_driven_attr_value(self, driven_attr: Usd.Attribute) -> None:
        """Remove any authored default value from the driven attr in session/anonymous layers.

        After a param is connected to a chunk-prim storage attr, the light prim attr
        should carry only the connection spec — no value opinion — so that the
        connection is the sole driver (matching how inputs:radius is authored).
        """
        if not self._stage:
            return
        path = driven_attr.GetPath()
        # The default may have been authored in whatever layer was the edit target at the
        # time — clear it wherever it exists across the stack, not just one tracked layer.
        for layer in self._stage.GetLayerStack(includeSessionLayers=True):
            spec = layer.GetAttributeAtPath(path)
            if spec and spec.HasDefaultValue():
                spec.ClearDefaultValue()

    def _on_contents_changed(self, notice, sender):
        # Before the _usd_write_active bail-out: a suppressed write is still a write, and
        # sublayer edits (which is most of what runs under that flag) decide whether an Edit
        # Target marker's layer is composed at all. Muting also arrives only here — USD
        # reports a mute with no changed paths at all, so no finer signal exists.
        invalidate_edit_layer_memo()
        if chunk_notice._usd_write_active or not self._scene:
            return
        stage = notice.GetStage()
        if not stage:
            return
        scene_stage = self._scene.get_stage()
        if not scene_stage or stage.GetRootLayer().identifier != scene_stage.GetRootLayer().identifier:
            return
        self._stage = stage
        self._scan_for_new_chunks(stage)

    def _scan_for_new_chunks(self, stage: Usd.Stage) -> None:
        """Turn any chunk prim that has appeared since the last scan into a chunk.

        Scoped to the subtrees ObjectsChanged reported resynced, rather than the whole
        stage. A prim cannot appear outside a resynced subtree, so this looks everywhere it
        needs to and nowhere else — which matters twice over: the scan runs on every stage
        change, so walking the whole stage made building a graph quadratic in the number of
        chunks, and in a real scene it also walked every piece of geometry on the stage to
        find out that nothing had happened.

        A resync is a structural change, so a prim that was not there before can be
        anywhere beneath one — those get walked. An info-only change cannot create a prim,
        but it can still make an existing one loadable, which is not a hypothetical: a
        chunk prim becomes loadable when its info:chunkType gets a value, and setting a
        value is an info-only change. So those contribute the one prim they name, and
        deliberately not its subtree — otherwise a metadata change on a big prim would drag
        its whole subtree through here.

        Having heard nothing at all is the one case with no basis to narrow, and walks the
        stage. State is cleared only here, where the scan actually happens: the callers bail
        out ahead of this on a suppressed write, and whatever they saw still has to be
        looked at on the next real one.
        """
        resyncs, info_changes = self._pending_resyncs, self._pending_info_changes
        self._pending_resyncs = set()
        self._pending_info_changes = set()
        prims = None
        if resyncs or info_changes:
            prims = _iter_scoped_prims(stage, resyncs, info_changes)
        load_chunks(stage, self._scene, self._loaded_prim_paths, prims=prims)

    def _on_chunk_deleted(self, notice, sender: "core.ChunkBase") -> None:
        if not self._stage or not isinstance(sender, core.ChunkBase):
            return
        if self._reloading:
            # File > Open: these chunks belong to the file being *replaced*, but self._stage
            # is already the newly opened one. Everything below addresses prims by path, and
            # the outgoing graph's paths routinely collide with the incoming file's (both
            # sides came from the same tab, e.g. /Tab1/Prim) — so the teardown would delete
            # the new file's own prim specs and heal_exec_chain_on_delete would rewire its
            # exec chain, all before load_chunks ever reads it. Nothing to unauthor either
            # way: the outgoing stage is being discarded whole.
            self._loaded_prim_paths.discard(self._chunk_prim_paths.get(id(sender), ""))
            self._chunk_prim_paths.pop(id(sender), None)
            return
        if sender.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            if not getattr(sender, "prim_path", None):
                return  # tab context — no USD prim to remove
            sdf_path = Sdf.Path(sender.prim_path)
            chunk_root = str(sdf_path.GetParentPath())
        else:
            # request_delete clears the owner before firing CHUNK_DELETED, so get_context()
            # (and thus _resolve_chunk_root) is None here and would resolve to the wrong root
            # (/chunks instead of e.g. /Tab1). Use the path recorded when the prim was authored;
            # only recompute if we never tracked it.
            stored = self._chunk_prim_paths.get(id(sender))
            if stored:
                sdf_path = Sdf.Path(stored)
                chunk_root = str(sdf_path.GetParentPath())
            else:
                chunk_root = _resolve_chunk_root(sender)
                sdf_path = Sdf.Path(f"{chunk_root}/{sender.name}")
        prim_path_str = str(sdf_path)
        self._loaded_prim_paths.discard(prim_path_str)
        self._chunk_prim_paths.pop(id(sender), None)

        # Heal the exec chain while the prim (and its inputs:chunk predecessor) still exists:
        # deleting a middle node A -> X -> B must leave A -> B, not a broken chain.
        heal_exec_chain_on_delete(self._stage, prim_path_str)

        # The nameChildren edits below are raw Sdf writes that fire StageContentsChanged
        # like any other stage edit. Without this guard, deleting a context with several
        # children re-enters _on_contents_changed -> load_chunks after every single child
        # removal (mid-cascade, before the remaining siblings are deleted), causing a full
        # stage rescan per child instead of once for the whole delete.
        prev_w = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            for layer in self._stage.GetLayerStack(includeSessionLayers=True):
                parent = layer.GetPrimAtPath(sdf_path.GetParentPath())
                if parent and sdf_path.name in parent.nameChildren:
                    del parent.nameChildren[sdf_path.name]

            # Tidy up the chunks root once its last chunk goes -- an empty /chunks prim is
            # bookkeeping nobody wants left behind. ONLY that root, though: a chunk inside a
            # chunkgroup has the GROUP as its root, so this used to delete the group's own
            # prim as soon as its last child went. An empty group is still a group, and it
            # vanishing took the wiring with it -- deleting a wired group deletes its
            # contents first, and the last of those pulled the group's prim out from under
            # heal_exec_chain_on_delete, which then bailed on its IsValid() guard and left a
            # downstream view connected to a prim that no longer existed.
            root_sdf = Sdf.Path(chunk_root)
            root_prim = self._stage.GetPrimAtPath(root_sdf)
            if chunk_root == _CHUNKS_ROOT and root_prim.IsValid() and not any(
                child.GetTypeName() == _CHUNK_PRIM_TYPENAME for child in root_prim.GetChildren()
            ):
                for layer in self._stage.GetLayerStack(includeSessionLayers=True):
                    root_parent = layer.GetPrimAtPath(root_sdf.GetParentPath())
                    if root_parent and root_sdf.name in root_parent.nameChildren:
                        del root_parent.nameChildren[root_sdf.name]
        finally:
            chunk_notice._usd_write_active = prev_w

        # Deleting an Edit Target chunk changes the target that now applies to everything
        # that was downstream of it (heal_exec_chain_on_delete has just rewired the chain
        # around it) — re-home those chunks to the target that survives at their position.
        reconcile_edit_targets(self._stage, self._scene)

    def _on_state_changed(self, notice, sender: "core.ChunkBase") -> None:
        if not self._stage or not isinstance(sender, core.ChunkBase):
            return
        prim_path = self._chunk_prim_paths.get(id(sender))
        if not prim_path:
            chunk_root = _resolve_chunk_root(sender)
            prim_path = f"{chunk_root}/{sender.name}"
        disabled = sender.has_state(ChunkFlags.State.DISABLED)
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not prim.IsValid():
            # A chunk under a deactivated ancestor is not in the composed scene at all, so
            # there is no prim to ask -- and a group re-enables its children before its own
            # prim comes back, so this is exactly when they are told to wake up. Returning
            # here left their authored active=false in place, and the group came back with
            # its contents still switched off. Clear it on the spec directly instead.
            if not disabled:
                prev_w = chunk_notice._usd_write_active
                chunk_notice._usd_write_active = True
                try:
                    for layer in self._stage.GetLayerStack(includeSessionLayers=True):
                        spec = layer.GetPrimAtPath(Sdf.Path(prim_path))
                        if spec is not None and spec.HasInfo("active"):
                            spec.ClearInfo("active")
                finally:
                    chunk_notice._usd_write_active = prev_w
            return
        # Compare against what is authored ON THIS PRIM, not the composed value, and for the
        # same reason the mirror does: a chunk inside a disabled group composes inactive
        # without anything being authored on it. Reading the composed value here left an
        # enabled chunk with a stale authored active=false -- the group came back but its
        # contents stayed switched off, because IsActive() already agreed with the new state
        # and the write was skipped.
        authored_active = prim.GetMetadata("active") if prim.HasAuthoredActive() else True
        if authored_active == disabled:
            # Mark this as our own write. Without the guard the resulting ObjectsChanged is
            # read back as if a user had deactivated the prim on the stage, and the mirror in
            # _on_objects_changed answers by rewriting the chunk's state -- which is how
            # disabling a chunkgroup ended up clearing the group's OWN disabled flag. With
            # the flag gone, re-enabling was a no-op: the group never saw a DISABLED->enabled
            # transition, so it never told its children to come back (live-reported: the
            # chunks inside stayed dead until you dived in and selected them).
            prev_w = chunk_notice._usd_write_active
            chunk_notice._usd_write_active = True
            try:
                prim.SetActive(not disabled)
            finally:
                chunk_notice._usd_write_active = prev_w

    def _on_objects_changed(self, notice: Usd.Notice.ObjectsChanged, sender):
        # As in _on_contents_changed: this runs before the guards, since a prim appearing or
        # an inputs:chunk connection changing is exactly what the memo is keyed against, and
        # those arrive here regardless of which flag is set.
        invalidate_edit_layer_memo()
        # Same reasoning, and the only place the resynced paths are visible at all: the scan
        # for new chunk prims runs off StageContentsChanged, which carries no paths. USD
        # sends this notice first for a given change (locked in by
        # test_objects_changed_precedes_stage_contents_changed), so what is recorded here is
        # what that scan then scopes itself by. A write suppressed by the guards below still
        # has to be recorded — suppression stops us reacting now, not the prims existing.
        self._pending_resyncs.update(notice.GetResyncedPaths())
        self._pending_info_changes.update(notice.GetChangedInfoOnlyPaths())
        if chunk_notice._usd_write_active or self._reconciling_exec or not self._scene:
            return
        # Resolve the stage from the notice itself, like _on_contents_changed does, rather
        # than trusting the raw `sender` argument -- RegisterGlobally's sender has been
        # observed to arrive as None here (unlike in isolation), crashing on
        # sender.GetRootLayer() below. notice.GetStage() is the same battle-tested fallback
        # _on_contents_changed already uses for exactly this reason.
        stage = notice.GetStage()
        if not stage:
            return
        scene_stage = self._scene.get_stage()
        if not scene_stage or stage.GetRootLayer().identifier != scene_stage.GetRootLayer().identifier:
            return
        self._stage = stage
        changed = {str(p) for p in notice.GetChangedInfoOnlyPaths()}
        # Include resynced paths for editors (e.g. usdview) that author active via the layer
        all_changed = changed | {str(p) for p in notice.GetResyncedPaths()}
        # NOT extended with paths from notices that the guard above suppressed, though the
        # temptation is strong: a change arriving mid-write is genuinely lost, which is a
        # candidate for "undo moved the prim every step but the sliders followed only about
        # half the time". Replaying them is unsound. A suppressed notice means WE were
        # writing, and a multi-component write leaves USD transiently holding partial
        # values -- replaying such a path afterwards pushed a stale (1, 0, 0) over a param
        # that already correctly held (1, 2, 3). That is precisely what the guard is for.
        # Fixing the dropped-undo case needs the drain to happen where USD is known
        # consistent (as the write block closes), not on the next unrelated notice.
        param_paths = all_changed
        if not all_changed:
            return
        self._reconcile_changed_exec_inputs(all_changed, notice)
        for chunk in self._scene.get_chunks():
            # all_changed, not just the info-only set. An ordinary edit -- typing in a field,
            # a gizmo writing a param -- arrives as an info-only change, but undo restores
            # layer content wholesale and USD reports THAT as a resync. Reading only the
            # info-only set meant precisely the edits a user cannot make by hand (undo, redo,
            # a layer reload) were the ones the mirror ignored: Ctrl+Z moved the prim back
            # while the chunk's sliders kept the undone value, and only caught up when the
            # gizmo was next touched. Confirmed live -- a real Ctrl+Z reports
            # `<chunk>.inputs:translate_grp:tz` as resynced, with nothing in the info-only set.
            if param_paths:
                for _, param in _iter_chunk_params(chunk):
                    data_path = getattr(param, "_param_attr_path", None)
                    if not data_path or data_path not in param_paths:
                        continue
                    attr = stage.GetAttributeAtPath(Sdf.Path(data_path))
                    if attr and attr.IsValid():
                        val = attr.Get(0)
                        raw = getattr(param, 'raw_value', None)
                        if (val is not None and not _values_agree(val, param.value)
                                and not (isinstance(raw, str) and chunk_utils.has_expression(raw))):
                            param.value = val
                for _, vector in _iter_chunk_vectors(chunk):
                    data_path = getattr(vector, "_param_attr_path", None)
                    if not data_path or data_path not in param_paths:
                        continue
                    attr = stage.GetAttributeAtPath(Sdf.Path(data_path))
                    if attr and attr.IsValid():
                        val = attr.Get(0)
                        # Same expression guard the scalar branch above has always had.
                        # Missing here, which went unnoticed while this only ran for
                        # info-only paths: writing the resolved value over the expression
                        # replaces a live link with a frozen number, and the vector stops
                        # tracking whatever it was pointed at.
                        raw = getattr(vector, 'raw_value', None)
                        if (val is not None and not _values_agree(val, vector.value)
                                and not (isinstance(raw, str) and chunk_utils.has_expression(raw))):
                            vector.value = val
            chunk_prim_path = self._chunk_prim_paths.get(id(chunk))
            if not chunk_prim_path or chunk_prim_path not in all_changed:
                continue
            prim = stage.GetPrimAtPath(Sdf.Path(chunk_prim_path))
            if not prim.IsValid():
                continue
            # This chunk's OWN activation, not the composed one. Deactivating a chunkgroup
            # deactivates its descendants too, and that is inherited -- nothing is authored
            # on the children. Reading prim.IsActive() here made every inner chunk latch a
            # DISABLED state it never had, and re-enabling the group then reactivated only
            # the group's prim: each inner chunk wrote its own active=false straight back
            # from that latched state, so the contents stayed dead (live-reported).
            #
            # A chunk that really is disabled has active=false authored on its own prim --
            # that is what _on_state_changed writes -- so this still sees a genuine deactivate,
            # including one done externally on the stage.
            is_active = prim.GetMetadata("active") if prim.HasAuthoredActive() else True
            is_disabled = chunk.has_state(ChunkFlags.State.DISABLED)
            if not is_active and not is_disabled:
                chunk.set_state(ChunkFlags.State.DISABLED)
            elif is_active and is_disabled:
                chunk.unset_state(ChunkFlags.State.DISABLED)

    def _reconcile_changed_exec_inputs(self, all_changed: "set[str]", notice) -> None:
        """Mirror any changed inputs:chunk connection into the Python DAG.

        A connection edit is reported against the *attribute* path
        (/chunks/b.inputs:chunk), not the prim — verified against Usd.Notice.ObjectsChanged,
        which puts it in GetChangedInfoOnlyPaths(). The rest of _on_objects_changed matches
        on prim paths only, which is why this wiring went unnoticed there for so long.

        Routing this through the stage notice rather than the node graph's
        _author_connection keeps every authoring path in sync — the Qt graph, the CLI, a
        USD-level edit in usdview, and any future frontend — instead of each one having to
        remember to call add_dependency itself. Merge slot compaction rides here for exactly
        the same reason: every way a slot can be cleared (a port disconnect, a shake, an
        upstream chunk being deleted, a hand edit in usdview) surfaces as this one notice, so
        the invariant is enforced once instead of at each call site that can break it.
        """
        touched_prims = set()
        for path in all_changed:
            # A property path is "<prim>.<attr>"; prim paths contain no dot at all. Matching
            # the attribute name (rather than the old endswith(".inputs:chunk")) is what lets
            # a merge's numbered slots through — inputs:chunk1 does NOT end with
            # ".inputs:chunk", so before this every branch past the first authored USD, drew
            # an edge and cooked in the right order while its Python DAG edge was never
            # created: no INPUT_CHANGED, so editing that branch never recooked the merge.
            prim_path, sep, attr_name = path.partition(".")
            if not sep or not attr_name.startswith(_INPUTS_PREFIX):
                continue
            # Any inputs: connection, not only the exec slots: a param wire is a real
            # chunk-level dependency for ordering too, and drag-to-connect authors it the
            # same way. _live_chunk_dependencies decides what actually counts.
            if not is_exec_input_name(attr_name):
                # But only when the *connection* moved. A param's stored value lives under
                # this same inputs: prefix, so without this every keystroke into any param
                # would reconcile the whole scene -- a value write reports the "default"
                # field, a wire reports "connectionPaths". No fields at all means the path
                # arrived by resync, where the connection may genuinely have moved, so it
                # is let through rather than guessed at.
                fields = notice.GetChangedFields(Sdf.Path(path))
                if fields and _CONNECTION_PATHS_FIELD not in fields:
                    continue
            touched_prims.add(prim_path)
        if not touched_prims:
            return

        chunks_by_prim = {prim_path: chunk_id for chunk_id, prim_path in self._chunk_prim_paths.items()}
        wanted_ids = {chunks_by_prim[p] for p in touched_prims if p in chunks_by_prim}
        if not wanted_ids:
            return

        self._reconciling_exec = True
        try:
            # Compact BEFORE reconciling: compaction renames connections between slots, and
            # the DAG must be reconciled against the settled result, not the torn state.
            for prim_path in touched_prims:
                normalize_merge_slots(self._stage, self._stage.GetPrimAtPath(Sdf.Path(prim_path)))
            for chunk in self._scene.get_chunks():
                if id(chunk) in wanted_ids:
                    reconcile_exec_dependencies(chunk)
        finally:
            self._reconciling_exec = False

    def _teardown_chunks(self) -> None:
        """Drop every chunk in the scene, Python-side only.

        _reloading keeps _on_chunk_deleted off the stage: by the time this runs self._stage
        is the stage being switched TO, and the outgoing chunks' prim paths routinely match
        the incoming file's, so the usual per-chunk prim removal would delete the new file's
        own content. The outgoing stage is discarded whole anyway, so there is nothing to
        unauthor on either side.
        """
        self._reloading = True
        try:
            for ctx in list(self._scene.chunks.get_my_params()):
                ctx.request_delete()
        finally:
            self._reloading = False
        self._loaded_prim_paths.clear()
        self._chunk_prim_paths.clear()

    def reload_for_stage(self, stage: Usd.Stage) -> None:
        """Reinitialize for a newly opened stage (File > Open).  Safe to call from a
        deferred context (e.g. QTimer.singleShot) once the stage has fully settled.

        Compares stage *object identity*, not the root layer's identifier string —
        re-exporting to the same path and reloading (a common iteration workflow)
        gives a genuinely new Usd.Stage with the same file path, so comparing
        identifiers would wrongly skip the reset here, leaving the previous file's
        chunk objects (and their now-orphaned bookkeeping/positions) still registered
        and silently re-wrapped alongside the freshly loaded content.
        """
        if not self._scene:
            return
        if stage is None:
            # The host dropped its stage — usdview clears it before opening the next file,
            # notifying twice (None, then the new stage). Drop the graph now instead of
            # waiting for the second notice: it belongs to a stage that no longer exists,
            # and usdview keeps pumping the Qt event loop across the gap (it even clears
            # the prim selection in there), so anything the previous graph still reacts to
            # — a selection sync, a deferred rebuild, a click — ran against no stage at all.
            self._teardown_chunks()
            self._loaded_stage = None
            self._stage = None
            return
        if stage is self._loaded_stage:
            return
        self._loaded_stage = stage
        self._stage = stage
        chunk_registry.ChunkLayerManager().initialize(stage)
        self._teardown_chunks()
        # Snapshot before load_chunks — the load itself overwrites any selection whose layer
        # isn't back yet (see snapshot_edit_target_selections).
        saved_targets = snapshot_edit_target_selections(stage)
        load_chunks(stage, self._scene, self._loaded_prim_paths)
        # Re-apply Edit Target selections dropped during load (Menu rejected them before their
        # layers existed), then consolidate any chunk whose saved home doesn't match its
        # topology-resolved target (e.g. a file authored before Edit Target became position-based).
        restore_edit_target_selections(stage, self._scene, saved_targets)
        reconcile_edit_targets(stage, self._scene)

    def _on_attr_changed(self, notice, sender):
        if not self.initialized or not self._stage:
            return
        attr_sender = notice.data
        if not isinstance(attr_sender, core.AttributeSender):
            return

        if attr_sender.name == "value" and not chunk_notice._usd_write_active:
            owner = attr_sender.get_owner()
            if owner is not None:
                self._sync_list_proxy_to_usd(owner)
                if isinstance(owner, core.ChunkParamBase):
                    chunk = owner.get_chunk()
                    if chunk and id(chunk) in self._chunk_prim_paths:
                        self._sync_param_input(chunk, owner)
                        self._refresh_gizmo_anchors(chunk)
                elif isinstance(owner, core.ChunkVectorBase):
                    chunk = owner.get_chunk()
                    if chunk and id(chunk) in self._chunk_prim_paths:
                        self._sync_vector_input(chunk, owner)
            return

        if attr_sender.name != "name":
            return
        old_name = attr_sender.old_value
        owner = attr_sender.get_owner()
        if not old_name or old_name == getattr(owner, "name", None):
            return

        if isinstance(owner, core.ChunkBase):
            old_path = self._chunk_prim_paths.get(id(owner))
            if not old_path:
                return  # chunk not yet registered — name collision during creation, not a user rename
            # Rename in place (sibling): keep the prim's parent, swap the leaf name. Do NOT use
            # _resolve_chunk_root — for a group chunk get_context() returns self, so its root is the
            # group's own prim, which would nest the rename inside itself (/G -> /G/newname).
            new_path = str(Sdf.Path(old_path).GetParentPath().AppendChild(owner.name))
            self._handle_rename(
                old_path=old_path,
                new_path=new_path,
                old_name=old_name,
                new_name=owner.name,
                chunk=owner,
            )
            self._chunk_prim_paths[id(owner)] = new_path
            if owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                # Renaming a group renames its whole subtree (BatchNamespaceEdit renames
                # descendants too). Update the group's prim_path and rebase every descendant's
                # tracked path so /old/... becomes /new/...
                owner.prim_path = new_path
                self._rebase_descendant_paths(old_path, new_path)
        elif isinstance(owner, core.ChunkParamBase):
            self._handle_param_rename(owner, old_name, owner.name)

    def _handle_rename(self, old_path: str, new_path: str, old_name: str, new_name: str, chunk=None) -> None:
        """Rename a chunk prim and patch all references to it.

        Renames the prim in every layer that actually authors it. If the prim can't be
        resolved to any spec, fall back to the stage's current edit target rather than bail
        (bailing left the chunk renamed but the prim not, so the node vanished).
        """
        if not self._stage:
            return
        old_prim = self._stage.GetPrimAtPath(Sdf.Path(old_path))
        target_layers = []
        if old_prim and old_prim.IsValid():
            for spec in old_prim.GetPrimStack():
                if spec.layer not in target_layers:
                    target_layers.append(spec.layer)
        # Fallback if the prim couldn't be resolved: the stage's current edit target.
        if not target_layers:
            fallback = self._stage.GetEditTarget().GetLayer()
            if fallback is not None:
                target_layers = [fallback]
        if not target_layers:
            return

        # Guard the whole rename as a chunk-driven write. Applying the namespace edit fires
        # StageContentsChanged synchronously; without this, _on_contents_changed re-enters
        # load_chunks before _loaded_prim_paths is updated below, sees the renamed prim as
        # "new", and creates a duplicate chunk (the "renamed node vanishes / stale copy" bug).
        old_write = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            applied_any = False
            for lyr in target_layers:
                edit = Sdf.BatchNamespaceEdit()
                edit.Add(Sdf.Path(old_path), Sdf.Path(new_path))
                if lyr.Apply(edit):
                    applied_any = True
            if not applied_any:
                logger.warning("Failed to rename prim %s → %s", old_path, new_path)
                return
            if chunk is not None:
                object.__setattr__(chunk, "_exec_prim_path", new_path)
            # BatchNamespaceEdit renames the prim but does NOT fix up connections that target
            # it (e.g. a view's inputs:chunk -> old.outputs:chunk), so they'd dangle and the
            # wire would drop. Repoint every chunk-prim connection from old_path to new_path.
            self._fixup_connection_targets(old_path, new_path)
            _patch_expressions(self._stage, f"@{old_name}.", f"@{new_name}.")
            self._loaded_prim_paths.discard(old_path)
            self._loaded_prim_paths.add(new_path)
        finally:
            chunk_notice._usd_write_active = old_write

    def _fixup_connection_targets(self, old_path: str, new_path: str) -> None:
        """Repoint any chunk-prim attribute connection that references old_path onto new_path,
        so wiring survives a rename (BatchNamespaceEdit leaves connection targets dangling)."""
        old_sdf, new_sdf = Sdf.Path(old_path), Sdf.Path(new_path)
        for prim in self._stage.Traverse():
            if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
                continue
            for attr in prim.GetAttributes():
                conns = attr.GetConnections()
                if not conns:
                    continue
                new_conns = [c.ReplacePrefix(old_sdf, new_sdf) for c in conns]
                if new_conns != list(conns):
                    attr.SetConnections(new_conns)

    def _rebase_descendant_paths(self, old_root: str, new_root: str) -> None:
        """After a group is renamed (its subtree moved old_root/... -> new_root/...), rebase every
        tracked descendant path (prim-path map, loaded set, _exec_prim_path, nested group
        prim_path) so later lookups keep matching the moved prims."""
        old_prefix = old_root + "/"

        def rebase(p: str) -> str:
            return new_root + p[len(old_root):] if p.startswith(old_prefix) else p

        for cid, cpath in list(self._chunk_prim_paths.items()):
            self._chunk_prim_paths[cid] = rebase(cpath)
        self._loaded_prim_paths = {rebase(p) for p in self._loaded_prim_paths}
        for c in self._scene.get_chunks():
            ep = getattr(c, "_exec_prim_path", None)
            if ep and ep.startswith(old_prefix):
                object.__setattr__(c, "_exec_prim_path", rebase(ep))
            if (c.has_flag(ChunkFlags.GroupFlags.CONTEXT) and getattr(c, "prim_path", None)
                    and c.prim_path.startswith(old_prefix)):
                c.prim_path = rebase(c.prim_path)

    def _handle_param_rename(self, param: "core.ChunkParamBase", old_name: str, new_name: str) -> None:
        """Rename a param's USD attribute and patch expression strings."""
        if not (self.initialized and self._stage):
            return
        chunk = next(
            (c for c in self._scene.get_chunks() if any(p is param for _, p in _iter_chunk_params(c))),
            None,
        )
        if chunk is None:
            return
        prim_path = f"{_resolve_chunk_root(chunk)}/{chunk.name}"
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not prim.IsValid():
            return
        old_attr = f"{_INPUTS_PREFIX}{old_name}"
        new_attr = f"{_INPUTS_PREFIX}{new_name}"
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(Sdf.Path(f"{prim_path}.{old_attr}"), Sdf.Path(f"{prim_path}.{new_attr}"))
        # Wherever the attr's spec currently lives — its Edit Target when first
        # authored, not necessarily the session layer anymore.
        for layer in self._stage.GetLayerStack(includeSessionLayers=True):
            prim_spec = layer.GetPrimAtPath(Sdf.Path(prim_path))
            if prim_spec and old_attr in prim_spec.properties:
                layer.Apply(edit)
        _patch_expressions(self._stage, f".{old_name}@", f".{new_name}@")

    def _sync_list_proxy_to_usd(self, proxy_param) -> None:
        """Write the full array for a ListGroup template param when any proxy value changes."""
        # Ownership chain: ProxyParam → NameSyncedContainer → ChunkGroupBase("0") → ChunkListGroupBase
        container = proxy_param.get_owner()
        proxy_group = container.get_owner() if container is not None else None
        if not isinstance(proxy_group, core.ChunkGroupBase) or not proxy_group.name.isdigit():
            return
        list_group = proxy_group.get_owner()
        if not isinstance(list_group, core.ChunkListGroupBase):
            return
        chunk = list_group.get_chunk()
        if chunk is None:
            return
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path:
            prim_path = f"{_resolve_chunk_root(chunk)}/{chunk.name}"
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not prim.IsValid():
            return
        lg_path = next(
            (p for p, lg in _iter_list_group_arrays(chunk) if lg is list_group), None
        )
        if lg_path is None:
            return
        template_name = proxy_param.name
        values = [
            v for i in range(list_group.size)
            if (px := list_group.get_indexed_param(template_name, i)) is not None
            for v in [px.value] if v is not None
        ]
        if not values:
            return
        sdf_type, usd_value = _sdf_type_and_value(values)
        if sdf_type is None:
            return
        attr_name = f"{_INPUTS_PREFIX}{lg_path.replace('.', ':')}:{template_name}"
        chunk_notice._usd_write_active = True
        try:
            # Rewritten wholesale on every change (not incrementally) — if it already
            # exists, keep updating it wherever its spec already lives; only a genuinely
            # new attr needs a target resolved via the chunk's current Edit Target.
            existing = prim.GetAttribute(attr_name)
            home = chunk_utils.get_attr_home_layer(existing) if existing.IsValid() else None
            ctx = Usd.EditContext(self._stage, home) if home else self._chunk_prim_edit_context(chunk)
            with ctx:
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                attr.Set(usd_value)
        finally:
            chunk_notice._usd_write_active = False

    def _on_dependency_added(self, notice, sender):
        if not self.initialized or not self._stage:
            return
        if self._reconciling_exec:
            # The edge is being mirrored FROM the stage, so inputs:chunk already says this.
            # Authoring it again writes a second spec into the bookkeeping layer, and
            # reconcile_edit_targets later migrates whichever spec it finds first — which is
            # how a healed exec chain got silently reverted to the pre-heal connection.
            return
        dependent = sender
        dependency = notice.data
        if not isinstance(dependent, core.ChunkBase) or not isinstance(dependency, core.ChunkBase):
            return
        dep_prim = self._stage.GetPrimAtPath(Sdf.Path(f"{_resolve_chunk_root(dependent)}/{dependent.name}"))
        if not dep_prim.IsValid():
            return
        src_path = Sdf.Path(exec_output_path(dependency))
        # Nothing below this is worth doing for an edge USD already has — and on load that
        # is every edge, since load_chunks mirrors add_dependency FROM the authored
        # inputs:chunk. Checking before opening the edit context matters as much as the
        # skipped write: resolving that context walks the exec chain upstream, as does the
        # reconcile, so a graph restored edge by edge paid two full walks per chunk to
        # re-derive an answer that could not have changed.
        # Every slot, not just inputs:chunk — otherwise a branch already wired into a merge's
        # slot 2 looks absent here and gets authored a second time.
        existing_conns = exec_input_connections(dep_prim)
        if src_path in existing_conns:
            return
        if is_merge_prim(dep_prim):
            # A merge holds exactly one connection per slot. Stacking a second target onto
            # slot 0 (what the branch below does) would collapse two branches into a single
            # port and destroy the ordering the slots exist to express, so author into the
            # first free slot instead — appending the new branch as the weakest. Density is
            # guaranteed by normalize_merge_slots, so the free slot is just the count.
            with self._chunk_prim_edit_context(dependent):
                exec_in = dep_prim.CreateAttribute(
                    exec_input_name(len(existing_conns)), Sdf.ValueTypeNames.Token, custom=False
                )
                exec_in.SetConnections([src_path])
        else:
            with self._chunk_prim_edit_context(dependent):
                exec_in = dep_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
                existing = list(exec_in.GetConnections())
                if src_path not in existing:
                    exec_in.SetConnections(existing + [src_path])
        # The exec chain the memo walks just gained an edge, under a suppressed-notice
        # edit context.
        invalidate_edit_layer_memo()
        # Wiring a chunk into the exec chain can change the Edit Target that now applies at
        # its position (e.g. it's now downstream of an Edit Target chunk) — re-home it and
        # everything downstream to honor the target at that place. Nothing above the new edge
        # can have moved, so the pass starts at the chunk that gained the input.
        reconcile_edit_targets(self._stage, self._scene, roots=[dependent])

    def _sync_vector_input(self, chunk: "core.ChunkBase", vector: "core.ChunkVectorBase") -> None:
        """Create/restore a chunk-prim attr for a vector and wire it.

        If the vector already drives a real prim attr (via connect_usd_attr — e.g. a
        Vec3f xformOp), the real attr's exact USD type is preserved and its value seeds
        the chunk-prim attr. Otherwise (a standalone vector param, e.g. a compute-chunk
        input/output with no real prim behind it) the type is derived from the vector's
        own default_value/length and lazily authored on first non-default change or
        expression connection, mirroring _sync_param_input's pattern for scalars.
        """
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path:
            return

        existing = getattr(vector, '_param_attr_path', None)
        if existing and existing.startswith(str(prim_path)):
            return  # already wired to this chunk prim

        raw = getattr(vector, 'raw_value', None)
        is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)

        real_attr_path = existing
        real_attr = self._stage.GetAttributeAtPath(Sdf.Path(real_attr_path)) if real_attr_path else None
        if real_attr and not real_attr.IsValid():
            real_attr = None

        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not (prim and prim.IsValid()):
            return

        vector_path = next((p for p, v in _iter_chunk_vectors(chunk) if v is vector), None)
        if vector_path is None:
            return

        if real_attr:
            sdf_type = real_attr.GetTypeName()
        else:
            type_source = vector.default_value
            if ChunkFlags.ParamType.FLOAT in vector.evaluate_type():
                sdf_type, _ = _VECTOR_TYPE_MAP[(float, vector.length)]
            else:
                sdf_type, _ = _sdf_vector_type(type_source) if type_source else (None, None)
            if sdf_type is None:
                return
            if not is_expr and vector.value == vector.default_value:
                return  # still at default — don't create attr

        prefix = "outputs:" if vector.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
        attr_name = f"{prefix}{vector_path.replace('.', ':')}"

        saved = prim.GetAttribute(attr_name)
        if saved.IsValid() and saved.Get() is not None:
            attr = saved
        elif real_attr:
            if not real_attr.HasAuthoredValue():
                return  # vector attr is at schema default — skip
            current_val = real_attr.Get()
            if current_val is None:
                return
            with self._chunk_prim_edit_context(chunk):
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                attr.Set(current_val)
        else:
            # Chunk-prim bookkeeping — the chunk's currently active Edit Target (session
            # by default), matching _sync_param_input's equivalent branch for scalars.
            with self._chunk_prim_edit_context(chunk):
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                if is_expr:
                    attr.SetConnections(self._expr_connections(prim, raw))
                    chunk_utils.store_expression_text(attr, raw)
                else:
                    attr.Set(vector.value)

        vector._connect_param_attr(attr)

        if real_attr_path and real_attr_path != str(attr.GetPath()):
            if old_attr := self._stage.GetAttributeAtPath(Sdf.Path(real_attr_path)):
                if old_attr.IsValid():
                    with self._chunk_prim_edit_context(chunk):
                        vector.connect_usd_attr(old_attr)
                    self._clear_driven_attr_value(old_attr)
                    chunk_layer = vector.get_edit_context()
                    if chunk_layer and (cur := attr.Get()) is not None:
                        with Usd.EditContext(self._stage, chunk_layer):
                            old_attr.Set(cur)

    def _refresh_gizmo_anchors(self, chunk: "core.ChunkBase") -> None:
        """Keep every gizmo's anchor rel pointing at the prim the chunk currently targets.

        Hangs off the generic param-changed path rather than watching the anchor param
        specifically: which param is the anchor is per-gizmo and can be inferred, so there is
        no single name to watch. Cheap enough to sit here because chunk_gizmo returns
        immediately for a chunk that declares no gizmos, which is nearly all of them.
        """
        if not chunk_gizmo.get_gizmos(chunk):
            return
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path:
            return
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not (prim and prim.IsValid()):
            return
        home = chunk_utils.get_bookkeeping_layer(self._stage, chunk)
        ctx = Usd.EditContext(self._stage, home) if home else contextlib.nullcontext()
        with ctx:
            chunk_gizmo.refresh_gizmo_anchors(self._stage, prim, chunk)

    def _sync_param_input(self, chunk: "core.ChunkBase", param: "core.ChunkParamBase", force: bool = False) -> None:
        """Create and wire a single param's chunk-prim input attr on first non-default change.

        Skips immediately if the param is already bound to this chunk's prim attr. With
        force=True the attr is created even while the param is still at its default value
        (used by Loft up, which needs a USD attr to promote to the context interface).
        """
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path:
            return

        existing = getattr(param, '_param_attr_path', None)
        if existing and existing.startswith(prim_path):
            return  # already wired; data_changed already wrote the value

        raw = getattr(param, 'raw_value', None)
        if raw is None:
            return
        is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
        type_source = getattr(param, 'default_value', None) if is_expr else raw
        sdf_type = _SDF_TYPE_MAP.get(type(type_source))
        # A ColorPicker holds an (r, g, b) tuple, and _SDF_TYPE_MAP is keyed by Python
        # type — there is no tuple entry, so without this a colour param resolves to no
        # type at all and never gets a chunk-prim attr, leaving its value unable to
        # round-trip through USD.
        if ChunkFlags.ParamType.COLOR_PICKER in param.evaluate_type():
            sdf_type = Sdf.ValueTypeNames.Color3f
        if sdf_type is None:
            return
        default = getattr(param, 'default_value', None)
        if not force and not is_expr and raw == default:
            return  # still at default — don't create attr

        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not (prim and prim.IsValid()):
            return

        param_path = next((p for p, q in _iter_chunk_params(chunk) if q is param), None)
        if param_path is None:
            # Param not in scalar iteration — it may be a child of a vector.
            parent_vector = next(
                (v for _, v in _iter_chunk_vectors(chunk)
                 if any(c is param for c in v.get_my_params())),
                None
            )
            if parent_vector:
                self._sync_vector_input(chunk, parent_vector)
            return

        prefix = "outputs:" if param.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
        attr_name = f"{prefix}{param_path.replace('.', ':')}"
        saved = prim.GetAttribute(attr_name)
        if saved.IsValid() and saved.GetTypeName() != sdf_type:
            self._delete_stale_attr_specs(prim, attr_name)
            saved = prim.GetAttribute(attr_name)
        if saved.IsValid() and saved.Get() is not None:
            attr = saved
        else:
            # Chunk-prim bookkeeping — the chunk's currently active Edit Target (session
            # by default, see chunk_utils.get_bookkeeping_layer).
            with self._chunk_prim_edit_context(chunk):
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                if is_expr:
                    attr.SetConnections(self._expr_connections(prim, raw))
                    chunk_utils.store_expression_text(attr, raw)
                else:
                    attr.Set(raw)
        param._connect_param_attr(attr)

        if existing and existing != str(attr.GetPath()):
            old_attr = self._stage.GetAttributeAtPath(Sdf.Path(existing))
            if old_attr and old_attr.IsValid():
                param.connect_usd_attr(old_attr)
                self._clear_driven_attr_value(old_attr)
                chunk_layer = param.get_edit_context()
                raw = getattr(param, 'raw_value', None)
                if chunk_layer and raw is not None:
                    with Usd.EditContext(self._stage, chunk_layer):
                        if old_attr.Get(0) != raw:
                            old_attr.Set(raw)

    def _delete_stale_attr_specs(self, prim: "Usd.Prim", attr_name: str) -> None:
        """Remove attr_name's spec from every layer in the stack where it's found.
        The stale spec may live in whichever layer was the edit target when it was
        first authored — not necessarily the current one — so scan the whole stack
        rather than just one layer."""
        for layer in self._stage.GetLayerStack(includeSessionLayers=True):
            prim_spec = layer.GetPrimAtPath(prim.GetPath())
            if prim_spec and attr_name in prim_spec.properties:
                del prim_spec.properties[attr_name]

    def _sync_chunk_inputs(self, chunk: "core.ChunkBase") -> None:
        """Wire dynamically-added params to chunk prim inputs: attrs.

        Creates the inputs: attr for each param (so _connect_param_attr can
        bind to it), but only authors a value when the param differs from its
        default.  Also registers any previously-direct real-prim attr as a
        driven attr so USD connections propagate param changes to it.
        """
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path:
            return
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not (prim and prim.IsValid()):
            return
        for path, param in _iter_chunk_params(chunk):
            raw = getattr(param, 'raw_value', None)
            if raw is None:
                continue
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            type_source = getattr(param, 'default_value', None) if is_expr else raw
            sdf_type = _SDF_TYPE_MAP.get(type(type_source))
            if sdf_type is None:
                continue
            if ChunkFlags.ParamType.FLOAT in param.evaluate_type():
                sdf_type = _SDF_TYPE_MAP[float]
            # See _sync_param_input: a ColorPicker's tuple has no _SDF_TYPE_MAP entry.
            if ChunkFlags.ParamType.COLOR_PICKER in param.evaluate_type():
                sdf_type = Sdf.ValueTypeNames.Color3f
            prefix = "outputs:" if param.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
            attr_name = f"{prefix}{path.replace('.', ':')}"

            # If the prim already has a saved value (from a prior save/load cycle),
            # restore the param from it rather than overwriting with the current default.
            saved = prim.GetAttribute(attr_name)
            if saved.IsValid() and saved.GetTypeName() != sdf_type:
                self._delete_stale_attr_specs(prim, attr_name)
                saved = prim.GetAttribute(attr_name)
            if saved.IsValid() and saved.Get() is not None:
                attr = saved
            else:
                backing_path = getattr(param, '_param_attr_path', None)
                if not is_expr:
                    if backing_path:
                        # param_from_attr: only store if the real USD attr has been authored
                        backing = self._stage.GetAttributeAtPath(Sdf.Path(backing_path))
                        if not (backing and backing.HasAuthoredValue()):
                            continue
                    else:
                        default = getattr(param, 'default_value', None)
                        if raw == default:
                            continue  # pure-Python param at its default — skip
                with self._chunk_prim_edit_context(chunk):
                    attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                    if is_expr:
                        attr.SetConnections(self._expr_connections(prim, raw))
                        chunk_utils.store_expression_text(attr, raw)
                    else:
                        attr.Set(raw)

            old_storage_path = getattr(param, '_param_attr_path', None)
            param._connect_param_attr(attr)  # reads attr value and updates param

            if old_storage_path and old_storage_path != str(attr.GetPath()):
                old_attr = self._stage.GetAttributeAtPath(Sdf.Path(old_storage_path))
                if old_attr and old_attr.IsValid():
                    param.connect_usd_attr(old_attr)
                    self._clear_driven_attr_value(old_attr)
                    chunk_layer = param.get_edit_context()
                    raw = getattr(param, 'raw_value', None)
                    if chunk_layer and raw is not None:
                        with Usd.EditContext(self._stage, chunk_layer):
                            if old_attr.Get(0) != raw:
                                old_attr.Set(raw)

        for _, vector in _iter_chunk_vectors(chunk):
            self._sync_vector_input(chunk, vector)

    def _on_param_added(self, notice, sender):
        if not self.initialized or not self._stage:
            return
        chunk = notice.data
        if not isinstance(chunk, core.ChunkBase):
            if isinstance(chunk, core.ChunkCommon):
                owner_chunk = chunk.get_chunk()
                if owner_chunk:
                    self._sync_chunk_inputs(owner_chunk)
            return
        is_group = chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT)
        if is_group:
            # For CONTEXT group chunks, prim lives at parent_root/name, not under
            # _resolve_chunk_root (which would give prim_path/chunks — the sub-root).
            parent_ctx = getattr(chunk, "_parent_context", None)
            parent_ctx = parent_ctx() if parent_ctx else None
            if parent_ctx and getattr(parent_ctx, "prim_path", None):
                root = parent_ctx.prim_path
            else:
                root = _CHUNKS_ROOT
        else:
            root = _resolve_chunk_root(chunk)
        prim_path = f"{root}/{chunk.name}"
        # If the path is already tracked it's either (a) a load — load_chunks pre-registers paths
        # before add_chunk — or (b) a name collision the add_chunk dedup missed. The dedup checks
        # the scene-root container, but a group's siblings live under its parent context, so two
        # same-named groups under one tab collide and this would wrongly skip the second's prim.
        # Distinguish: (b) means the path already belongs to ANOTHER chunk → make the name unique.
        if prim_path in self._loaded_prim_paths:
            taken_by_other = any(
                p == prim_path for cid, p in self._chunk_prim_paths.items() if cid != id(chunk)
            )
            if not taken_by_other:
                # (a) load — the prim exists; register and skip _define_prim.
                self._chunk_prim_paths[id(chunk)] = prim_path
                return
            # (b) collision — pick the next free sibling name.
            base, i = chunk.name, 1
            while f"{root}/{base}{i}" in self._loaded_prim_paths:
                i += 1
            chunk.name = f"{base}{i}"
            prim_path = f"{root}/{chunk.name}"
        self._loaded_prim_paths.add(prim_path)
        self._chunk_prim_paths[id(chunk)] = prim_path
        # Use object.__setattr__ to store the prim path without going through
        # the attrs _wrapped_setattr, which would fire value_changed / notices.
        object.__setattr__(chunk, '_exec_prim_path', prim_path)

        if is_group:
            pc = getattr(chunk, "_parent_context", None)
            tail_context = pc() if pc else None
        else:
            tail_context = chunk.get_context()
        # Auto-chain (prev -> new via a default inputs:chunk dep) only in the Params tab, where
        # creates build a linear chain. In the node graph, new nodes are created UNCONNECTED —
        # the user wires them (or drops onto a wire); only the first node connects, via the
        # view auto-wiring in _ensure_default_view.
        prev = self._find_dag_tail(tail_context, exclude=chunk) if _params_tab_create else None
        prim = self._define_prim(chunk, prev)
        self._wire_inputs(prim, chunk)
        # Set prim_path on group contexts so sub-chunks nest correctly under them.
        if is_group:
            chunk.prim_path = prim_path

        # View-follows-tail — ONLY for chunks created in the Params tab (_params_tab_create).
        # The new chunk was chained after `prev` (prev -> new); if a view was fed by `prev`,
        # move it onto `new` (prev -> new -> view). In the node graph the user wires manually,
        # so we don't auto-repoint the view there.
        if _params_tab_create and prev is not None and not is_view_chunk(chunk):
            self._rewire_views_from_source(prev, chunk)

    def _rewire_views_from_source(self, old_src: "core.ChunkBase", new_src: "core.ChunkBase") -> None:
        """Repoint any view whose inputs:chunk feeds off old_src onto new_src's output."""
        def _path(c):
            return self._chunk_prim_paths.get(id(c)) or f"{_resolve_chunk_root(c)}/{c.name}"
        old_path = _path(old_src)
        new_out = Sdf.Path(f"{_path(new_src)}.{_EXEC_OUTPUT}")
        for c in self._scene.get_chunks():
            if not is_view_chunk(c):
                continue
            vprim = self._stage.GetPrimAtPath(Sdf.Path(_path(c)))
            if not vprim.IsValid():
                continue
            exec_in = vprim.GetAttribute(_EXEC_INPUT)
            if not (exec_in and exec_in.IsValid()):
                continue
            if any(str(p.GetPrimPath()) == old_path for p in exec_in.GetConnections()):
                # exec_in already exists — update wherever its spec already lives, not
                # wherever c's Edit Target currently resolves to (see get_attr_home_layer).
                home = chunk_utils.get_attr_home_layer(exec_in)
                ctx = Usd.EditContext(self._stage, home) if home else self._chunk_prim_edit_context(c)
                with ctx:
                    exec_in.SetConnections([new_out])

    def _on_chunk_loaded(self, notice, sender):
        """Run a chunk's module execute() hook once on interactive creation.

        We fire on CHUNK_LOADED rather than PARAM_ADDED because add_param
        clears the chunk's edit layer *after* PARAM_ADDED — running execute earlier would
        author into that layer only for it to be wiped. The load path already calls
        execute() itself, so skip while a stage is loading to avoid a double run.
        """
        if not self.initialized or not self._stage:
            return
        if chunk_notice._loading_stage:
            return
        chunk = notice.data
        if not isinstance(chunk, core.ChunkBase):
            return
        module = getattr(chunk, "_module", None)
        if not callable(getattr(module, "execute", None)):
            return
        # A blocked chunk (view-driven: created out of the active view's tree) must not execute —
        # it cooks only once wired in and unblocked by recook.
        if chunk.is_blocked():
            return

        chunk.execute()

    def _find_dag_tail(self, context, exclude=None) -> "core.ChunkBase | None":
        """Return the tail of the exec DAG for this context — the chunk whose outputs:chunk
        is not referenced by any other chunk's inputs:chunk. Used as prev_chunk when no
        interactive history exists (e.g. first add after loading a .usda)."""
        # Regular chunks in this context (never a view — a view consumes the tail and is
        # terminal, so it must not be treated as the chain tail itself).
        context_chunks = [
            c for c in self._scene.get_chunks()
            if c.get_context() == context and c is not exclude and not is_view_chunk(c)
        ]
        # Also include ChunkGroup chunks (CONTEXT with a USD prim) that belong here.
        # Plain tab contexts are excluded — they have no prim and no exec connections.
        for c in self._scene.get_chunks():
            if not isinstance(c, core.ChunkBase) or c is exclude:
                continue
            if not (c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                    and c.get_config() and c.get_config().module):
                continue
            pc = getattr(c, "_parent_context", None)
            if (pc() if pc else None) == context:
                context_chunks.append(c)

        if not context_chunks:
            return None

        referenced: set[str] = set()
        for c in context_chunks:
            prim_path = (
                c.prim_path if c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                else f"{_resolve_chunk_root(c)}/{c.name}"
            )
            prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
            if not prim.IsValid():
                continue
            # Every slot — anything feeding a merge is referenced, whichever slot it lands in.
            for conn in exec_input_connections(prim):
                referenced.add(str(conn.GetPrimPath()))

        # Last unreferenced chunk in list order is the tail
        tail = None
        for c in context_chunks:
            prim_path = (
                c.prim_path if c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                else f"{_resolve_chunk_root(c)}/{c.name}"
            )
            if prim_path not in referenced:
                tail = c
        return tail

    def _new_chunk_bookkeeping_layer(self, chunk: core.ChunkBase, prev_chunk: "core.ChunkBase | None") -> "Sdf.Layer | None":
        """Resolve where a *brand new* chunk's bookkeeping prim should be authored, from its
        exec-chain predecessor(s) — the chunk's own prim isn't wired into the chain yet, so
        nearest_upstream_edit_layer on it would resolve to the session-layer default.

        CONTEXT chunks (containers) always pin to the session layer, like get_bookkeeping_layer.
        Otherwise: if a predecessor is itself an Edit Target chunk, its published layer applies;
        else recurse up from that predecessor. Falls back to the session layer.
        """
        session = self._stage.GetSessionLayer() if self._stage else None
        if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            return session
        deps = [d for _, d in chunk.get_dependencies() if isinstance(d, core.ChunkBase)]
        if not deps and prev_chunk is not None:
            deps = [prev_chunk]
        for dep in deps:
            dep_prim = self._stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dep)))
            if not dep_prim.IsValid():
                continue
            layer = _prim_edit_target_layer(self._stage, dep_prim)
            if layer is None:
                layer = nearest_upstream_edit_layer(self._stage, dep_prim)
            if layer is not None:
                return layer
        return session

    def _define_prim(self, chunk: core.ChunkBase, prev_chunk: "core.ChunkBase | None" = None) -> Usd.Prim:
        # Author the prim at the exact path _on_param_added registered (chunk._exec_prim_path),
        # so the prim is CREATED where it was REGISTERED. For CONTEXT group chunks these diverge:
        # _on_param_added resolves the parent via _parent_context (e.g. "/MyTab"), but
        # _resolve_chunk_root resolves via get_context() — None for a root-level group, giving
        # "/chunks". A create/register mismatch defeats load_chunks' dedup and recurses forever
        # (_define_prim → StageContentsChanged → load_chunks re-adds → _define_prim → …).
        exec_path = getattr(chunk, "_exec_prim_path", None)
        if exec_path:
            target_path = str(exec_path)
            chunk_root = str(Sdf.Path(target_path).GetParentPath())
        else:
            chunk_root = _resolve_chunk_root(chunk)
            target_path = f"{chunk_root}/{chunk.name}"

        # Resolve the layer this new chunk's bookkeeping should live in from its exec-chain
        # predecessor, BEFORE authoring — the chunk isn't wired into the chain yet, so
        # nearest_upstream_edit_layer on its own (still-unconnected) prim would wrongly say
        # "session". Authoring straight into the right layer here avoids a fragile post-hoc
        # migration (and the inputs:chunk it would author would otherwise land in session and
        # need moving). All authoring must land here, never on a chunk's own anonymous execute
        # layer (Clear()ed every run, not saved).
        book_layer = self._new_chunk_bookkeeping_layer(chunk, prev_chunk)

        def book_ctx():
            if book_layer:
                return Usd.EditContext(self._stage, book_layer)
            return contextlib.nullcontext()

        with book_ctx():
            _define_chunk_parent(self._stage, chunk_root)

        root_prim = self._stage.GetPrimAtPath(Sdf.Path(chunk_root))
        index = sum(
            1 for child in (root_prim.GetChildren() if root_prim.IsValid() else [])
            if child.GetTypeName() == _CHUNK_PRIM_TYPENAME
        )

        with book_ctx():
            prim = self._stage.DefinePrim(Sdf.Path(target_path), _CHUNK_PRIM_TYPENAME)

            ui_api = UsdUI.NodeGraphNodeAPI.Apply(prim)
            if not ui_api.GetPosAttr().HasAuthoredValue():
                if _create_node_pos is not None:
                    ui_api.CreatePosAttr(Gf.Vec2f(*_create_node_pos))
                else:
                    ui_api.CreatePosAttr(Gf.Vec2f(index * _NODE_STEP, 0.0))

            config = chunk.get_config()
            if config and config.module:
                attr = prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, custom=False)
                attr.SetVariability(Sdf.VariabilityUniform)
                if not attr.Get():
                    attr.Set(config.module)

            pinned_attr = prim.CreateAttribute("chunk:pinned", Sdf.ValueTypeNames.Bool, custom=True)
            pinned_attr.SetVariability(Sdf.VariabilityUniform)
            if not pinned_attr.HasAuthoredValue():
                # Views are never auto-pinned, regardless of which surface/flow created them —
                # see is_chunk_pinned's matching unconditional view special case.
                pinned_attr.Set(False if is_view_chunk(chunk) else bool(_pin_new_chunks))

            config = chunk.get_config()
            if not (config and config.terminal):
                exec_out = prim.CreateAttribute(_EXEC_OUTPUT, Sdf.ValueTypeNames.Token, custom=False)
                exec_out.SetVariability(Sdf.VariabilityUniform)

            if config and getattr(config, "outputs", None):
                for out_name, out_type_name in config.outputs:
                    out_type = getattr(Sdf.ValueTypeNames, out_type_name, None)
                    if out_type is not None:
                        prim.CreateAttribute(f"outputs:{out_name}", out_type, custom=False)

            chunk_gizmo.author_gizmos(self._stage, prim, chunk)

            deps = [dep for _, dep in chunk.get_dependencies() if isinstance(dep, core.ChunkBase)]
            if not deps and prev_chunk:
                deps = [prev_chunk]
            if deps:
                exec_in = prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
                exec_in.SetConnections([
                    Sdf.Path(
                        f"{dep.prim_path}.{_EXEC_OUTPUT}"
                        if dep.has_flag(ChunkFlags.GroupFlags.CONTEXT) and getattr(dep, "prim_path", None)
                        else f"{_resolve_chunk_root(dep)}/{dep.name}.{_EXEC_OUTPUT}"
                    )
                    for dep in deps
                ])
                invalidate_edit_layer_memo()
                if not prim.IsValid():
                    # Wiring this new prim's exec-chain connection just changed exec
                    # topology, and a dependency's own script can react to that reactively
                    # (e.g. create_layer.py's execute() re-ranks its layer against the
                    # active view on any exec-chain change) -- reentrant, before this
                    # function returns. If that reentrant run reassigns subLayerPaths (it
                    # does, to reposition itself), the resulting stage recomposition can
                    # invalidate THIS prim's own Usd.Prim handle if it composes through
                    # the same layer subtree, even though the underlying prim spec itself
                    # is untouched. Re-fetch by path rather than continuing to hold (and
                    # eventually deref) a dead handle -- live-reproduced building a 3rd
                    # chained Create Layer/Edit Target pair, which crashed here instead of
                    # in the caller a few lines away.
                    prim = self._stage.GetPrimAtPath(Sdf.Path(target_path))

            for path, param in _iter_chunk_params(chunk):
                raw = param.raw_value
                if raw is None:
                    continue
                is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
                type_source = getattr(param, 'default_value', None) if is_expr else raw
                sdf_type = _SDF_TYPE_MAP.get(type(type_source))
                if sdf_type is None:
                    continue
                backing_path = getattr(param, '_param_attr_path', None)
                if not is_expr:
                    if backing_path:
                        backing = self._stage.GetAttributeAtPath(Sdf.Path(backing_path))
                        if not (backing and backing.HasAuthoredValue()):
                            continue
                    else:
                        default = getattr(param, 'default_value', None)
                        if raw == default:
                            continue
                prefix = "outputs:" if param.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
                attr_name = f"{prefix}{path.replace('.', ':')}"
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                if is_expr:
                    attr.SetConnections(self._expr_connections(prim, raw))
                    chunk_utils.store_expression_text(attr, raw)
                elif not attr.Get():
                    attr.Set(raw)

            for vec_path, vector in _iter_chunk_vectors(chunk):
                real_path = getattr(vector, '_param_attr_path', None)
                if not real_path:
                    continue
                real_attr = self._stage.GetAttributeAtPath(Sdf.Path(real_path))
                if not (real_attr and real_attr.IsValid()):
                    continue
                if not real_attr.HasAuthoredValue():
                    continue
                current_val = real_attr.Get()
                if current_val is None:
                    continue
                attr_name = f"{_INPUTS_PREFIX}{vec_path.replace('.', ':')}"
                attr = prim.CreateAttribute(attr_name, real_attr.GetTypeName(), custom=False)
                if not attr.Get():
                    attr.Set(current_val)

        return prim

    def _wire_inputs(self, prim: Usd.Prim, chunk: core.ChunkBase) -> None:
        _load_wire_inputs(prim, chunk)


def _patch_expressions(stage: Usd.Stage, old: str, new: str) -> None:
    """Replace all occurrences of old with new in string attributes across the stage.

    Each chunk-prim bookkeeping attr (the expression strings this patches) can now live
    in a different layer — whichever was the active Edit Target when it was first
    authored — rather than always the session layer, so there's no single EditContext
    that's correct for every attr in this traversal. Patch each one wherever its
    existing spec already lives instead (see chunk_utils.get_attr_home_layer); writing
    to the stage's ambient edit target (unscoped) would author a weaker opinion that
    gets silently overridden by the original in composition.
    """
    for prim in stage.TraverseAll():
        for attr in prim.GetAttributes():
            val = attr.Get()
            # A non-String attr keeps its expression text in customData rather than as its
            # value (chunk_utils.store_expression_text), so patch that too — otherwise
            # renaming a chunk leaves every numeric param wired to it holding the old name.
            text = attr.GetCustomDataByKey(chunk_utils.EXPR_TEXT_KEY)
            hit_val = isinstance(val, str) and old in val
            hit_text = isinstance(text, str) and old in text
            if not (hit_val or hit_text):
                continue
            home = chunk_utils.get_attr_home_layer(attr)
            if not home:
                continue
            with Usd.EditContext(stage, home):
                if hit_val:
                    attr.Set(val.replace(old, new))
                if hit_text:
                    attr.SetCustomDataByKey(chunk_utils.EXPR_TEXT_KEY, text.replace(old, new))



def _define_chunk_parent(stage: "Usd.Stage", path: str) -> "Usd.Prim":
    """Define the parent prim a chunk hangs under, typed for what it actually is.

    The /chunks root is a Scope; every other parent is a chunkgroup's own Chunk prim.
    """
    typename = _CHUNKS_ROOT_TYPENAME if str(path) == _CHUNKS_ROOT else _CHUNK_PRIM_TYPENAME
    return stage.DefinePrim(Sdf.Path(path), typename)


def _resolve_chunk_root(chunk: "core.ChunkBase") -> str:
    """Return the parent prim path for chunk based on its context's prim_path."""
    context = chunk.get_context()
    if context and context.prim_path:
        return context.prim_path
    return _CHUNKS_ROOT


def get_chunk_prim_path(chunk: "core.ChunkBase") -> str:
    """Return the USD prim path string for a chunk."""
    if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT) and chunk.get_config() and chunk.get_config().module:
        return chunk.prim_path or f"{_resolve_chunk_root(chunk)}/{chunk.name}"
    return f"{_resolve_chunk_root(chunk)}/{chunk.name}"


def exec_output_path(chunk: "core.ChunkBase") -> str:
    """Return the outputs:chunk attribute path an exec connection to chunk must point at.

    A ChunkGroup is its own prim root, so its exec output lives on the group prim itself
    (/Tab1/Group.outputs:chunk) — NOT under the root it provides for its children, which is
    what _resolve_chunk_root returns and would give the nonexistent
    /Tab1/Group/Group.outputs:chunk.

    Args:
        chunk (core.ChunkBase): The upstream chunk being connected from

    Returns:
        str: The absolute outputs:chunk attribute path
    """
    if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT) and getattr(chunk, "prim_path", None):
        return f"{chunk.prim_path}.{_EXEC_OUTPUT}"
    return f"{_resolve_chunk_root(chunk)}/{chunk.name}.{_EXEC_OUTPUT}"


def is_node_in_context(chunk: "core.ChunkBase", context: "core.ChunkBase | None") -> bool:
    """True if chunk is a NODE in context's graph — its prim is a direct child of the context's
    root prim. Matches how the graph rebuild picks nodes, and correctly separates 'group as a
    node' from 'group as a container': a group is a node in its PARENT graph (its prim's parent
    is the parent root), and the group's inner chunks are nodes in the group's graph. Using
    get_context() here would conflate the two (it returns self for a context chunk).
    """
    if context is None:
        return True
    root = context.prim_path if getattr(context, "prim_path", None) else _CHUNKS_ROOT
    return str(Sdf.Path(get_chunk_prim_path(chunk)).GetParentPath()) == str(root)


def is_view_chunk(chunk: "core.ChunkBase") -> bool:
    """Return True if chunk is a view-type chunk."""
    cfg = chunk.get_config()
    return bool(cfg and cfg.module == "view")


def is_group_chunk(chunk: "core.ChunkBase") -> bool:
    """Return True if chunk owns child chunks — a CONTEXT chunk with a module.

    This is about *containment*: whose children are hidden until entered, cook as a unit,
    and share a layer. It is true of a chunkgroup, and equally true of a chunk-in-chunk
    like AMDMaterialXLibrary, whose children are an implementation detail. For the
    narrower "is this a subnet the user may navigate into", see is_chunkgroup.
    """
    cfg = chunk.get_config()
    return bool(chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT) and cfg and cfg.module)


def is_chunkgroup(chunk: "core.ChunkBase") -> bool:
    """Return True if chunk is a chunkgroup proper — the `chunkgroup` chunk type.

    A chunkgroup is defined in USD and is a subnet you navigate into; a chunk is defined in
    python, and one that owns children (AMDMaterialXLibrary creating `reference` and
    `bind_material`) is reusing them as an implementation detail, not exposing a subnet to
    edit. Both carry GroupFlags.CONTEXT, so CONTEXT cannot tell them apart — which is why
    diving into the AMD node used to be offered at all.

    Navigation, and the UI that advertises it, keys on this. Containment and cooking key on
    is_group_chunk, so a chunk-in-chunk keeps hiding its children and running their code.
    """
    cfg = chunk.get_config()
    return bool(cfg and cfg.module == "chunkgroup")


def is_merge_chunk(chunk: "core.ChunkBase") -> bool:
    """Return True if chunk is a Merge chunk (scripts/merge.py) — the only chunk type that
    takes more than one exec input."""
    cfg = chunk.get_config()
    return bool(cfg and cfg.module == "merge")


def is_merge_prim(prim: "Usd.Prim") -> bool:
    """is_merge_chunk for a bare prim — used where only the stage is at hand (slot
    compaction, cook ordering) and there may be no live chunk object to ask."""
    if not (prim and prim.IsValid()):
        return False
    attr = prim.GetAttribute(_CHUNK_TYPE_ATTR)
    return bool(attr and attr.IsValid() and attr.Get() == "merge")


def is_chunk_pinned(chunk: "core.ChunkBase") -> bool:
    """Return whether chunk is pinned into its context's Params page.

    Reads the persisted ``chunk:pinned`` custom attribute (see ADR-0003), following the
    ``chunk:activeView`` precedent. An explicitly authored value (set by set_chunk_pinned, i.e.
    the Pin/Unpin menu action, or already written by _define_prim for this chunk) always wins.

    Otherwise — no authored value, which can mean the PRIM ITSELF doesn't exist yet, not just
    a missing attribute on an existing one — fall back in order:

    1. Views are graph-wiring utility nodes, never meant for Params curation: default unpinned,
       whether genuinely legacy, mid-creation, or racing _define_prim's own write (below).
    2. _pin_new_chunks, when a create/paste flow is actively deciding this chunk's pin state
       right now. Set by the caller BEFORE the create notice is even sent, so it's correct
       regardless of listener order. This matters because the Params-page widget and
       ChunkStageSync both react to the same PARAM_ADDED notice for a newly-created chunk, and
       delivery order between two independent listeners on one notice isn't guaranteed — the
       widget's listener can run (and call this) before ChunkStageSync's has even started
       _define_prim, let alone authored the attribute. Falling back to the migration default
       in that window would show a freshly Graph-created chunk in Params for a moment, and
       permanently, since nothing re-checks after _define_prim's write lands right after.
    3. Migration default: pinned=true, so a genuinely legacy chunk (no creation flow involved
       at all — e.g. a scene saved before this feature shipped) doesn't lose Params visibility.
    """
    stage = chunk.get_stage()
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk))) if stage else None
    attr = prim.GetAttribute("chunk:pinned") if (prim and prim.IsValid()) else None
    val = attr.Get() if (attr and attr.IsValid()) else None
    if val is not None:
        return bool(val)
    if is_view_chunk(chunk):
        return False
    if _pin_new_chunks is not None:
        return _pin_new_chunks
    return True


def set_chunk_pinned(chunk: "core.ChunkBase", pinned: bool) -> None:
    """Persist chunk's pinned state as a custom ``chunk:pinned`` attribute on its prim.

    This is chunk-prim bookkeeping like any other inputs:/outputs: attr — it must not
    rely on the stage's ambient edit target (chunk_utils.get_bookkeeping_layer resolves
    it instead, following Edit Target for a regular chunk / pinned to session for a
    CONTEXT chunk). Update an existing spec in place (chunk_utils.get_attr_home_layer)
    so toggling this repeatedly never leaves an older, stronger opinion in one layer
    shadowing a newer one authored into a different, weaker layer — the exact
    "second mutation doesn't propagate" bug this same class of fix addresses elsewhere
    (see ChunkParamBase.data_changed / update_usd_prop).
    """
    stage = chunk.get_stage()
    if not stage:
        return
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not prim.IsValid():
        return
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        attr = prim.GetAttribute("chunk:pinned")
        home = chunk_utils.get_attr_home_layer(attr) if attr and attr.IsValid() else None
        target = home or chunk_utils.get_bookkeeping_layer(stage, chunk)
        with Usd.EditContext(stage, target):
            prim.CreateAttribute("chunk:pinned", Sdf.ValueTypeNames.Bool, custom=True).Set(bool(pinned))
    finally:
        chunk_notice._usd_write_active = prev_w


def _context_own_prim_path(context: "core.ChunkBase") -> "str | None":
    """Return a CONTEXT chunk's own prim path — NOT get_chunk_prim_path(context), which is for
    a chunk resolving its path as a CHILD of some parent context. Called on the context itself,
    get_chunk_prim_path double-appends: ChunkBase.get_context() returns self for a chunk that
    already has the CONTEXT flag, so _resolve_chunk_root(context) resolves to context.prim_path,
    and get_chunk_prim_path then appends "/{context.name}" a second time (only chunkgroups
    dodge this, because their prim_path branch short-circuits before _resolve_chunk_root runs).
    Both a plain tab and a chunkgroup already have .prim_path set directly at creation
    (add_context / the group-node prim_path assignment in _on_param_added) — just use it.
    """
    return context.prim_path


def is_pin_filter_enabled(context: "core.ChunkBase") -> bool:
    """Return whether context's Params page currently applies the pin filter (only pinned
    chunks show) or not (every chunk shows, regardless of pin state).

    Reads the persisted ``filter:pinEnabled`` custom attribute on the context's own prim, same
    pattern as ``chunk:pinned``. Absent attribute means enabled — a tab from before this filter-
    bar feature shipped keeps behaving exactly as it does today (pin filter always applied).
    """
    stage = context.get_stage()
    path = _context_own_prim_path(context)
    prim = stage.GetPrimAtPath(Sdf.Path(path)) if (stage and path) else None
    attr = prim.GetAttribute("filter:pinEnabled") if (prim and prim.IsValid()) else None
    val = attr.Get() if (attr and attr.IsValid()) else None
    return True if val is None else bool(val)


def set_pin_filter_enabled(context: "core.ChunkBase", enabled: bool) -> None:
    """Persist context's pin-filter toggle as a custom ``filter:pinEnabled`` attribute on its
    own prim (the context/tab prim itself, not any of its children).

    Unlike a regular chunk, a plain tab context's own prim is never explicitly authored by
    _define_prim — a tab context is added with send_added=False (see SceneSender.add_chunk),
    so PARAM_ADDED never fires for it and its prim only starts existing as an implicit ancestor
    once some CHILD chunk is added under it. An empty, just-created tab therefore has no prim
    at all yet — define one on demand here rather than silently no-op-ing.
    """
    stage = context.get_stage()
    path = _context_own_prim_path(context)
    if not stage or not path:
        return
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim.IsValid():
            prim = stage.DefinePrim(Sdf.Path(path), _CHUNK_PRIM_TYPENAME)
        prim.CreateAttribute("filter:pinEnabled", Sdf.ValueTypeNames.Bool, custom=True).Set(bool(enabled))
    finally:
        chunk_notice._usd_write_active = prev_w


_SEARCH_SCOPES = ("both", "name", "type")


def get_search_query(context: "core.ChunkBase") -> str:
    """Return context's persisted Params-page search query. Absent attribute means empty —
    a tab from before this search feature shipped behaves as if nothing was ever typed."""
    stage = context.get_stage()
    path = _context_own_prim_path(context)
    prim = stage.GetPrimAtPath(Sdf.Path(path)) if (stage and path) else None
    attr = prim.GetAttribute("filter:searchQuery") if (prim and prim.IsValid()) else None
    val = attr.Get() if (attr and attr.IsValid()) else None
    return val or ""


def set_search_query(context: "core.ChunkBase", query: str) -> None:
    """Persist context's Params-page search query as a custom ``filter:searchQuery`` attribute
    on its own prim. Defines the prim on demand — see set_pin_filter_enabled's docstring for
    why a plain tab context may not have one yet."""
    stage = context.get_stage()
    path = _context_own_prim_path(context)
    if not stage or not path:
        return
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim.IsValid():
            prim = stage.DefinePrim(Sdf.Path(path), _CHUNK_PRIM_TYPENAME)
        prim.CreateAttribute("filter:searchQuery", Sdf.ValueTypeNames.String, custom=True).Set(query)
    finally:
        chunk_notice._usd_write_active = prev_w


def get_search_scope(context: "core.ChunkBase") -> str:
    """Return context's persisted search scope — one of "both", "name", "type". Absent
    attribute, or an unrecognized stored value, means "both" (search name and type)."""
    stage = context.get_stage()
    path = _context_own_prim_path(context)
    prim = stage.GetPrimAtPath(Sdf.Path(path)) if (stage and path) else None
    attr = prim.GetAttribute("filter:searchScope") if (prim and prim.IsValid()) else None
    val = attr.Get() if (attr and attr.IsValid()) else None
    return val if val in _SEARCH_SCOPES else "both"


def set_search_scope(context: "core.ChunkBase", scope: str) -> None:
    """Persist context's search scope ("both", "name", or "type") as a custom
    ``filter:searchScope`` attribute on its own prim."""
    if scope not in _SEARCH_SCOPES:
        scope = "both"
    stage = context.get_stage()
    path = _context_own_prim_path(context)
    if not stage or not path:
        return
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim.IsValid():
            prim = stage.DefinePrim(Sdf.Path(path), _CHUNK_PRIM_TYPENAME)
        prim.CreateAttribute("filter:searchScope", Sdf.ValueTypeNames.String, custom=True).Set(scope)
    finally:
        chunk_notice._usd_write_active = prev_w


# How many characters a typo may cost. Two covers a doubled letter, a swap and a slip;
# past that the difference is a different name, not a misspelling of this one.
_TYPO_SLACK = 2


def fuzzy_matches(term: str, candidate: str, threshold: float = 0.6) -> bool:
    """True if term matches candidate — an exact case-insensitive substring always matches
    (the common case: the user typed the name/type correctly, or a meaningful fragment of it);
    otherwise fall back to difflib's typo-tolerant ratio so a small typo still matches."""
    term = term.strip().lower()
    if not term:
        return True
    candidate = candidate.lower()
    if term in candidate:
        return True
    # SequenceMatcher's ratio is normalised by combined length, so two short names sharing
    # a prefix score on the overlap alone: "haha" comes out 0.73 similar to "haha222" and
    # was being returned by a search for "haha222". Length is what separates the two cases
    # -- a typo is a character or two out of place, never a name three characters shorter.
    # Deliberate fragments ("trans" for "transform_scale") never reach this line; they have
    # already matched as a substring above.
    if abs(len(term) - len(candidate)) > _TYPO_SLACK:
        return False
    return difflib.SequenceMatcher(None, term, candidate).ratio() >= threshold


def chunk_matches_search(chunk: "core.ChunkBase", query: str, scope: str = "both") -> bool:
    """True if chunk's name and/or type (per scope) fuzzy-matches query. Empty query always
    matches (no search filtering in effect)."""
    if not query.strip():
        return True
    candidates = []
    if scope in ("both", "name"):
        candidates.append(chunk.name)
    if scope in ("both", "type"):
        cfg = chunk.get_config()
        if cfg and cfg.module:
            candidates.append(cfg.module)
    return any(fuzzy_matches(query, c) for c in candidates)


def find_group_active_view(
    group: "core.ChunkBase", stage: "Usd.Stage", scene: "core.SceneSender"
) -> "core.ChunkBase | None":
    """The inner active view of a group — its output driver. Reads the group prim's
    ``chunk:activeView`` slot (written by the Qt layer when the view is switched inside the
    group), falling back to the first inner view. None if the group has no inner view.
    """
    inner_views = [
        c for c in scene.get_chunks() if is_view_chunk(c) and is_node_in_context(c, group)
    ]
    if not inner_views:
        return None
    gp = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(group)))
    slot = None
    if gp and gp.IsValid():
        a = gp.GetAttribute("chunk:activeView")
        if a and a.IsValid():
            slot = a.Get()
    if slot:
        for v in inner_views:
            vp = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(v)))
            if vp and vp.IsValid() and vp.GetAttribute("view:slot").Get() == slot:
                return v
    return inner_views[0]


def find_view_chunk_for_slot(
    slot: int,
    stage: "Usd.Stage",
    scene: "core.SceneSender",
    context: "core.ChunkBase | None" = None,
) -> "core.ChunkBase | None":
    """Return the view chunk assigned to slot, or None. Scoped to `context` when given — each
    graph (tab or group) has its own slots, so a tab view and a group view can both be slot 1."""
    for c in scene.get_chunks():
        if not is_view_chunk(c):
            continue
        if context is not None and not is_node_in_context(c, context):
            continue
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
        if prim.IsValid() and prim.GetAttribute("view:slot").Get() == slot:
            return c
    return None


def get_view_exec_sources(
    view_chunk: "core.ChunkBase",
    stage: "Usd.Stage",
    scene: "core.SceneSender",
) -> "list[core.ChunkBase]":
    """Return the chunks wired into view_chunk's inputs:chunk."""
    prim_to_chunk = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(view_chunk)))
    if not prim.IsValid():
        return []
    exec_in = prim.GetAttribute(_EXEC_INPUT)
    if not exec_in.IsValid():
        return []
    return [
        c for conn in exec_in.GetConnections()
        if (c := prim_to_chunk.get(str(conn.GetPrimPath()))) is not None
    ]


def get_exec_downstream_chunks(
    start: "core.ChunkBase",
    stage: "Usd.Stage",
    scene: "core.SceneSender",
) -> "set[core.ChunkBase]":
    """BFS forward through inputs:chunk connections from start.

    Returns all downstream ChunkBase objects (start itself is excluded).
    """
    prim_to_chunk = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
    start_path = get_chunk_prim_path(start)
    downstream: set = set()
    frontier = {start_path}
    while frontier:
        next_frontier: set = set()
        for prim in stage.Traverse():
            path = str(prim.GetPath())
            if path in downstream or path == start_path:
                continue
            # Every slot — a merge fed through slot 2 is downstream of that branch just as
            # much as one fed through slot 0.
            if any(str(conn.GetPrimPath()) in frontier for conn in exec_input_connections(prim)):
                next_frontier.add(path)
                if c := prim_to_chunk.get(path):
                    downstream.add(c)
        frontier = next_frontier
    return downstream


def get_upstream_stage(stage: "Usd.Stage", scene: "core.SceneSender", chunk: "core.ChunkBase") -> "Usd.Stage":
    """A throwaway stage composing every chunk layer except `chunk` itself and its real
    downstream (plus the raw root layer), for a read that must never be influenced by a
    downstream chunk's edit to the same prim -- e.g. Scatter reading a mesh a Transform wired
    *after* it also targets.

    Unlike exec_with_hidden_view, this never mutates the real stage, mutes any real layer, or
    executes anything -- it's a pure composition of already-existing layers into a scratch
    root, discarded once the caller is done reading. That makes it safe to call unconditionally
    from inside a chunk's own execute() (reactive param changes included), which is where it's
    actually needed: a plain param edit never goes through recook_for_view at all, so nothing
    else protects that path. Chunks are included by "not downstream of me", not "reachable
    upstream of me" — a mesh from an unwired sibling chunk (e.g. an Add Mesh chunk the user
    picked by path, never wired into the exec chain) still resolves correctly; only genuine
    downstream chunks are excluded.

    Included layers keep their REAL relative strength order (read off the actual graph layers'
    subLayerPaths), not scene.get_chunks()'s iteration order. Two included chunks can both
    touch the same prim without conflicting with each other (e.g. whatever created a mesh, and
    a Transform rotating it, both authoring xformOp:transform on the same prim) -- the real
    order already correctly resolves that via the existing sort-key/view-driven ranking, and
    rebuilding it from scratch here would silently pick a different, wrong winner.
    """
    lm = chunk_registry.ChunkLayerManager()
    excluded = get_exec_downstream_chunks(chunk, stage, scene) | {chunk}
    excluded_ids = {layer.identifier for c in excluded if (layer := lm.get_layer(c)) is not None}
    layer_ids: list = []
    for g_layer in lm.graph_layers.values():
        for ident in g_layer.subLayerPaths:
            if ident in excluded_ids or ident in layer_ids:
                continue
            if stage.IsLayerMuted(ident):
                # A muted layer is one the real stage is currently showing nothing of: its
                # chunk is dormant -- blocked (outside the active view's cook set, e.g. just
                # disconnected) or explicitly disabled. Rebuilding the composition from raw
                # subLayerPaths resurrected it here, so a dormant chunk's stale content kept
                # driving everyone else's reads while being invisible everywhere else.
                #
                # That got WORSE the moment a chunk was disconnected, which is backwards:
                # while it was still wired downstream the exclusion above already covered it,
                # and cutting it loose is what let it back in. Live-reported: shaking a Prune
                # node off a Sublayer -> Transform -> Scatter -> Prune -> View chain brought
                # the apple back on the real stage, but Scatter still read it through Prune's
                # orphaned "active = false" -- so Scatter found no surface geometry at all and
                # fell back to scattering around the origin, looking like the Transform
                # feeding it had silently stopped being applied.
                continue
            layer_ids.append(ident)
    scratch_root = Sdf.Layer.CreateAnonymous()
    scratch_root.subLayerPaths = layer_ids + [stage.GetRootLayer().identifier]
    return Usd.Stage.Open(scratch_root)


def heal_exec_chain_on_delete(stage: "Usd.Stage", deleted_path: str) -> None:
    """Rewire around a node about to be deleted so its exec chain survives.

    For A -> X -> B, deleting X must leave A -> B (each successor B's inputs:chunk is repointed
    to X's predecessor(s) A; cleared if X had none). Call while X's prim still exists — its
    inputs:chunk predecessor is only readable before removal. Exec only (inputs/outputs:chunk);
    data/typed connections to X are left to drop with the prim.
    """
    deleted = Sdf.Path(deleted_path)
    del_prim = stage.GetPrimAtPath(deleted)
    if not (del_prim and del_prim.IsValid()):
        return
    # The deleted node's PREDECESSOR is read from slot 0 only, on purpose: a successor has a
    # single input to inherit into, so it can be handed exactly one branch, and slot 0 is the
    # primary/strongest one. Deleting a merge therefore grafts its top branch onto whatever
    # came after it, rather than fanning all of them into a chunk that cannot express fan-in.
    exec_in = del_prim.GetAttribute(_EXEC_INPUT)
    pred_conns = list(exec_in.GetConnections()) if (exec_in and exec_in.IsValid()) else []
    out_path = f"{deleted_path}.{_EXEC_OUTPUT}"
    touched_merges = []
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        for prim in stage.Traverse():
            if prim.GetPath() == deleted:
                continue
            # SUCCESSORS, by contrast, must be found across every slot. A merge fed through
            # slot 2 is just as much a successor as one fed through slot 0, and skipping it
            # would leave that slot connected to a prim that is about to stop existing.
            for succ in iter_exec_inputs(prim):
                if not any(str(c) == out_path for c in succ.GetConnections()):
                    continue
                # Write to succ's OWN home layer (or the session layer as a last resort),
                # not wherever the stage's ambient edit target happens to be (the root
                # layer, since nothing here ever calls SetEditTarget) -- an interactively
                # wired connection almost always lives on the session layer, which is
                # stronger than root. Healing on root left the original, still-composed
                # session-layer opinion (pointing at the just-deleted node) in charge, so
                # the "healed" A->B edge was silently shadowed away by composition and
                # never actually took effect. Same pattern as _author_connection /
                # _author_raw_connection.
                home = chunk_utils.get_attr_home_layer(succ) or chunk_utils.get_bookkeeping_layer(stage, None)
                with Usd.EditContext(stage, home):
                    if pred_conns:
                        succ.SetConnections(pred_conns)
                    else:
                        succ.ClearConnections()
                if is_merge_prim(prim):
                    touched_merges.append(prim)
    finally:
        chunk_notice._usd_write_active = prev_w
    # Suppressing notices above means opting out of the choke point that normally keeps the
    # merge slot invariant (_reconcile_changed_exec_inputs), so restore it here instead: a
    # slot cleared because its branch was deleted has to compact like any other.
    for prim in touched_merges:
        normalize_merge_slots(stage, prim)
    # Healing rewrites inputs:chunk across the graph with notices suppressed — every
    # memoised answer below the deleted node is now measured against an edge that is gone.
    invalidate_edit_layer_memo()


def _remove_attr_spec(stage: "Usd.Stage", prim: "Usd.Prim", name: str) -> None:
    """Remove an attribute wherever it is actually authored.

    Usd.Prim.RemoveProperty only unauthors from the stage's *current* edit target, which for
    chunk bookkeeping is routinely not where the spec lives — an interactively wired
    connection sits on the session layer, a loaded one in the file. Removing from the wrong
    layer leaves the real opinion composed and in charge, the same failure mode
    heal_exec_chain_on_delete documents. So sweep the layer stack and drop every spec.
    """
    prim_path = prim.GetPath()
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        prim_spec = layer.GetPrimAtPath(prim_path)
        if prim_spec and name in prim_spec.properties:
            del prim_spec.properties[name]


def normalize_merge_slots(stage: "Usd.Stage", prim: "Usd.Prim") -> bool:
    """Enforce a Merge chunk's slot invariant: connected exec inputs occupy slots 0..N-1 with
    no gaps.

    Clearing a middle slot compacts the rest up, so slot order (which is what decides whose
    opinion wins) always matches the port order the user sees on the node, rather than
    drifting into a question about invisible attribute names.

    Judged on the CONNECTED slots alone — an exec attribute that exists but connects nothing
    is deliberately left in place. It has to be: this runs synchronously from the stage notice
    that CreateAttribute itself fires, so an authoring path which creates the attribute and
    then connects it (_author_connection does exactly that, and so does the numbered-slot
    branch of _on_dependency_added) would otherwise have the attribute deleted underneath it
    between the two statements, and would go on to call SetConnections on an invalid handle.
    The cost is that clearing the LAST slot can leave a connectionless inputs:chunkN behind;
    it is inert — it carries no connection, `_build_ports` counts connections rather than
    attributes, and the next compaction sweeps it — so it is preferable to breaking the
    authoring paths.

    No-ops for anything that isn't a merge, and for a merge already in shape, so it is safe
    to call on every exec-input change.

    Returns:
        bool: True if any slot was rewritten
    """
    if not is_merge_prim(prim):
        return False
    current = [(attr, list(attr.GetConnections())) for attr in iter_exec_inputs(prim)]
    live = [(attr, conns) for attr, conns in current if conns]
    desired_names = [exec_input_name(i) for i in range(len(live))]
    if [attr.GetName() for attr, _ in live] == desired_names:
        return False

    # Resolve each surviving branch's home layer BEFORE unauthoring anything, so a moved
    # connection is re-authored in the layer it was already living in rather than being
    # silently relocated (which could put it somewhere weaker than a spec left elsewhere).
    moves = [
        (
            desired_names[i],
            conns,
            chunk_utils.get_attr_home_layer(attr) or chunk_utils.get_bookkeeping_layer(stage, None),
        )
        for i, (attr, conns) in enumerate(live)
    ]

    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        # Clear every slot first, then re-author. Renaming in place would have to worry about
        # a move landing on a slot that is still occupied by the branch below it.
        for attr, _ in current:
            _remove_attr_spec(stage, prim, attr.GetName())
        for name, conns, home in moves:
            with Usd.EditContext(stage, home):
                attr = prim.CreateAttribute(name, Sdf.ValueTypeNames.Token, custom=False)
                attr.SetConnections(conns)
    finally:
        chunk_notice._usd_write_active = prev_w
    # Slots moved with notices suppressed, so nothing else will drop the memoised
    # nearest-upstream-Edit-Target answers measured against the old wiring.
    invalidate_edit_layer_memo()
    return True


def is_edit_target_chunk(chunk: "core.ChunkBase") -> bool:
    """Return True if chunk is an Edit Target chunk (scripts/edit_target.py)."""
    cfg = chunk.get_config()
    return bool(cfg and cfg.module == "edit_target")


def _prim_edit_target_layer(stage: "Usd.Stage", prim: "Usd.Prim") -> "Sdf.Layer | None":
    """If prim carries the Edit Target marker, return the Sdf.Layer it names — but only if
    that layer is actually in the stage's current layer stack (so the result is always safe
    to author into). Otherwise None."""
    attr = prim.GetAttribute(_EDIT_TARGET_MARKER)
    if not (attr and attr.IsValid()):
        return None
    ident = attr.Get()
    if not ident:
        return None
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        if layer.identifier == ident:
            return layer
    return None


def _iter_exec_upstream(stage: "Usd.Stage", start: "Sdf.Path"):
    """Breadth-first walk of the exec chain (inputs:chunk connections) upstream from
    start, yielding each reachable (path, prim, unbranched) exactly once, start itself
    excluded. prim may be invalid (dangling connection) — such nodes are yielded but not
    walked through. Shared by every "look upstream" consumer so the traversal semantics
    (dedup, ordering, dangling handling) live in one place.

    unbranched says everything visited up to and including this node forms a single chain
    start -> ... -> node, with no level holding more than one node. That is the condition
    under which "what does the walk from here find" is the same question as "what does the
    walk from start find", which is what lets nearest_upstream_edit_layer share one answer
    along the run instead of re-deriving it per node. It goes False for good at the first
    fork and never comes back.

    Deliberately exec-only (inputs:chunk), NOT the exec+data walk get_exec_upstream_chunks
    does: Edit Target resolution follows the execution chain that actually defines
    authoring position, and must ignore incidental data wiring — a chunk reading another's
    points shouldn't drag its Edit Target upstream. The two are separate on purpose."""
    seen = {start}
    frontier = [start]
    unbranched = True
    while frontier:
        next_frontier: list = []
        for path in frontier:
            p = stage.GetPrimAtPath(path)
            if path != start:
                yield path, p, unbranched
            if not (p and p.IsValid()):
                continue
            exec_in = p.GetAttribute(_EXEC_INPUT)
            if exec_in and exec_in.IsValid():
                for conn in exec_in.GetConnections():
                    conn_prim = conn.GetPrimPath()
                    if conn_prim not in seen:
                        seen.add(conn_prim)
                        next_frontier.append(conn_prim)
        unbranched = unbranched and len(next_frontier) == 1
        frontier = next_frontier


_edit_layer_memo: "dict[str, Sdf.Layer]" = {}
_edit_layer_memo_stage: "weakref.ref | None" = None
_edit_layer_memo_frozen = False


def invalidate_edit_layer_memo() -> None:
    """Drop the memoised nearest_upstream_edit_layer answers.

    Must be called by anything that can change what that function returns: the exec chain
    it walks (an inputs:chunk connection appearing, moving or being healed away), the Edit
    Target markers it looks for, or the stage's layer stack (a marker only counts while the
    layer it names is actually composed).

    Two kinds of write are exempt, both because they provably cannot move an answer: a
    frozen block (see freeze_edit_layer_memo) and ChunkLayerManager rearranging its own
    internal layers (see chunk_notice._internal_layer_write_active).
    """
    if _edit_layer_memo_frozen or chunk_notice._internal_layer_write_active:
        return
    _edit_layer_memo.clear()


@contextlib.contextmanager
def freeze_edit_layer_memo():
    """Hold the memo across a block whose writes provably cannot move any answer.

    Only for authoring that rearranges *where* an already-composed opinion is stored —
    _migrate_chunk_bookkeeping's per-property strong -> weak consolidation, which by
    construction leaves both inputs:chunk and chunk:editTargetLayer composing exactly what
    they composed before. Those writes still resync prims, so without this every migration
    in a pass throws away the answers the pass just computed, and each of the next chunk's
    answers is re-walked from scratch up the whole chain above it.

    Never wrap a block that can author a new chunk prim, an exec connection or a layer
    stack change: those are the three things the memo is a cache of.
    """
    global _edit_layer_memo_frozen
    prev = _edit_layer_memo_frozen
    _edit_layer_memo_frozen = True
    try:
        yield
    finally:
        _edit_layer_memo_frozen = prev


def nearest_upstream_edit_layer(stage: "Usd.Stage", prim: "Usd.Prim") -> "Sdf.Layer | None":
    """Return the layer a chunk at prim's position should author into: the layer published
    by the nearest Edit Target chunk reachable by walking the exec chain (inputs:chunk)
    upstream from prim, or the session layer if there is no upstream Edit Target.

    A pure function of graph topology — this is what replaces the old global
    ChunkStageSync._user_edit_layer state. prim's own marker is deliberately skipped (an
    Edit Target chunk defines the target for everything *downstream* of it, not for its own
    bookkeeping — that follows whatever is upstream of it, or the session layer).

    Memoised per prim path, because being a pure function of topology also makes it a walk
    of the entire exec chain above prim, and it is asked once per get_edit_context — several
    times per chunk. On a long chain that is quadratic and was the single largest cost of
    opening a saved graph. The memo is dropped wholesale by invalidate_edit_layer_memo()
    rather than aged per entry: one chunk's answer depends on every chunk above it, so there
    is no cheaper unit to invalidate than "all of it".
    """
    if not stage:
        return None
    session = stage.GetSessionLayer()
    if not (prim and prim.IsValid()):
        return session

    # Answers are keyed by prim path, and prim paths repeat across stages — /chunks/Dot in a
    # file just opened is the same string as /chunks/Dot in the stage it replaced, while the
    # layer it resolves to belongs to the old stage and is in no layer stack at all now.
    # Authoring against that layer loses the write; migrating a chunk into it strips the
    # chunk's prim from the stage it is actually on. The memo serves one stage at a time, so
    # a different stage starts a new one.
    global _edit_layer_memo_stage
    if _edit_layer_memo_stage is None or _edit_layer_memo_stage() is not stage:
        _edit_layer_memo.clear()
        _edit_layer_memo_stage = weakref.ref(stage)

    key = str(prim.GetPath())
    if (memoised := _edit_layer_memo.get(key)) is not None:
        return memoised

    result = session
    # Every node on an unbranched run above prim has this same answer, so the walk both
    # stops at the first one already memoised and leaves the answer on the rest. Without
    # that the memo only ever holds the node that was asked about, and a chain restored or
    # reconciled node by node re-walks the whole thing above each one in turn.
    shared: "list[str]" = []
    for path, p, unbranched in _iter_exec_upstream(stage, prim.GetPath()):
        if not (p and p.IsValid()):
            continue
        layer = _prim_edit_target_layer(stage, p)
        if layer is not None:
            result = layer
            break
        if not unbranched:
            continue
        upstream_key = str(path)
        if (upstream := _edit_layer_memo.get(upstream_key)) is not None:
            result = upstream
            break
        shared.append(upstream_key)

    _edit_layer_memo[key] = result
    for shared_key in shared:
        _edit_layer_memo[shared_key] = result
    return result


def _promote_prim_spec(dst_spec: "Sdf.PrimSpec", holders: "list[Sdf.Layer]", sdf_path: "Sdf.Path") -> None:
    """Give dst_spec the specifier/typeName the composed prim currently has, if it lacks them.

    Only fills gaps: an existing `def` or an already-authored typeName on dst_spec is left
    alone, so a destination that already carries the real spec keeps its own prim metadata.
    Resolution matches composition — `def` anywhere in the stack wins over `over` regardless
    of strength, while typeName is strongest-wins (holders is strong -> weak).
    """
    if dst_spec.specifier != Sdf.SpecifierDef and any(
        layer.GetPrimAtPath(sdf_path).specifier == Sdf.SpecifierDef for layer in holders
    ):
        dst_spec.specifier = Sdf.SpecifierDef
    if not dst_spec.typeName:
        dst_spec.typeName = next(
            (tn for layer in holders if (tn := layer.GetPrimAtPath(sdf_path).typeName)), ""
        )


def _apply_connection_list_op(spec: "Sdf.PropertySpec", op: "Sdf.PathListOp") -> None:
    """Author op onto spec's connections, replacing whatever it had.

    connectionPaths is a read-only field for SetInfo, so the list op has to be replayed
    through the connectionPathList proxy one edit list at a time.
    """
    proxy = spec.connectionPathList
    proxy.ClearEdits()
    if op.isExplicit:
        proxy.explicitItems = list(op.explicitItems)
        return
    proxy.addedItems = list(op.addedItems)
    proxy.prependedItems = list(op.prependedItems)
    proxy.appendedItems = list(op.appendedItems)
    proxy.deletedItems = list(op.deletedItems)
    proxy.orderedItems = list(op.orderedItems)


def _migratable_layers(stage: "Usd.Stage") -> "list[Sdf.Layer]":
    """The stage's layer stack, strong -> weak, minus the chunk graph's own internal
    procedural layers — the layers a chunk's bookkeeping may legitimately be moved between.

    Its own function because it is loop-invariant across a reconcile pass but costs the
    size of the layer stack, and the layer stack grows with the graph: every chunk carries
    a procedural layer. Recomputed per chunk, it makes a whole-scene pass quadratic on
    exactly the graphs that pass exists to keep correct.

    Each layer is asked directly whether the chunk system owns it, rather than matched
    against a set collected by walking every tracked layer's sublayers. Collecting that set
    is itself proportional to the graph, and this runs once per pass, of which there is one
    per wire — so the walk was most of the cost of building a graph's worth of them.
    """
    return [
        layer for layer in stage.GetLayerStack(includeSessionLayers=True)
        if not chunk_registry.is_chunk_internal_layer(layer)
    ]


def _migrate_chunk_bookkeeping(
    stage: "Usd.Stage",
    prim_path: str,
    dst_layer: "Sdf.Layer",
    migratable: "list[Sdf.Layer] | None" = None,
) -> bool:
    """Consolidate a chunk prim's bookkeeping spec into dst_layer, removing it from any other
    migratable layer it currently sits in. Returns True if anything moved.

    "Migratable" excludes the chunk graph's own internal per-chunk procedural layers — those
    hold driven content that is Clear()ed and regenerated on execute (and re-nested under the
    right target by ChunkLayerManager), never hand-moved.

    Consolidation is per-property, resolved strong -> weak across every holder INCLUDING
    dst_layer, so what survives is exactly what was composing before the move. A whole-spec
    Sdf.CopySpec cannot do this: it REPLACES the destination, so it drops every field the
    copy source happens not to carry. Both directions of that actually bite here, because a
    chunk's bookkeeping is routinely split — the real spec (info:chunkType, view:slot,
    ui:nodegraph:node:pos) in the session layer, plus a bare `over` holding just inputs:chunk
    left in the root layer by a write that went through the stage's ambient edit target:

      - copying the root `over` over the session spec loses view:slot, and without it
        find_view_chunk_for_slot finds no view at all, collapsing the cook set to
        "nothing cooks";
      - keeping the session spec and merely pruning the root `over` loses inputs:chunk,
        which unwires the chunk from its exec chain.

    dst_layer keeps its own prim-level metadata (specifier, typeName) when it already has a
    spec; only a dst with nothing there yet takes those from the strongest holder. The one
    exception is a dst spec that is a bare `over` with no typeName — the shape left behind by
    an ordinary attr write that happened to target dst while this chunk's `def` still lived
    elsewhere. Keeping that as-is while the sources (holding the only `def`) are pruned below
    would leave the chunk prim undefined and typeless across the whole stack, so it vanishes
    from any Usd.PrimIsDefined traversal — the node graph stops drawing a node for it.
    """
    sdf_path = Sdf.Path(prim_path)
    # migratable is the same list for every chunk in a pass; a caller doing a pass hands it
    # in once instead of paying for it per chunk. It must be re-read after anything that can
    # open or sublayer a layer — see _migratable_layers.
    if migratable is None:
        migratable = _migratable_layers(stage)
    holders = [layer for layer in migratable if layer.GetPrimAtPath(sdf_path)]
    sources = [layer for layer in holders if layer != dst_layer]
    if not sources:
        return False
    winners: "dict[str, Sdf.Layer]" = {}
    # Connections resolve as their own field, not with the property spec that holds them.
    # A bare attr spec (a CreateAttribute that authored no connection) can sit in a stronger
    # layer than the spec actually carrying the wire — it wins `winners` below, its
    # connection-less spec is what gets copied, and pruning the sources then drops the
    # connection for good: a view loaded from file loses the inputs:chunk feeding it. So
    # snapshot the strongest *authored* connection opinion per property up front — before
    # any copying rewrites dst — and re-apply it after the copies.
    conn_ops: "dict[str, Sdf.PathListOp]" = {}
    for layer in holders:  # GetLayerStack is strong -> weak, so first seen is strongest
        for name, prop in layer.GetPrimAtPath(sdf_path).properties.items():
            winners.setdefault(name, layer)
            if name not in conn_ops and prop.HasInfo("connectionPaths"):
                conn_ops[name] = prop.GetInfo("connectionPaths")
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        if dst_layer.GetPrimAtPath(sdf_path) is None:
            Sdf.CreatePrimInLayer(dst_layer, sdf_path)
            Sdf.CopySpec(holders[0], sdf_path, dst_layer, sdf_path)
        else:
            _promote_prim_spec(dst_layer.GetPrimAtPath(sdf_path), holders, sdf_path)
        for name, layer in winners.items():
            if layer == dst_layer:
                continue  # already the winning opinion, and copying onto itself is a no-op
            prop_path = sdf_path.AppendProperty(name)
            Sdf.CopySpec(layer, prop_path, dst_layer, prop_path)
        for name, op in conn_ops.items():
            dst_prop = dst_layer.GetPropertyAtPath(sdf_path.AppendProperty(name))
            if dst_prop is not None:
                _apply_connection_list_op(dst_prop, op)
        for layer in sources:
            parent = layer.GetPrimAtPath(sdf_path.GetParentPath())
            if parent and sdf_path.name in parent.nameChildren:
                del parent.nameChildren[sdf_path.name]
    finally:
        chunk_notice._usd_write_active = prev
    return True


_reconciling = False


def _chunks_downstream_of(roots: "list[core.ChunkBase]") -> "set[int]":
    """ids of every chunk that solves at or after one of roots.

    Read off the in-memory DAG rather than by traversing the stage for inputs:chunk
    connections: the exec chain is mirrored into that graph as it is built, so the answer is
    already there, and getting it costs the size of the answer instead of the size of the
    scene. Held as ids because ChunkBase's __hash__ is by chunk hash, and this only ever
    needs identity.

    A superset of the exec-only downstream — the DAG also carries param wiring and ownership
    — which is what makes it safe to select on: reconciling a chunk whose target has not
    moved does nothing, so over-including costs a no-op while under-including would leave a
    chunk homed against a target that no longer applies to it.
    """
    found: "set[int]" = set()
    for root in roots:
        for node in chunk_scheduler.downstream_closure(root.get_dag_node()):
            if isinstance(node.propagator, core.ChunkBase):
                found.add(id(node.propagator))
    return found


def connect_exec(stage: "Usd.Stage", scene: "core.SceneSender", src_prim_path: str, dst_prim_path: str) -> "Usd.Attribute | None":
    """Author a plain outputs:chunk -> inputs:chunk exec connection between two chunk
    prims, without going through the Qt node graph.

    For a caller that creates chunks directly (a chunk module wiring its own children,
    a test, a CLI) rather than via ChunkGraphView's drag-to-connect: view-driven execution
    (recook_for_view) mutes any chunk not reachable from its context's active view, so a
    programmatically created chunk that's never wired into the exec chain gets hidden the
    first time its context recooks, even though it authored real content. Mirrors the
    exec-only path of ChunkGraphView._author_connection; does not handle typed param
    connections or merge chunks' numbered inputs:chunkN slots, neither of which this needs.

    Deliberately does NOT also trigger a recook the way _author_connection does for a
    live drag-to-connect: figuring out which view (if any) is safe to recook through from
    inside a headless caller, without touching mute/block state anywhere it doesn't have
    full information about, turned out to be its own source of bugs (muting unrelated
    scene content, or the chunk just wired itself, depending on what existed nearby at the
    time). Simpler and safer to just author the connection correctly here and let the
    caller's own next real recook (a view switch, another wire, diving into the context)
    pick it up -- which it will, now that the connection and the layer-ranking logic
    downstream of it are both correct.

    Returns the authored inputs:chunk attribute, or None if either prim couldn't be
    resolved.
    """
    dst_prim = stage.GetPrimAtPath(Sdf.Path(dst_prim_path))
    if not dst_prim.IsValid():
        return None
    dst_attr = dst_prim.GetAttribute(_EXEC_INPUT)
    if not (dst_attr and dst_attr.IsValid()):
        dst_attr = dst_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        if not dst_attr.IsValid():
            return None
    dst_chunk = next((c for c in scene.get_chunks() if get_chunk_prim_path(c) == dst_prim_path), None)
    src_conn_path = Sdf.Path(f"{src_prim_path}.{_EXEC_OUTPUT}")
    home = chunk_utils.get_attr_home_layer(dst_attr)
    target = home or chunk_utils.get_bookkeeping_layer(stage, dst_chunk)
    with Usd.EditContext(stage, target):
        dst_attr.SetConnections([src_conn_path])
    reconcile_edit_targets(stage, scene)
    return dst_attr


def reconcile_edit_targets(
    stage: "Usd.Stage",
    scene: "core.SceneSender",
    roots: "list[core.ChunkBase] | None" = None,
    cook: bool = True,
    protect_root_layer: bool = False,
) -> None:
    """Make every chunk's bookkeeping consistent with the Edit Target resolved for its
    exec-chain position.

    For each regular (non-CONTEXT) chunk whose effective edit layer
    (nearest_upstream_edit_layer) no longer matches where its bookkeeping prim lives, move
    the prim spec there and re-execute it so ChunkLayerManager.get_chunk_layer re-nests the
    chunk's driven content under the new target too (its graph layer reparents — see
    chunk_registry.get_chunk_layer). A chunk already homed correctly moves nothing and is
    NOT re-executed, so on the common paths (creation authors bookkeeping into the right
    layer up front via _new_chunk_bookkeeping_layer) this is a cheap no-op and never
    oscillates. It does real work only when a target actually changes — which is exactly
    when publish_edit_target gates its call on the marker having changed.

    Idempotent, so safe after any structural change (attach/detach/delete of an exec wire,
    or an Edit Target changing its layer). CONTEXT chunks (Tab containers, groups) are
    structural scaffolding pinned to the session layer, not content, so they are left alone.

    roots narrows the pass to those chunks and everything downstream of them. A change at one
    place in the graph can only move the target of chunks at or below it, so a caller that
    knows where the change happened (wiring an edge, an Edit Target picking a new layer) can
    say so and skip the rest. Left as None the whole scene is visited, which is what a load or
    a delete — where the change is not localised to one chunk — still wants. This matters
    because the pass is O(chunks) and every wire fires one: on a graph of any size, visiting
    the whole scene per edge is what made building it quadratic.

    cook=False migrates without re-running anything, for the one caller that is about to cook
    the whole graph anyway: loading. There the re-run is not just redundant but quadratic —
    each migration would cascade a solve over everything downstream of it, once per chunk.
    """
    global _reconciling
    if _reconciling or not (stage and scene):
        return
    _reconciling = True
    try:
        affected = _chunks_downstream_of(roots) if roots is not None else None
        migratable = _migratable_layers(stage)
        if protect_root_layer:
            # Opening a file must not rewrite it. _migrate_chunk_bookkeeping moves a spec by
            # DELETING the original, and on a freshly opened file the original lives in that
            # file's own root layer -- so a load re-homing anything gutted the layer it had
            # just read. Sdf caches layers per process, so the emptied layer is what the next
            # Usd.Stage.Open of the same path gets: loading a network twice in one process
            # restored only the context the second time, while the file on disk stayed
            # correct. Excluding just the root layer keeps re-homing between the session and
            # anon layers, which is what settles the graph during a load.
            root = stage.GetRootLayer()
            migratable = [layer for layer in migratable if layer != root]
        for chunk in scene.get_chunks():
            if affected is not None and id(chunk) not in affected:
                continue
            if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                continue
            prim_path = get_chunk_prim_path(chunk)
            prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
            if not prim.IsValid():
                continue
            dst = nearest_upstream_edit_layer(stage, prim)
            if dst is None:
                continue
            # Migration only moves opinions between layers, never changes what they
            # compose to, so the pass keeps the answers it has computed instead of
            # dropping them at the first move and re-walking the whole chain above the
            # chunk after it. The re-run below is outside the freeze — that one can
            # author anything.
            with freeze_edit_layer_memo():
                moved = _migrate_chunk_bookkeeping(stage, prim_path, dst, migratable)
            if moved and cook:
                # The chunk moved layers — re-run it so its driven content re-nests
                # under the new target (bookkeeping is hand-moved above; driven
                # content follows via ChunkLayerManager on execute).
                chunk.execute()
                # A cook can create and sublayer this chunk's procedural layer, so the
                # migratable set is stale from here on.
                migratable = _migratable_layers(stage)
                if protect_root_layer:
                    migratable = [layer for layer in migratable if layer != stage.GetRootLayer()]
    finally:
        _reconciling = False


def snapshot_edit_target_selections(stage: "Usd.Stage") -> "dict[str, str]":
    """Read every Edit Target prim's stored selection straight off the stage, keyed by prim
    path. Must be called BEFORE load_chunks — restore_edit_target_selections needs it.

    Loading is itself destructive to these values. When a chunk.Menu is handed a value that
    isn't (yet) one of its menu_items, edit_target.py's refresh handler resets it to the
    default, and that reset is a param write, so it lands back in USD — overwriting the saved
    selection with "Session Layer". Mid-load that happens routinely: an Edit Target pointing
    at "A" is rebuilt before the upstream Create Layer chunk has recreated layer A, so "A"
    isn't a menu item yet and the selection is destroyed on the way in.

    Reading afterwards therefore can't recover it, however carefully — by then USD holds
    "Session Layer" and the real answer exists only in the file on disk. This runs first,
    while the stage still has exactly what was saved, and needs no chunks to exist yet since
    it reads raw attrs off prims.
    """
    saved: "dict[str, str]" = {}
    if not stage:
        return saved
    for prim in stage.Traverse():
        type_attr = prim.GetAttribute(_CHUNK_TYPE_ATTR)
        if not (type_attr and type_attr.IsValid()) or type_attr.Get() != "edit_target":
            continue
        attr = prim.GetAttribute("inputs:edit_target")
        stored = attr.Get() if (attr and attr.IsValid()) else None
        if stored:
            saved[str(prim.GetPath())] = stored
    return saved


def restore_edit_target_selections(
    stage: "Usd.Stage", scene: "core.SceneSender", saved: "dict[str, str] | None" = None
) -> None:
    """After a load, re-apply each Edit Target chunk's stored selection.

    A chunk.Menu rejects a value not in its menu_items, and on load the stored selection
    ("shot") is restored before the anon layer it names is recreated by the upstream Create
    Layer chunk — so the Menu silently drops it and keeps the default ("Session Layer"). By
    the time the whole graph has loaded, every Create Layer chunk has run, so the layers (and
    therefore the menu items) exist; re-applying the stored value here restores the selection.
    The resulting value change re-publishes the marker and reconciles downstream via the
    normal path.

    saved is the pre-load snapshot from snapshot_edit_target_selections, and is the only
    trustworthy source for these values: the Menu's rejection does not merely drop the value
    in memory, it writes the default back to USD, so a selection whose layer didn't exist yet
    at the moment its chunk was rebuilt is already gone from the stage by the time this runs.
    Falls back to reading the stage when no snapshot is supplied, which still covers any
    selection that happened to survive load.

    Runs strictly post-load — NOT from the refresh callback mid-load — so a chain of Edit
    Targets settles deterministically. Applied UPSTREAM-FIRST: re-applying a downstream Edit
    Target before an upstream one leaves the upstream still on its (rejected) default while
    the downstream's execute/reconcile cascade runs, which corrupts the upstream's selection.
    Ordering by how many Edit Targets sit upstream keeps each chain settling from its root out.
    """
    if not (stage and scene):
        return
    edit_targets = [c for c in scene.get_chunks() if is_edit_target_chunk(c)]
    if not edit_targets:
        return
    et_paths = {get_chunk_prim_path(c) for c in edit_targets}

    def _upstream_et_count(chunk) -> int:
        start = Sdf.Path(get_chunk_prim_path(chunk))
        return sum(
            1 for path, _p, _unbranched in _iter_exec_upstream(stage, start)
            if str(path) in et_paths
        )

    # Resolve each stored selection BEFORE applying any of them. Applying one target's value
    # triggers an execute/reconcile cascade that can overwrite a *different* target's stored
    # inputs:edit_target attr, so re-reading per-iteration would pick up a corrupted value.
    # Resolve first, then apply.
    snapshot = []
    for chunk in edit_targets:
        prim_path = get_chunk_prim_path(chunk)
        prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not prim.IsValid():
            continue
        stored = (saved or {}).get(prim_path)
        if not stored:
            attr = prim.GetAttribute("inputs:edit_target")
            stored = attr.Get() if (attr and attr.IsValid()) else None
        param = chunk.get_param("edit_target")
        if param is not None and stored:
            snapshot.append((chunk, param, stored))

    # Apply upstream-first: each set's own publish_edit_target reconcile re-homes the
    # downstream part of the chain before the next (more-downstream) selection is applied,
    # so every target settles against an already-correct upstream.
    for chunk, param, stored in sorted(snapshot, key=lambda t: _upstream_et_count(t[0])):
        # Only re-apply once the stored selection is actually a valid menu item now (its
        # layer exists); a genuinely missing layer is left at whatever the Menu settled on.
        if param.value != stored and stored in getattr(param, "menu_items", []):
            param.value = stored


def _active_scene() -> "core.SceneSender | None":
    """The SceneSender to reconcile against, or None if there is no scene yet.

    ChunkStageSync is the usual holder, but it only gains a _scene once initialize() has run,
    and the headless CLI (usdchunks run) deliberately never initializes it — it loads chunks
    straight onto a stage with no live sync. The registry's scene is set by every host
    including that one, so it's the reliable fallback.
    """
    sync = ChunkStageSync()
    if sync.initialized and getattr(sync, "_scene", None):
        return sync._scene
    registry = chunk_registry.ChunkRegistry()
    return registry.scene if registry.initialized else None


def publish_edit_target(chunk: "core.ChunkBase", stage: "Usd.Stage", layer: "Sdf.Layer | None") -> None:
    """Record on chunk's prim the layer it currently targets (the _EDIT_TARGET_MARKER attr
    read by nearest_upstream_edit_layer), then reconcile so everything downstream re-homes
    to it. The single entry point the Edit Target chunk's execute() calls — keeps the marker
    format and the downstream migration owned by core, not the script.

    The marker is updated in place at its existing home (get_attr_home_layer) so re-pointing
    the Edit Target repeatedly never leaves a stale stronger opinion shadowing the new one —
    same discipline as set_chunk_pinned.
    """
    if not (stage and chunk):
        return
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not prim.IsValid():
        return
    ident = layer.identifier if layer else ""
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        attr = prim.GetAttribute(_EDIT_TARGET_MARKER)
        old_ident = attr.Get() if attr and attr.IsValid() else None
        home = chunk_utils.get_attr_home_layer(attr) if attr and attr.IsValid() else None
        target = home or chunk_utils.get_bookkeeping_layer(stage, chunk)
        with Usd.EditContext(stage, target):
            prim.CreateAttribute(_EDIT_TARGET_MARKER, Sdf.ValueTypeNames.String, custom=True).Set(ident)
    finally:
        chunk_notice._usd_write_active = prev
    # The marker this whole memo is a search for just moved, and the write above ran with
    # notices suppressed, so nothing else will drop it.
    invalidate_edit_layer_memo()
    # Only re-home downstream when the target actually changed. The Edit Target chunk
    # re-executes on every stage content change (its refresh callback), so reconciling
    # unconditionally here would run — and thrash — on every unrelated edit.
    if old_ident != ident:
        reconcile_edit_targets(stage, _active_scene())


def get_exec_upstream_chunks(
    view_chunk: "core.ChunkBase",
    stage: "Usd.Stage",
    scene: "core.SceneSender",
) -> "list[core.ChunkBase]":
    """Return the chunks feeding view_chunk, following ALL input connections backward.

    This is the *cook set* for a view: view-driven execution runs only these chunks.
    Follows both the exec wire (inputs:chunk) AND data connections (any inputs:* → a
    source prim), so a node feeding another purely via data (e.g. a Transform between a
    Scatter and an RBD solver that reads its points) is still cooked — otherwise it would
    be blocked and its downstream would read empty/stale input. Ordered roots-first (a
    node's sources appear before it). The view chunk itself is excluded.
    """
    prim_to_chunk = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
    view_path = get_chunk_prim_path(view_chunk)

    def sources_of(path: str) -> "list[str]":
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim.IsValid():
            return []
        srcs: list[str] = []
        for attr in prim.GetAttributes():
            if not attr.GetName().startswith(_INPUTS_PREFIX):
                continue
            for conn in attr.GetConnections():
                src = str(conn.GetPrimPath())
                if src in prim_to_chunk:
                    srcs.append(src)
        if not is_merge_prim(prim):
            return srcs
        # A merge's slot order is what decides which branch wins, and visit order is what
        # produces that ranking: visit() appends a node only after its sources, so whichever
        # branch is visited FIRST lands earliest in cook_list — and recook_for_view ranks
        # later-in-cook_list as STRONGER. Visiting the slots in descending order therefore
        # leaves slot 0 visited last, ranked highest, and strongest, which is the documented
        # "first input wins". iter_exec_inputs is strongest-first, hence the reverse.
        #
        # Branches are disjoint, so each stays contiguous in cook_list whatever its length.
        # Where two slots share an upstream node, visit()'s `seen` keeps it at its FIRST
        # visit — i.e. with the weakest of the branches that reach it. Well-defined, and the
        # only stable choice: a shared node cannot sit at two strengths at once.
        exec_srcs = [
            src
            for attr in reversed(iter_exec_inputs(prim))
            for conn in attr.GetConnections()
            if (src := str(conn.GetPrimPath())) in prim_to_chunk
        ]
        exec_set = set(exec_srcs)
        return [s for s in srcs if s not in exec_set] + exec_srcs

    order: "list[core.ChunkBase]" = []
    seen: set = set()

    def visit(path: str) -> None:
        if path in seen:
            return
        seen.add(path)
        for src in sources_of(path):
            visit(src)
        if path != view_path and (c := prim_to_chunk.get(path)) is not None:
            order.append(c)

    visit(view_path)
    return order


def recook_for_view(
    stage: "Usd.Stage",
    scene: "core.SceneSender",
    context: "core.ChunkBase | None",
    view_chunk: "core.ChunkBase | None",
    _synthetic_tail: bool = False,
) -> None:
    """Apply view-driven execution for one context.

    With a view_chunk: cook only its upstream tree (roots-first); every other non-view
    chunk in the context is blocked + muted (dormant, hidden). With view_chunk=None (no active
    view): block + mute everything — nothing cooks until a view is activated.

    Blocking a chunk both hides its result (its layer is muted) and stops it reacting to
    param changes (see ChunkBase.value_changed) — so being outside the active view's tree
    means "does not execute", live.

    _synthetic_tail: True when view_chunk is a stand-in exec tail rather than a real view
    chunk (see the is_group_chunk branch below) — real view chunks are markers excluded
    from their own cook_list by get_exec_upstream_chunks, but a synthetic tail is real
    content that must cook too, so it's appended back in rather than excluded.
    """
    lm = chunk_registry.ChunkLayerManager()
    disabled = ChunkFlags.State.DISABLED

    def in_context(c: "core.ChunkBase") -> bool:
        # Node membership (prim-path based) so a group is managed by its PARENT graph's recook
        # and the group's inner chunks by the group's own recook — not conflated by get_context().
        return context is None or is_node_in_context(c, context)

    def mute(layer, muted: bool) -> None:
        # Use stage.Mute/UnmuteLayer, NOT layer.SetMuted — SetMuted recomposes lazily and
        # clears geometry / expires prims (the "chunk layers disappear" bug). stage muting is
        # the pattern the disable path already uses.
        if layer is None:
            return
        if muted:
            stage.MuteLayer(layer.identifier)
        else:
            stage.UnmuteLayer(layer.identifier)
        # Muting drops the layer out of GetLayerStack, so an Edit Target marker naming it
        # stops resolving. _on_contents_changed also catches this today, but only because it
        # invalidates on everything: USD reports a mute with no changed paths at all, so if
        # that handler is ever narrowed to the changes it can actually identify, this is the
        # only place the mute is still visible from.
        invalidate_edit_layer_memo()

    if view_chunk is None:
        cook_list: "list[core.ChunkBase]" = []
        cook_set: set = set()
    else:
        cook_list = get_exec_upstream_chunks(view_chunk, stage, scene)
        if _synthetic_tail:
            cook_list.append(view_chunk)
        cook_set = set(cook_list)

    # Block/mute leaf nodes by cook membership (blocking non-cook chunks first means the
    # ObjectsChanged from muting can't re-execute them). Groups are NEVER blocked as a node — a
    # group is its own cook domain, so its parent-visibility block must not propagate to its
    # children via the owner chain (that left the inner view unable to cook unless the group was
    # wired to a parent view). Instead recurse: a cooked group cooks its inner active view's tree;
    # a hidden group mutes its whole inner tree. Prim-path nesting prevents cycles.
    #
    # Cook-set members are unblocked here but stay MUTED for now — revealed one at a time in the
    # execute loop below, immediately before each one's own turn. Unmuting the whole upstream
    # tree upfront (the previous behaviour) meant a chunk that reads composed state during its
    # own execute() (e.g. Scatter reading a mesh's transform) could see a downstream chunk's
    # stale content that hadn't re-executed yet in this pass — the final "closer to view wins"
    # layer-strength ranking below is a real, correct rule for the settled result once everyone
    # has run, but it has nothing to do with what should be visible mid-pass while a chunk is
    # still reading its own upstream.
    mute_idents: list = []
    seen_idents: set = set()
    # scene.get_chunks() is a DFS via stack.pop() (LIFO) over insertion order -- roughly
    # reverse creation order, with no relation to exec/view proximity. That's harmless for
    # blocking/muting a chunk on its own, but a group recurses into recook_for_view here,
    # which runs that group's OWN cook_list (including any Create Layer-style chunk that
    # unconditionally reinserts itself at the front of a SHARED parent layer's subLayerPaths
    # on every execute -- last to run wins strongest). Reversed order there meant the group
    # FURTHEST from the view executed last and won, exactly backwards from "closer to view
    # wins". Visit cook_list's own members in ITS order (roots-first, so the group closest
    # to the view is recursed into last and so executes last) -- anything outside the cook
    # set isn't competing for layer strength at all, so its relative order doesn't matter.
    cook_rank = {id(c): i for i, c in enumerate(cook_list)}
    for c in sorted(scene.get_chunks(), key=lambda c: cook_rank.get(id(c), -1)):
        if is_view_chunk(c) or not in_context(c):
            continue
        if is_group_chunk(c):
            if c in cook_set:
                inner_view = find_group_active_view(c, stage, scene)
                synthetic = False
                if inner_view is not None and not get_exec_upstream_chunks(inner_view, stage, scene):
                    # A view wired to NOTHING is not an answer to "what does this group
                    # output" -- it is a default view that never got pointed at anything,
                    # or one left dangling when the chunk it watched was deleted (an exec
                    # heal clears the connection when the deleted node had no predecessor
                    # to inherit). Taken literally it means the group cooks nothing at all:
                    # its contents go blocked and muted, it stops feeding everything
                    # downstream of it, and there is no sign of why. Live-reported as a
                    # chunkgroup's Duplicate Prim being invisible from the group wired after
                    # it, with the copy simply absent from the stage.
                    #
                    # Fall through to the exec-tail fallback below, which is the same
                    # question ("what is the end of this group's chain") asked of a group
                    # whose view is missing rather than empty.
                    inner_view = None
                if inner_view is None:
                    # No real inner view chunk -- the common case for a group whose
                    # contents were never explicitly given one (view chunks are
                    # deliberately excluded from group copy/paste, see
                    # _iter_group_children). Without this fallback, a reachable group's
                    # contents never actually cook via view-driven execution at all --
                    # they only ever ran as an accidental side effect of the user diving
                    # into the group, which auto-wires a default view as a UI action
                    # (ChunkGraphView._ensure_default_view). That made execution order
                    # (and therefore which sibling's self-managed layer, e.g. Create
                    # Layer's, ends up strongest) depend on dive/rename order instead of
                    # actual graph position. Cook through the group's own exec tail
                    # instead, exactly as if a real inner view were pointed at it.
                    tails = get_exec_tail_chunks(stage, scene, c)
                    inner_view = tails[0] if tails else None
                    synthetic = inner_view is not None
                recook_for_view(stage, scene, c, inner_view, _synthetic_tail=synthetic)
            else:
                recook_for_view(stage, scene, c, None)
            continue
        was_blocked = c.is_blocked()
        c.set_blocked(c not in cook_set)
        if was_blocked and not c.is_blocked():
            # A blocked chunk's param value-changed notices are suppressed (see this
            # function's own docstring: blocking "stops it reacting to param changes"),
            # which was meant to stop it from re-executing while hidden -- but the same
            # suppression also silently drops _sync_param_input, the one-time step that
            # first authors a param's value into a USD attr at all. A freshly created,
            # not-yet-wired chunk is blocked (nothing reachable from any view yet), so a
            # value typed into it before wiring never got an attr; nothing later ever
            # retried, since _sync_param_input only runs reactively off that same
            # suppressed notice. Catch up here, the one place a chunk's blocked state
            # actually flips off — _sync_param_input is already idempotent (skips a
            # param already wired, or still at its default), so this is a no-op for
            # every param that didn't hit the gap.
            sync = ChunkStageSync()
            for _, param in _iter_chunk_params(c):
                sync._sync_param_input(c, param)
            for _, vector in _iter_chunk_vectors(c):
                sync._sync_vector_input(c, vector)
        layer = lm.get_layer(c)
        if layer is not None and layer.identifier not in seen_idents:
            seen_idents.add(layer.identifier)
            mute_idents.append(layer.identifier)

    # One recomposition for the whole "hide every non-view leaf" pass — each stage.MuteLayer
    # recomposes the stage, so muting N leaves one at a time was N recompositions on every view
    # switch. All set_blocked() calls above run first, so the block-before-mute ordering the
    # comment relies on holds even more strictly (every block precedes the single mute).
    if mute_idents:
        stage.MuteAndUnmuteLayers(mute_idents, [])

    for c in cook_list:
        if not c.has_state(disabled) and not is_group_chunk(c):
            mute(lm.get_layer(c), False)
            c.execute()

    if not cook_list:
        return

    # Layer-win order follows the exec traversal: the chunk nearest the view (last in
    # cook_list) must win, so reorder each graph layer's sublayers to put the most
    # downstream layer strongest (subLayerPaths[0]). This is what makes rewiring change
    # who-wins for chunks that author to the same target. (Safe re: prim expiry — this
    # reorders the result layers under the graph root, not the chunk prims in the edit layer.)
    rank = {id(c): i for i, c in enumerate(cook_list)}  # higher = more downstream = stronger
    # Tuple ranks: (cook_list position, 0-for-the-chunk-itself-or-1-for-a-descendant).
    # The second element only matters for ties on the first -- see the descendant loop
    # below for why those ties exist and must break in the descendant's favor.
    ident_rank: dict = {}
    for c in cook_list:
        layer = lm.get_layer(c)
        if layer is not None:
            ident_rank[layer.identifier] = (rank[id(c)], 0)
    # A ranked group's own children are ranked by a completely separate, incomparable
    # numbering scheme -- their own inner recook's cook_list -- so from THIS call's
    # perspective they're simply invisible descendants, not competitors for a tier of
    # their own. Left alone they just sit wherever they were first inserted instead of
    # ranking anywhere near the group that actually produced them (live-reported: AMD
    # MaterialX Library correctly outranked its unrelated Transform/Sublayer siblings
    # once wired into the view, but its own Reference/BindMaterial children stayed
    # stuck at the bottom). Give every descendant of a ranked group the group's own
    # rank tier, strictly above the group's own layer within that tier (a group's
    # actual produced content should win over its own generic bookkeeping, and a plain
    # equal rank would leave that ambiguous -- tie-broken arbitrarily by whatever the
    # two happened to be in before this sort, not something to depend on). The stable
    # sort in _reorder_graph_layers_by_rank then keeps whatever relative order the
    # group's own inner recook already established among the descendants themselves
    # (their own ranks tie 1-vs-1, so stay untouched relative to each other), while
    # moving the whole cluster to sit where the group belongs among its outer siblings.
    for c in cook_list:
        if not is_group_chunk(c):
            continue
        prefix = get_chunk_prim_path(c) + "/"
        for other in scene.get_chunks():
            if id(other) in rank:
                continue
            if get_chunk_prim_path(other).startswith(prefix):
                layer = lm.get_layer(other)
                if layer is not None:
                    ident_rank.setdefault(layer.identifier, (rank[id(c)], 1))
    _reorder_graph_layers_by_rank(lm.graph_layers.values(), ident_rank)


def _reorder_graph_layers_by_rank(graph_layers: "Iterable[Sdf.Layer]", ident_rank: dict) -> None:
    """Reposition each graph layer's sublayers so higher-ranked idents (per ident_rank)
    come first (subLayerPaths[0] = strongest).

    A chunkgroup's own children share their parent's physical graph layer -- "chunkgroups
    are subnets, not spatial containers" -- so the SAME graph layer gets this treatment
    once per context, recursively: a group's own inner recook runs first and ranks its
    children by proximity to ITS view, then this same step runs again for the OUTER
    context, whose ident_rank knows nothing about those children at all. A global sort
    keyed on ident_rank.get(ident, -1) shoves every ident a given call doesn't recognize
    to the single weakest position, silently destroying a more specific call's ordering
    underneath it (live-reported: a chunkgroup's own content ranked weaker than its
    unrelated siblings in the same tab).

    Only reposition idents this call actually has a rank for, among the slots they
    collectively occupy -- everything else (including a nested group's own children)
    stays exactly where a more specific call already put it.
    """
    for g_layer in graph_layers:
        current = list(g_layer.subLayerPaths)
        slots = [i for i, ident in enumerate(current) if ident in ident_rank]
        if not slots:
            continue
        ranked = sorted((current[i] for i in slots), key=lambda ident: ident_rank[ident], reverse=True)
        ordered = list(current)
        for slot, ident in zip(slots, ranked):
            ordered[slot] = ident
        if ordered != current:
            g_layer.subLayerPaths = ordered  # type: ignore


def recook_focus(
    stage: "Usd.Stage",
    scene: "core.SceneSender",
    context: "core.ChunkBase | None",
    view_chunk: "core.ChunkBase | None",
) -> None:
    """Establish the GLOBAL stage for the tab/graph the user is looking at.

    The stage is shared by every context, but only the focused one should drive it. Root
    (context is None) is already a full global establish — recook_for_view(None) touches every
    chunk and recurses into all groups. But a GROUP focus only manages its own children, so the
    parent tab's active view (and sibling groups) would stay unmuted and bleed through ("switch
    tabs, still see the previous view"). So when focusing a group, first mute + block everything
    OUTSIDE the group's subtree, then recook inside it — leaving only the group's inner active
    view driving the stage.
    """
    if context is not None:
        lm = chunk_registry.ChunkLayerManager()
        group_path = get_chunk_prim_path(context)
        inside_prefix = group_path + "/"
        # A focused group's own UPSTREAM must stay composed. The group's inner chunks
        # author on top of that content -- BindMaterial inside AMD MaterialX Library
        # binds a material onto a mesh that a Sublayer several nodes upstream is what
        # brings onto the stage at all. Muting it (as this loop otherwise does for
        # everything outside the group) means those inner chunks execute against a stage
        # where their target prims simply don't exist, so they author nothing -- and
        # since a chunk's execute() Clear()s its own layer first, whatever it authored
        # last time is destroyed in the process. Live-reported: diving into the AMD node
        # dropped the applied material, and diving back out never brought it back
        # (nothing re-executes BindMaterial once its params haven't changed).
        #
        # Muting the group's upstream was never the point of this pass anyway -- it
        # exists to stop the parent tab's active view and UNRELATED sibling branches
        # bleeding through, and the group's own upstream is neither.
        # Every ANCESTOR's upstream, not just the focused group's. A group nested inside
        # another is fed by its parent's chain: focusing it, the parent's own upstream --
        # the Sublayer bringing the scene onto the stage two levels up -- fell outside this
        # set and was muted, so the inner chunks cooked against a stage their targets were
        # not on and the viewport went empty. Reported as content coming and going
        # depending on which level you were looking at; it reproduces with the nesting
        # built by hand, so it is not about how the groups were made.
        upstream_keep: set = set()
        by_path = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
        node: "core.ChunkBase | None" = context
        seen_ancestors: set = set()
        while node is not None:
            node_path = get_chunk_prim_path(node)
            if node_path in seen_ancestors:
                break  # a cycle would hang the focus; prim nesting should prevent it
            seen_ancestors.add(node_path)
            upstream_keep.update(
                get_chunk_prim_path(u) for u in get_exec_upstream_chunks(node, stage, scene)
            )
            parent_path = str(Sdf.Path(node_path).GetParentPath())
            node = by_path.get(parent_path)
        # An upstream GROUP authors nothing of its own -- all of its content lives in its
        # children's layers -- so keeping only the group's own (empty) layer keeps nothing at
        # all. Live-reported: wire chunkgroup A -> chunkgroup B, dive into B, and A's prims
        # are gone from the stage, so B's inner chunks cook against a stage their input isn't
        # on. It looked like a group could only reach another group's stage through a data
        # wire, because lofting/connecting the group's Description authors a connection from
        # the group prim DOWN to an inner chunk -- which is exactly what put those inner
        # chunks into get_exec_upstream_chunks' node-level answer in the first place.
        keep_prefixes = tuple(u + "/" for u in upstream_keep)
        for c in scene.get_chunks():
            if is_view_chunk(c):
                continue
            cp = get_chunk_prim_path(c)
            # Keep the focused group itself and its descendants — recook_for_view handles those.
            if cp == group_path or cp.startswith(inside_prefix):
                continue
            if cp in upstream_keep or (keep_prefixes and cp.startswith(keep_prefixes)):
                # Leave an upstream group's internals exactly as the parent context's cook
                # left them: whichever of its children that cook muted (outside the group's
                # own active view) stay muted, and the ones driving its output stay composed.
                continue
            # NEVER block a context/container (a tab or group). is_blocked() climbs the owner
            # chain, so blocking the focused group's ANCESTOR would propagate down and block the
            # focused inner tree itself — nothing would cook. Container chunks author nothing of
            # their own; their leaf children are muted individually by this same loop.
            if c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                continue
            c.set_blocked(True)
            layer = lm.get_layer(c)
            if layer is not None:
                stage.MuteLayer(layer.identifier)
    recook_for_view(stage, scene, context, view_chunk)


def get_exec_tail_chunks(
    stage: "Usd.Stage",
    scene: "core.SceneSender",
    context: "core.ChunkBase | None" = None,
) -> "list[core.ChunkBase]":
    """Return non-view chunks whose outputs:chunk is not consumed by any inputs:chunk.

    These are the downstream ends of the exec chains. Wiring a view to a tail makes the
    view cook that tail's whole upstream chain. Used to auto-wire a default view.
    """
    candidates = [
        c for c in scene.get_chunks()
        if not is_view_chunk(c) and is_node_in_context(c, context)
    ]
    consumed: set = set()
    for prim in stage.Traverse():
        # Every slot — a chunk feeding a merge's slot 2 is just as consumed as one feeding
        # slot 0, and calling it a tail would wire a view to the middle of a live branch.
        for conn in exec_input_connections(prim):
            consumed.add(str(conn.GetPrimPath()))
    return [c for c in candidates if get_chunk_prim_path(c) not in consumed]


def get_exec_head_chunks(
    stage: "Usd.Stage",
    scene: "core.SceneSender",
    context: "core.ChunkBase | None" = None,
) -> "list[core.ChunkBase]":
    """Return non-view chunks with no non-view exec (inputs:chunk) input.

    These are the roots of the exec chain — re-executing them propagates
    through the full chain.  Pass context to restrict to one tab context.
    """
    candidates = [
        c for c in scene.get_chunks()
        if not is_view_chunk(c) and is_node_in_context(c, context)
    ]
    candidate_paths = {get_chunk_prim_path(c) for c in candidates}
    has_non_view_input: set = set()
    for c in candidates:
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
        if not prim.IsValid():
            continue
        # Every slot — a merge fed only through slot 2 still has a real input, and calling
        # it a root would make it the start of a chain it is actually the end of.
        for conn in exec_input_connections(prim):
            if str(conn.GetPrimPath()) in candidate_paths:
                has_non_view_input.add(get_chunk_prim_path(c))
    return [c for c in candidates if get_chunk_prim_path(c) not in has_non_view_input]


def _connected_source_paths(prim: "Usd.Prim") -> "set[str]":
    """Prim paths this chunk prim reads from: every exec slot plus every typed input.

    Both kinds of wire count as a dependency for ordering. A param connection is authored
    on the *destination* attr pointing at the source (see ChunkStageSync._expr_connections),
    so reading this prim's own inputs: connections gives the chunks it depends on.
    """
    my_path = prim.GetPath()
    sources = {str(conn.GetPrimPath()) for conn in exec_input_connections(prim)}
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith(_INPUTS_PREFIX) or is_exec_input_name(name):
            continue
        for conn in attr.GetConnections():
            target = Sdf.Path(str(conn)).GetPrimPath()
            if target == my_path:
                continue
            if my_path.HasPrefix(target):
                # An ancestor target is a loft (this param promoted to its group's
                # interface), not a sibling dependency — the group contains this chunk, so
                # treating it as an input would invert the real order and can cycle.
                continue
            sources.add(str(target))
    return sources


def commit_order_from_stage(
    chunks: "list[core.ChunkBase]", stage: "Usd.Stage | None"
) -> "list[core.ChunkBase]":
    """Order chunks for commit by what the stage says they read, not by DAG depth.

    chunk_scheduler.commit_order sorts on DagNode depth, which only sees edges someone
    created with add_dependency. That covers exec wiring (load_chunks mirrors inputs:chunk
    into add_dependency) but NOT a param port: drag-to-connect authors an @Node.param@
    expression, whose live link is a NoticeConnection rather than a DAG edge, so two
    chunks joined only by a param wire both sit at depth 0 and commit in arbitrary order.

    The stage has both kinds of wire, and is already the authority the rest of the wiring
    code trusts (see _live_exec_dependencies), so order from it instead.

    A cycle can't be topologically ordered and must not drop anyone: the first chunk still
    waiting is emitted and the sort continues, so the result always contains every input
    chunk exactly once.

    Args:
        chunks (list[core.ChunkBase]): The chunks to order
        stage (Usd.Stage | None): The stage holding their chunk prims; None falls back to
            the depth-based ordering

    Returns:
        list[core.ChunkBase]: The chunks, every chunk preceded by everything it reads from
    """
    ordered = list(chunks)
    if stage is None:
        return chunk_scheduler.commit_order(ordered)

    by_path = {get_chunk_prim_path(c): c for c in ordered}
    deps: "dict[int, set[int]]" = {}
    for c in ordered:
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
        if not (prim and prim.IsValid()):
            deps[id(c)] = set()
            continue
        # Only chunks in this batch can be waited on — a connection to something outside it
        # would otherwise never be satisfied and stall every dependant into the cycle path.
        deps[id(c)] = {
            id(src) for path in _connected_source_paths(prim)
            if (src := by_path.get(path)) is not None and src is not c
        }

    result: "list[core.ChunkBase]" = []
    done: "set[int]" = set()
    waiting = ordered
    while waiting:
        ready = [c for c in waiting if deps[id(c)] <= done]
        if not ready:
            ready = [waiting[0]]  # cycle — break it at the first chunk still waiting
        result.extend(ready)
        done.update(id(c) for c in ready)
        ready_ids = {id(c) for c in ready}
        waiting = [c for c in waiting if id(c) not in ready_ids]
    return result


_chunk_clipboard: list[str] = []


def copy_selected_chunks() -> None:
    """Store prim paths of selected non-view chunks for later paste."""
    global _chunk_clipboard
    registry = chunk_registry.ChunkRegistry()
    if not (scene := registry.scene):
        return
    _chunk_clipboard = [
        get_chunk_prim_path(c)
        for c in scene.get_selected_chunks()
        if not is_view_chunk(c)
    ]


def paste_chunks() -> None:
    """Create duplicates of clipboard chunks, copying their input attribute values."""
    if not _chunk_clipboard:
        return
    registry = chunk_registry.ChunkRegistry()
    scene = registry.scene
    if not scene:
        return
    stage = scene.get_stage()
    if not stage:
        return

    prim_to_chunk = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
    for prim_path in _chunk_clipboard:
        src_chunk = prim_to_chunk.get(prim_path)
        if src_chunk is None:
            continue
        cfg = src_chunk.get_config()
        if not cfg or not cfg.module:
            continue
        src_prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not src_prim.IsValid():
            continue
        new_chunks = _chunk_mod.create_chunks(cfg.module)
        if not new_chunks:
            continue
        new_chunk = new_chunks[0]
        new_prim_path = get_chunk_prim_path(new_chunk)
        new_prim = stage.GetPrimAtPath(Sdf.Path(new_prim_path))
        if not new_prim.IsValid():
            continue
        for attr in src_prim.GetAttributes():
            name = attr.GetName()
            if not name.startswith(_INPUTS_PREFIX) or name == _EXEC_INPUT:
                continue
            dst_attr = new_prim.GetAttribute(name)
            if not dst_attr.IsValid():
                continue
            # dst_attr was just authored moments ago by create_chunks() -> _define_prim,
            # via the chunk's own current Edit Target — update it there, not wherever
            # ambient happens to point.
            home = chunk_utils.get_attr_home_layer(dst_attr)
            ctx = Usd.EditContext(stage, home) if home else contextlib.nullcontext()
            with ctx:
                val = attr.Get()
                if val is not None:
                    dst_attr.Set(val)
                if attr.HasAuthoredConnections():
                    dst_attr.SetConnections(attr.GetConnections())
        _load_wire_inputs(new_prim, new_chunk)


def _iter_chunk_params(
    obj: "core.ChunkCommon", prefix: str = ""
) -> Generator[Tuple[str, "core.ChunkParamBase"], None, None]:
    for param in obj.get_my_params():
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkParamBase):
            yield path, param
        elif isinstance(param, (core.ChunkListGroupBase, core.ChunkVectorBase)):
            pass  # list groups handled by _iter_list_group_arrays; vectors by _iter_chunk_vectors
        else:
            yield from _iter_chunk_params(param, path)


def _iter_chunk_vectors(
    obj: "core.ChunkCommon", prefix: str = ""
) -> Generator[Tuple[str, "core.ChunkVectorBase"], None, None]:
    """Yield (path, vector) for every ChunkVectorBase reachable under obj."""
    for param in obj.get_my_params():
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkVectorBase):
            yield path, param
        elif not isinstance(param, (core.ChunkParamBase, core.ChunkListGroupBase)):
            yield from _iter_chunk_vectors(param, path)


def _iter_list_group_arrays(
    obj: "core.ChunkCommon", prefix: str = ""
) -> Generator[Tuple[str, "core.ChunkListGroupBase"], None, None]:
    """Yield (path, list_group) for every ChunkListGroupBase in the hierarchy."""
    for param in obj.get_my_params():
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkListGroupBase):
            yield path, param
        elif not isinstance(param, core.ChunkParamBase):
            yield from _iter_list_group_arrays(param, path)


def initialize(stage: Usd.Stage, scene: "core.SceneSender") -> None:
    ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, scene)
    scene.add_stage_replaced_listener(ChunkStageSync().reload_for_stage)


def _find_prim_layer(prim: Usd.Prim) -> "Sdf.Layer | None":
    """Return the non-root, non-anonymous layer that authors this prim."""
    root_id = prim.GetStage().GetRootLayer().identifier
    for spec in prim.GetPrimStack():
        layer = spec.layer
        if not layer.anonymous and layer.identifier != root_id:
            return layer
    return None


def _iter_scoped_prims(
    stage: Usd.Stage, resynced: "set[Sdf.Path]", info_changed: "set[Sdf.Path]"
):
    """The prims a change round could have made loadable, once each.

    Each resynced path contributes its whole subtree, since a structural change can bring
    in prims anywhere below it — a chunk arriving in a sublayer resyncs the *parent* of the
    new prim, not the prim. Each info-changed path contributes only the prim it names.

    Paths may be property paths, and may name a prim that has since gone (a resync reports
    a deletion the same way it reports an arrival); both resolve to the owning prim or to
    nothing. Overlapping subtrees are normal, so prims are de-duplicated.
    """
    seen: "set[str]" = set()

    def _emit(prim):
        key = str(prim.GetPath())
        if key in seen:
            return False
        seen.add(key)
        return True

    for path in resynced:
        if path == Sdf.Path.absoluteRootPath:
            for prim in stage.Traverse():
                if _emit(prim):
                    yield prim
            continue
        prim = stage.GetPrimAtPath(path.GetPrimPath())
        if not (prim and prim.IsValid()):
            continue
        for descendant in Usd.PrimRange(prim):
            if _emit(descendant):
                yield descendant

    for path in info_changed:
        prim = stage.GetPrimAtPath(path.GetPrimPath())
        if prim and prim.IsValid() and _emit(prim):
            yield prim


def _restore_disabled_state(prim: "Usd.Prim", chunk: "core.ChunkBase") -> None:
    """Carry a prim's authored deactivation back onto the chunk it belongs to.

    Disabling a chunk authors active=false on its prim, so the file records the state
    correctly -- but a freshly created chunk starts enabled, and nothing on the load path
    reads it back. The chunk returned switched ON, quietly re-running work the scene was
    saved with switched off.

    Authored, not composed: a chunk inside a disabled group composes inactive without
    anything being authored on it, and it is not itself disabled.
    """
    if prim.HasAuthoredActive() and prim.GetMetadata("active") is False:
        chunk.set_state(ChunkFlags.State.DISABLED)


def load_chunks(
    stage: Usd.Stage,
    scene: "core.SceneSender",
    loaded_prim_paths: set[str] | None = None,
    prims: "Generator | None" = None,
) -> None:
    """Turn Chunk prims into chunks.

    prims narrows which prims are considered — see ChunkStageSync._scan_for_new_chunks,
    which derives it from what USD reported changed. None considers the whole stage, which
    is what a file being opened wants.
    """
    if not stage:
        return

    to_load = []
    # Top-level (direct child of the pseudo-root) Chunk-typed prims with no chunkType —
    # a structural context prim (a Tab/ChunkGroup saved via export_chunks), not a
    # module-backed chunk. Nested ChunkGroups use a separate parent-context mechanism
    # (_parent_context) not covered by this reload path yet.
    context_prims = []
    for prim in (stage.Traverse(_LOAD_INCLUDE_INACTIVE) if prims is None else prims):
        # The /chunks scope is structural, not a chunk: it holds the graph but is not one.
        # It used to be typed Chunk and so came through this filter as a "context prim";
        # typing it Scope made the loader skip it, and with the saved container
        # unrecognised every chunk fell through to the root-layer branch and was rehomed
        # under a fresh tab named after the file. Prim paths moved on every reload, so
        # anything addressing a path -- a binding, a sublayer target -- stopped resolving.
        is_chunks_scope = (
            str(prim.GetPath()) == _CHUNKS_ROOT
            and prim.GetTypeName() == _CHUNKS_ROOT_TYPENAME
        )
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME and not is_chunks_scope:
            continue
        if loaded_prim_paths is not None and str(prim.GetPath()) in loaded_prim_paths:
            continue
        chunk_type = prim.GetAttribute(_CHUNK_TYPE_ATTR).Get()
        if not chunk_type:
            if prim.GetParent().IsPseudoRoot():
                context_prims.append(prim)
            continue
        mod = chunk_registry.ChunkRegistry().get_chunk_module(str(chunk_type))
        if not mod:
            logger.warning("load_chunks: no module for type %r", chunk_type)
            continue
        to_load.append((prim, mod))

    if not to_load:
        return

    # Pre-register all prim paths before processing to prevent StageContentsChanged
    # callbacks (from add_layer subLayerPaths inserts) from triggering recursive loads
    # that find unregistered paths and create duplicate contexts.
    if loaded_prim_paths is not None:
        for prim, _ in to_load:
            loaded_prim_paths.add(str(prim.GetPath()))

    # Suppress premature reactive cascades for the whole load, not just each
    # _add_prim_mods call below: recreating a context (scene.add_context, right below)
    # already triggers CHUNK_LOADED -> get_edit_context() -> get_or_create_layer(),
    # whose subLayerPaths.insert() fires a global STAGE_CONTENTS_CHANGED that an Edit
    # Target chunk's globally-listening refresh handler reacts to -- reactively
    # re-executing everything downstream of it before a single chunk has even been
    # restored yet. _add_prim_mods's own old_loading_stage/finally below still runs,
    # now just re-affirming this same value and restoring back to it -- harmless.
    old_loading_stage = chunk_notice._loading_stage
    chunk_notice._loading_stage = stage
    try:
        # Recreate each existing named top-level context under its own saved name/path —
        # before grouping below, so its children are recognized as already having a home
        # instead of falling through as "no source layer" orphans wrapped in a brand-new
        # tab named after whatever file happens to be open right now.
        group_chunk_by_path: dict[str, "core.ChunkBase"] = {}
        context_paths: set[str] = set()
        for prim in context_prims:
            prim_path = str(prim.GetPath())
            if loaded_prim_paths is not None:
                loaded_prim_paths.add(prim_path)
            if prim_path == _CHUNKS_ROOT:
                # A scope holds chunks without being a context itself ("/chunks det är
                # bara ett scope"). Recording the path is still what routes its children
                # to _EXISTING_CONTEXT, so they keep their saved home instead of being
                # wrapped in a tab named after the file -- we just do not mint a context
                # for the scope to be that home.
                context_paths.add(prim_path)
                continue
            ctx = scene.add_context(prim.GetName(), prim_path)
            if ctx is not None:
                group_chunk_by_path[prim_path] = ctx
                context_paths.add(prim_path)

        def _has_recreated_context_ancestor(prim) -> bool:
            p = prim.GetParent()
            while p and not p.IsPseudoRoot():
                if str(p.GetPath()) in context_paths:
                    return True
                p = p.GetParent()
            return False

        # Group prims by their source layer so each sublayer file gets its own context —
        # except ones already homed under a context just recreated above.
        _EXISTING_CONTEXT = object()
        groups = {}
        for prim, mod in to_load:
            src = None
            if context_paths and _has_recreated_context_ancestor(prim):
                key = _EXISTING_CONTEXT
            else:
                src = _find_prim_layer(prim)
                key = src.identifier if src else None
            if key not in groups:
                groups[key] = (src, [])
            groups[key][1].append((prim, mod))

        def _add_prim_mods(prim_mods):
            old_loading_stage = chunk_notice._loading_stage
            chunk_notice._loading_stage = stage
            try:
                deferred = []  # (prim, chunk)
                top_level_chunk_ids: set[int] = set()
                for prim, mod in prim_mods:
                    # Walk up the prim's parent chain to find the nearest loaded ChunkGroup.
                    ancestor_group = None
                    p = prim.GetParent()
                    while p and not p.IsPseudoRoot():
                        g = group_chunk_by_path.get(str(p.GetPath()))
                        if g is not None:
                            ancestor_group = g
                            break
                        p = p.GetParent()

                    chunks = mod.create() if mod.loaded else mod.load()
                    for c in chunks:
                        # Restore the saved chunk name from its prim (prims are authored at
                        # <root>/<chunk.name>, so this is a lossless round-trip). Freshly
                        # created chunks otherwise get default names uniquified in
                        # traversal order, which need not match the prims — two chunks of
                        # the same type could swap identities, scrambling the name-keyed
                        # wiring below into a cyclic DAG from a perfectly linear file.
                        c.name = prim.GetName()
                        if c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                            # Restore saved param values before scene.add_chunk, same as the
                            # other two branches below -- a CONTEXT chunk is usually pure
                            # structural scaffolding (a Tab/ChunkGroup) with nothing of its
                            # own worth restoring, but one that ALSO has a real backing module
                            # (e.g. AMD MaterialX Library, which uses CONTEXT purely for its
                            # collapsible/embedded-widget node styling) can carry a full set of
                            # its own inputs: attrs, and those were silently dropped here --
                            # every param reverted to its default on reload, live-reported as
                            # "settings don't get saved" even though the file correctly held
                            # them (confirmed: inputs:picked_title etc. were present in the
                            # saved layer the whole time; only the read-back on load was
                            # missing).
                            _load_wire_inputs(prim, c)
                            _restore_disabled_state(prim, c)
                            c.prim_path = str(prim.GetPath())
                            object.__setattr__(c, '_exec_prim_path', str(prim.GetPath()))
                            if ancestor_group is not None:
                                # A chunkgroup inside another chunkgroup. Every CONTEXT chunk
                                # used to go to scene.add_chunk regardless, which is the scene
                                # ROOT -- so a nested group came back as a SIBLING of the group
                                # it was saved in, lost the name collision with it, and was
                                # renamed, taking its contents to a different address and
                                # leaving its parent empty.
                                #
                                # Cook order went with it: a group's inner chunks cook in
                                # prim-path depth order, so an upstream Sublayer runs before a
                                # BindMaterial nested deeper that reads what it brings onto the
                                # stage. Flattened to siblings at one depth, the sort had
                                # nothing left to order by and a reopened scene kept its
                                # original material.
                                #
                                # The save side was never wrong -- the file holds the nesting.
                                #
                                # _parent_context is what _on_param_added derives a group's
                                # prim root from; without it the group falls back to the
                                # /chunks scope, computes the same path as the default group
                                # and loses the collision, which is the rename itself.
                                # _parent_context, then the SCENE container -- which is
                                # where a nested group lives when one is made live, so a
                                # reload rebuilds the same shape rather than a different one.
                                #
                                # Not ancestor_group.add_param: that applies the param naming
                                # rules, and get_reserved_names() includes the group's OWN
                                # name, so a group named the same as its parent came back
                                # renamed with a "1" suffix. USD is perfectly happy with
                                # /chunks/ChunkGroup/ChunkGroup, the file says that is where
                                # it lives, and anything addressing it by name breaks when we
                                # decide otherwise on load.
                                # Enter the ancestor for the duration of the add, which is
                                # exactly how a group nests when one is made live: add_chunk
                                # reads _current_context and overwrites _parent_context from
                                # it, so setting the latter directly is undone on the way in.
                                #
                                # Not ancestor_group.add_param either: that applies the param
                                # naming rules, and get_reserved_names() includes the group's
                                # OWN name, so a group named the same as its parent came back
                                # renamed with a "1" suffix. USD is happy with
                                # /chunks/ChunkGroup/ChunkGroup, the file says that is where it
                                # lives, and anything addressing it by name breaks if we decide
                                # otherwise on load.
                                prev_ctx = scene._current_context
                                scene._current_context = weakref.ref(ancestor_group)
                                try:
                                    scene.add_chunk(c)
                                finally:
                                    scene._current_context = prev_ctx
                            else:
                                scene.add_chunk(c)
                            if c.get_config() and c.get_config().module:
                                group_chunk_by_path[str(prim.GetPath())] = c
                        elif ancestor_group is not None:
                            # Sub-chunk of a ChunkGroup: restore saved values before
                            # add_param so PARAM_ADDED widgets see the correct values.
                            _load_wire_inputs(prim, c)
                            _restore_disabled_state(prim, c)
                            ancestor_group.add_param(c, notify=False)
                            object.__setattr__(c, '_exec_prim_path', str(prim.GetPath()))
                            deferred.append((prim, c))
                        else:
                            # Restore saved param values before scene.add_chunk so the
                            # PARAM_ADDED widget sees the correct values on creation.
                            _load_wire_inputs(prim, c)
                            _restore_disabled_state(prim, c)
                            if added := scene.add_chunk(c, notify=False):
                                object.__setattr__(added, '_exec_prim_path', str(prim.GetPath()))
                                deferred.append((prim, added))
                                top_level_chunk_ids.add(id(added))

                # Restore DAG dependencies from inputs:chunk connections so execution
                # order is driven by the explicit graph, not just prim appearance order.
                # Resolve connection targets by prim path — names are scene-wide and can
                # be uniquified/renamed, so a name lookup can bind to the wrong chunk.
                chunk_by_path = {str(p.GetPath()): c for p, c in deferred}
                chunks_with_dependencies: set[int] = set()
                for prim, chunk in deferred:
                    # Every slot, in slot order — a reloaded merge must regain ALL its branches,
                    # and regain them strongest-first so the restored DAG matches the saved file.
                    exec_conns = exec_input_connections(prim)
                    if not exec_conns:
                        continue
                    existing_deps = {id(d) for _, d in chunk.get_dependencies()}
                    for conn_path in exec_conns:
                        dep = chunk_by_path.get(str(conn_path.GetPrimPath()))
                        if dep is not None and id(dep) not in existing_deps:
                            chunk.add_dependency(dep)
                            chunks_with_dependencies.add(id(chunk))

                _restore_lofted_params(stage, deferred)

                # _load_wire_inputs (via _connect_param_attr) resolves @node.param@
                # expressions to set up the live-update listener (_expression_connections),
                # but the referenced chunk may not have been registered yet at that point
                # in this same loop — resolution fails and no listener gets attached, so
                # later edits to the referenced chunk never notify this param/widget even
                # though a fresh .value read is correct. Now that every chunk in this batch
                # is registered and every lofted value restored, retry once for anything
                # still empty. This runs after _restore_lofted_params so a lofted param
                # carrying an expression is covered by the same retry.
                #
                # This is the first of two passes. A param lofted inside a CHUNK_LOADED
                # callback does not exist yet here -- that notice fires much further down --
                # so an expression referring to one still resolves to nothing. The second
                # pass runs after those notices and carries the warning; nothing is reported
                # dangling until both have had a go.
                _resolve_pending_expressions(deferred, warn=False)

                # Wire the context (ChunkGroup) as DagNode owner of its terminal sub-chunks.
                # Terminal nodes have no outputs so DagNode.should_solve() returns False by
                # default, skipping them in the cascade. Setting the owner makes them solve.
                chunks_that_are_deps: set[int] = set()
                for _, c in deferred:
                    for _, dep in c.get_dependencies():
                        chunks_that_are_deps.add(id(dep))
                for _, c in deferred:
                    if id(c) not in chunks_that_are_deps:
                        context = c.get_context()
                        if context and isinstance(context, core.ChunkBase):
                            c.get_dag_node().set_owner(context)

                # Trigger the DAG solve for root nodes (no upstream dependencies).
                # The solve cascades to dependents in topological order via add_dependency.
                # Top-level chunks always execute. Sub-chunks inside ChunkGroups only
                # execute when ChunkLayerManager owns this specific stage — a stale
                # singleton from a different test/session must not add layers to the
                # wrong stage stack or call execute prematurely.
                lm = ChunkLayerManager()
                can_execute_sub_chunks = lm.initialized and lm.stage is stage

                # Ensure every chunk eligible to execute below already has its own layer
                # registered before the re-sort pass. Most chunks' layers get created as a
                # side effect of restoring their values in _load_wire_inputs (via
                # _connect_param_attr) -- but a chunk whose only restored value is a
                # list-group row (ProxyParam's own __setattr__, e.g. Sublayer's
                # imports:sublayer_path) never goes through that path, so without this its
                # layer wouldn't exist until the execute loop below -- by which point the
                # re-sort pass has already run and can never include it. The layer then
                # never gets spliced into the stage even though get_or_create_layer() is
                # called there too (live-reported as a saved scene's Sublayer coming back
                # empty on reload; only reliably worked before because a stray premature
                # notify from that same ProxyParam path happened to create/splice it in
                # ahead of time as a side effect -- see ProxyParam.__setattr__'s "value"
                # case). get_or_create_layer() is idempotent, so this is a no-op for every
                # chunk that already got one the normal way.
                if lm.initialized:
                    for _, c in deferred:
                        if id(c) in top_level_chunk_ids or can_execute_sub_chunks:
                            lm.get_or_create_layer(c)

                # Re-sort graph sublayers so downstream chunks (higher sort_key after
                # wiring) end up with stronger layers than their upstream dependencies.
                # Layers are created early (during _load_wire_inputs, and now unconditionally
                # just above) when sort_keys are all 0, so the initial insertion order needs
                # correction here.
                if lm.initialized:
                    layer_to_sk: dict[str, int] = {
                        lay.identifier: c.get_dag_node().get_sort_key()
                        for _, c in deferred
                        if (lay := lm.get_layer(c))
                    }
                    # Snapshot before iterating: reassigning subLayerPaths below can
                    # reactively cascade into chunk execute()s (e.g. an Edit Target chunk
                    # reacting to the resulting STAGE_CONTENTS_CHANGED), which can call
                    # get_or_create_layer() and add/remove graph_layers entries — mutating
                    # the dict this loop is reading, mid-iteration.
                    for g_layer in list(lm.graph_layers.values()):
                        sl = list(g_layer.subLayerPaths)
                        sl.sort(key=lambda ident: layer_to_sk.get(ident, 0), reverse=True)
                        g_layer.subLayerPaths = sl  # type: ignore

                # Home every chunk's bookkeeping now, while the exec chain is final (it came
                # from the file and the dependency restore above only mirrored it) and before
                # anything cooks. Left to the reconcile after load_chunks, this same work is
                # quadratic: each chunk that moves is re-run, and by then a re-run cascades a
                # solve over the entire graph below it. Done here, cook=False, the cook loop
                # immediately below is the one and only pass over the graph.
                # protect_root_layer: the file already records which layer holds each chunk's
                # bookkeeping, so on open the file IS the answer and there is nothing to
                # re-home. Migrating here does active harm -- _migrate_chunk_bookkeeping
                # moves a spec by deleting the original, and on a freshly opened file the
                # original lives in that file's own root layer. Sdf caches layers per
                # process, so the emptied layer is what the NEXT Usd.Stage.Open of the same
                # path receives: loading a network twice in one process restored only the
                # context the second time, while the file on disk stayed perfectly correct.
                # Re-homing still happens on every later topology change, which is what the
                # reconcile is actually for.
                reconcile_edit_targets(stage, scene, cook=False, protect_root_layer=True)

                # Cook exec roots OUTERMOST-FIRST. Only roots are run here; everything with
                # a dependency cooks by cascade from one. But a chunkgroup's inner chunks
                # are roots of their OWN subgraph, and nothing connects them to the group's
                # upstream -- only the GROUP itself is wired into the outer chain -- so the
                # DAG imposes no order between them and the outer chunks whose content they
                # act on. In `deferred` order a group's inner root could therefore cook
                # first, against a stage that did not yet hold what it needs. Live-reported:
                # reopening a saved scene ran an AMD MaterialX Library's inner BindMaterial
                # before the Sublayer feeding the group had brought the mesh onto the stage,
                # so it found no target, authored nothing, and never ran again -- the file
                # opened with the original material still bound. Prim-path depth is exactly
                # the nesting the ordering has to respect (/chunks/Tab1/Sublayer before
                # /chunks/Tab1/AMDMaterialXLibrary/BindMaterial), and sorted() is stable, so
                # chunks at the same depth keep their existing relative order.
                for prim, added in sorted(
                    deferred, key=lambda item: get_chunk_prim_path(item[1]).count("/")
                ):
                    if id(added) not in chunks_with_dependencies:
                        is_top_level = id(added) in top_level_chunk_ids
                        if is_top_level or can_execute_sub_chunks:
                            if lm.initialized:
                                lm.get_or_create_layer(added)
                            added.execute()

                # Fire CHUNK_LOADED for all loaded chunks (UI notification) and restore
                # params that were created dynamically inside execute (e.g. show_attribute).
                for prim, added in deferred:
                    chunk_notice.send_notice(
                        chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, added, data=added
                    )
                    _restore_dynamic_params(prim, added)

                # Second and last expression pass. Chunks that loft their params in the
                # CHUNK_LOADED callback above -- mtlx_shader from its nodedef, wedge from
                # its ranges -- have only just created them, so an expression pointing at
                # one could not resolve in the earlier pass and got no live-update
                # listener. A `.value` read stayed correct, which is why sweeps and exports
                # were right while the UI never updated: the value was fine and only the
                # notification was missing.
                _resolve_pending_expressions(deferred, warn=True)
            finally:
                chunk_notice._loading_stage = old_loading_stage

        # Chunks already homed under a context recreated above — no new tab needed, and
        # the context chunk already exists, so just process them directly.
        if _EXISTING_CONTEXT in groups:
            _add_prim_mods(groups.pop(_EXISTING_CONTEXT)[1])

        # Root-layer chunks first — create a context tab named after the file.
        if None in groups:
            layer = stage.GetRootLayer()
            tab_name = Path(layer.GetDisplayName() or layer.identifier).stem or "Chunks"
            scene.add_context(tab_name)
            _add_prim_mods(groups.pop(None)[1])

        # Sublayer groups — each gets its own context tab for visual organisation
        for src_layer, prim_mods in groups.values():
            tab_name = Path(src_layer.GetDisplayName() or src_layer.identifier).stem or "Chunks"
            scene.add_context(tab_name)
            _add_prim_mods(prim_mods)
    finally:
        chunk_notice._loading_stage = old_loading_stage


def export_chunks(scene: "core.SceneSender", path: "str | Path") -> None:
    """Export the current chunk graph as one clean, portable USD layer.

    Copies each chunk's already-composed, already-correctly-wired prim spec straight
    across from the live stage — connections (inputs:chunk exec wiring, expression
    refs), values, node positions, everything, exactly as currently authored — rather
    than re-deriving them from Python object state via _author_prim. That
    reconstruction was error-prone: chunk.get_dependencies() (the Python DAG) is
    empty for a graph wired live via the node-graph UI's drag-to-connect (which
    authors the USD-level inputs:chunk connection directly and never calls
    add_dependency()) — so _author_prim's prev_chunk fallback silently chained
    chunks in scene.get_chunks() iteration order instead of their real exec order,
    scrambling the graph on export.

    UsdUtils.FlattenLayerStack resolves the live composition first — chunk-prim
    bookkeeping can now be split across several layers (chunk_utils.get_bookkeeping_layer,
    since it follows Edit Target) — giving each prim/attr its correct final composed
    state to copy from, regardless of which layer(s) it's actually scattered across.

    Only chunk prims themselves are copied — reopening regenerates everything a chunk
    drives by re-running load_chunks' cook pass, not by replaying a content snapshot.
    Content that isn't reproduced by re-execution (an RBD Solver's bake) or that never
    went through a chunk at all does not round-trip through this — see save_scene.
    """
    stage = scene.get_stage()
    if not stage:
        return

    flat = UsdUtils.FlattenLayerStack(stage)
    tmp_stage = Usd.Stage.Open(flat)
    chunk_paths = sorted(
        (p.GetPath() for p in tmp_stage.TraverseAll() if p.GetTypeName() == _CHUNK_PRIM_TYPENAME),
        key=lambda p: p.pathElementCount,
    )

    dst = Sdf.Layer.CreateAnonymous()
    for prim_path in chunk_paths:
        if dst.GetPrimAtPath(prim_path):
            continue  # already brought in as part of an ancestor's subtree copy
        parent = prim_path.GetParentPath()
        if not parent.isEmpty and parent != Sdf.Path.absoluteRootPath and not dst.GetPrimAtPath(parent):
            Sdf.CreatePrimInLayer(dst, parent)
        Sdf.CopySpec(flat, prim_path, dst, prim_path)
        _carry_ancestor_specs(flat, dst, prim_path)

    dst.Export(str(path))


def _carry_ancestor_specs(flat: "Sdf.Layer", dst: "Sdf.Layer", prim_path: "Sdf.Path") -> None:
    """Give an exported chunk's ancestors the typename AND specifier they have on the stage.

    Only chunk prims are copied across; their ancestors are conjured by
    Sdf.CreatePrimInLayer, which makes a bare `over` with no typename. The /chunks scope
    used to survive anyway, by accident: it was itself typed Chunk, so it was in the copy
    set and came across as a full def. Typing it Scope took it out of that set and left
    only the over.

    An over is not IsDefined(), and Usd.Stage.Traverse() prunes at an undefined prim --
    taking the whole chunk subtree with it. So an exported file loaded back as an empty
    scene: no scope, no chunks, nothing. Carry the specifier as well, and an ancestor
    that is a real defined prim on the stage stays one in the export.
    """
    parent = prim_path.GetParentPath()
    while not parent.isEmpty and parent != Sdf.Path.absoluteRootPath:
        spec = dst.GetPrimAtPath(parent)
        source = flat.GetPrimAtPath(parent)
        if spec is not None and source is not None:
            if not spec.typeName and source.typeName:
                spec.typeName = source.typeName
            if source.specifier == Sdf.SpecifierDef:
                spec.specifier = Sdf.SpecifierDef
        parent = parent.GetParentPath()


def save_scene(scene: "core.SceneSender", chunkgroup_path: "str | Path") -> None:
    """The "nice save flow": export_chunks, plus the stage metadata it doesn't carry.

    Previously wrote a bundled pair -- the chunk graph plus a flattened snapshot of
    everything else the stage composed, so genuinely non-reproducible content (an RBD
    bake) or content that never went through a chunk at all would still come back.
    Dropped once every chunk that authors real content got a reserved execute() hook
    (see the reload-reliability fixes this shipped alongside): reopening a chunk-only
    export now correctly regenerates everything a chunk is responsible for by re-running
    it, the same as any other reload, so the second file was carrying nothing that
    isn't already reproduced this way -- except a bake, which is expected to need an
    explicit re-run after reload, same as Houdini's File Cache model, and content that
    was never a chunk's in the first place, which this flow was never meant to cover.

    Args:
        chunkgroup_path: Where the chunk graph is written -- the file to reopen.
    """
    stage = scene.get_stage()
    if not stage:
        return

    chunkgroup_path = Path(chunkgroup_path)
    export_chunks(scene, chunkgroup_path)

    # Live-confirmed: up-axis (and by the same rule, units) does NOT compose across
    # sublayers the way ordinary metadata does -- it resolves only from a stage's own
    # root/session layer, falling back to USD's global default (Y) otherwise. Sdf.CopySpec
    # (export_chunks' own copy mechanism) only copies prim specs, so the exported file's
    # own root layer has no opinion at all without this -- reopening it showed Y even
    # though the live scene was Z. Stamp it directly onto the file that matters.
    cg_layer = Sdf.Layer.FindOrOpen(str(chunkgroup_path))
    if cg_layer is not None:
        cg_stage = Usd.Stage.Open(cg_layer)
        UsdGeom.SetStageUpAxis(cg_stage, UsdGeom.GetStageUpAxis(stage))
        UsdGeom.SetStageMetersPerUnit(cg_stage, UsdGeom.GetStageMetersPerUnit(stage))
        cg_layer.Save()


def rename_context_prim_path(context: "core.ChunkBase", new_prim_path: "str | None") -> None:
    """Move all chunk prims in context to a new root using Sdf.CopySpec, then remove the old specs."""
    sync = ChunkStageSync()
    if not sync.initialized or not sync._stage:
        context.prim_path = new_prim_path
        return

    old_root = context.prim_path if context.prim_path else _CHUNKS_ROOT
    new_root = new_prim_path if new_prim_path else _CHUNKS_ROOT

    if old_root == new_root:
        context.prim_path = new_prim_path
        return

    stage = sync._stage
    chunk_objs = [c for c in context.get_my_params() if isinstance(c, core.ChunkBase)]

    # Use prim.GetPrimStack() to find the exact layers authoring each chunk prim,
    # rather than scanning GetLayerStack() which may not find anonymous sublayers.
    layer_chunks: dict = {}  # layer → [chunk_obj, ...]
    for chunk_obj in chunk_objs:
        prim = stage.GetPrimAtPath(Sdf.Path(f"{old_root}/{chunk_obj.name}"))
        if not (prim and prim.IsValid()):
            continue
        for spec in prim.GetPrimStack():
            layer_chunks.setdefault(spec.layer, []).append(chunk_obj)

    if not layer_chunks:
        context.prim_path = new_prim_path
        return

    old_write = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        for layer, to_copy in layer_chunks.items():
            with Sdf.ChangeBlock():
                # Ensure the new root container prim exists in this layer
                new_root_sdf = Sdf.Path(new_root)
                if not layer.GetPrimAtPath(new_root_sdf):
                    root_spec = Sdf.CreatePrimInLayer(layer, new_root_sdf)
                    root_spec.typeName = _CHUNK_PRIM_TYPENAME
                    root_spec.specifier = Sdf.SpecifierDef

                for chunk_obj in to_copy:
                    old_path = Sdf.Path(f"{old_root}/{chunk_obj.name}")
                    new_path = Sdf.Path(f"{new_root}/{chunk_obj.name}")
                    if layer.GetPrimAtPath(old_path):
                        Sdf.CopySpec(layer, old_path, layer, new_path)

                # Fix inputs:chunk connection paths in the copied specs
                for chunk_obj in to_copy:
                    new_path = Sdf.Path(f"{new_root}/{chunk_obj.name}")
                    new_spec = layer.GetPrimAtPath(new_path)
                    if not new_spec or _EXEC_INPUT not in new_spec.attributes:
                        continue
                    exec_attr_spec = new_spec.attributes[_EXEC_INPUT]
                    updated = [
                        Sdf.Path(new_root + str(conn)[len(old_root):])
                        if str(conn).startswith(old_root + "/") else conn
                        for conn in exec_attr_spec.connectionPathList.explicitItems
                    ]
                    exec_attr_spec.connectionPathList.explicitItems = updated

                # Remove old chunk specs
                for chunk_obj in to_copy:
                    old_path = Sdf.Path(f"{old_root}/{chunk_obj.name}")
                    old_spec = layer.GetPrimAtPath(old_path)
                    if not old_spec:
                        continue
                    parent_path = old_path.GetParentPath()
                    parent_spec = layer.GetPrimAtPath(parent_path) or layer.pseudoRoot
                    if old_path.name in parent_spec.nameChildren:
                        del parent_spec.nameChildren[old_path.name]

                # Remove the old root container prim if now empty
                old_root_spec = layer.GetPrimAtPath(Sdf.Path(old_root))
                if old_root_spec and not list(old_root_spec.nameChildren):
                    old_root_path = Sdf.Path(old_root)
                    old_root_parent = layer.GetPrimAtPath(old_root_path.GetParentPath()) or layer.pseudoRoot
                    if old_root_path.name in old_root_parent.nameChildren:
                        del old_root_parent.nameChildren[old_root_path.name]
    finally:
        chunk_notice._usd_write_active = old_write

    # Update path tracking in the sync singleton
    for chunk_obj in chunk_objs:
        old_tracked = f"{old_root}/{chunk_obj.name}"
        new_tracked = f"{new_root}/{chunk_obj.name}"
        sync._loaded_prim_paths.discard(old_tracked)
        sync._loaded_prim_paths.add(new_tracked)
        if sync._chunk_prim_paths.get(id(chunk_obj)) == old_tracked:
            sync._chunk_prim_paths[id(chunk_obj)] = new_tracked

    context.prim_path = new_prim_path

    # Re-wire param attribute listeners to the new prim attributes
    for chunk_obj in chunk_objs:
        prim = stage.GetPrimAtPath(Sdf.Path(f"{new_root}/{chunk_obj.name}"))
        if prim and prim.IsValid():
            _load_wire_inputs(prim, chunk_obj)


def _lofted_attr_name(context_prim: Usd.Prim, context, prefix: str, owner, param_name: str) -> str:
    """The interface name a lofted param takes on the context prim.

    Qualified by the chunk it came from -- Rim_base_color, not base_color -- because a
    group's interface is flat while the params feeding it are not. Six materials in one
    group all offer base_color, and unqualified they landed on a single attribute: the
    second loft silently overwrote the first's value, and only one of them was actually
    driven by it.

    Always qualified, not only on a clash. Qualifying on demand would leave the first
    loft bare and the second prefixed, so the interface would depend on the order someone
    happened to loft in.

    An underscore rather than a colon. A colon is USD's namespace separator and already
    means something here -- inputs:Base:base_color is the param base_color inside the page
    group Base -- and _load_wire_inputs turns that back into a "Base.base_color" lookup.
    Rim:base_color would be read as a group named Rim.

    Suffixed if that is still taken, so lofting the same param twice cannot clobber what
    is already published. "Taken" means the attribute is DECLARED on the prim, not that it
    holds a value -- so a name stays reserved for as long as its declaration survives, even
    after the param that published it is gone. That is deliberate: an interface name is what
    the CLI addresses and what saved files record, so handing it to a different param
    because the first one was unlofted would silently repoint whatever still refers to it.

    The qualifier is a snapshot of the owning chunk's name at loft time, and renaming that
    chunk afterwards does not update it -- a param lofted from Rim stays Rim_base_color even
    if Rim is later renamed Key. Stability wins here for the same reason: the group's
    published interface must not change under anything addressing it by name.
    """
    base = param_name
    if owner is not None and owner is not context and getattr(owner, "name", None):
        base = f"{owner.name}_{param_name}"

    candidate, index = base, 1
    while context_prim.GetAttribute(f"{prefix}{candidate}"):
        candidate = f"{base}{index}"
        index += 1
    return candidate


def loft_param_to_context(param: "core.ChunkParamBase", context: "core.ChunkBase") -> None:
    """Loft a param up to the context prim's public interface.

    Creates ``inputs:{owner}_{param.name}`` on the context prim, connects the inner
    storage attribute to it, and re-binds the Python param to track the context
    attribute — so reads and writes flow through the context prim interface.

    The name is settled here, once, and then belongs to the group. It is deliberately not
    re-derived from the owning chunk later: the interface is what the CLI addresses by
    name, what expressions reference and what saved files record, so renaming an inner
    chunk must not silently change the group's published interface.
    """
    sync = ChunkStageSync()
    if not sync.initialized or not sync._stage:
        return

    stage = sync._stage

    if not param._param_attr_path:
        # A param still at its default has no USD attr yet — create it so there's something to
        # promote (Loft up right after creating a chunk is the common case).
        owner = param.get_chunk()
        if owner is not None:
            sync._sync_param_input(owner, param, force=True)
    if not param._param_attr_path:
        logger.warning("loft_param_to_context: param %r has no USD attribute bound", param.name)
        return

    inner_attr = stage.GetAttributeAtPath(Sdf.Path(param._param_attr_path))
    if not (inner_attr and inner_attr.IsValid()):
        logger.warning("loft_param_to_context: attribute not found at %s", param._param_attr_path)
        return

    # OUTPUT-flagged params loft to outputs:name so a group can expose computed results, not
    # just inputs (data-out ports); everything else is a data-in port (inputs:name).
    prefix = "outputs:" if param.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
    context_prim_path = Sdf.Path(context.prim_path if context.prim_path else _CHUNKS_ROOT)

    context_prim = stage.GetPrimAtPath(context_prim_path)
    if not (context_prim and context_prim.IsValid()):
        context_prim = stage.DefinePrim(context_prim_path)

    if not (context_prim and context_prim.IsValid()):
        logger.warning("loft_param_to_context: could not get/create context prim at %s", context.prim_path)
        return

    # Resolved against the prim, so the name is unique among what is already published.
    lofted_name = _lofted_attr_name(context_prim, context, prefix, param.get_chunk(), param.name)
    attr_name = f"{prefix}{lofted_name}"
    context_attr_path = context_prim_path.AppendProperty(attr_name)

    context_attr = context_prim.CreateAttribute(attr_name, inner_attr.GetTypeName(), custom=True)
    if (val := param.value) is not None:
        context_attr.Set(val)
    inner_attr.SetConnections([context_attr_path])

    # Clone before any state changes so the context widget is fully enabled.
    # _param_attr_path and _driven_attr_paths are NO_COPY so the clone starts unbound;
    # we immediately wire it to the context attr.
    context_param = param.clone()
    # Named to match the attribute it fronts, before add_param -- which uniquifies names
    # itself, and would otherwise hand back base_color1 for a param bound to
    # inputs:Rim_base_color. That mismatch is what made the second loft a phantom control:
    # a distinct param in the panel, wired to somebody else's attribute.
    context_param.name = lofted_name
    context.add_param(context_param)
    # clone() carries over the source param's cached _chunk_parent (the inner chunk), and
    # add_param doesn't invalidate it — so get_chunk() would keep returning the inner chunk
    # instead of the group this param now lives on. That broke lofting a group's own param up
    # again (the "loft to parent group" case): the Loft-up target resolver couldn't tell the
    # param sits on a group, so it re-lofted onto the same group. Clear it so it re-resolves.
    context_param._chunk_parent = None
    context_param._connect_param_attr(context_attr)

    # Forward value changes from the context interface param back through the inner param
    # so that the inner chunk's callbacks fire. _on_objects_changed is suppressed during
    # the USD write, so we bridge the gap with a direct Python callback.
    #
    # The closure must be kept alive for as long as context_param lives — NoticeConnection
    # stores plain functions as weakrefs, so without a strong reference the callback is
    # garbage-collected immediately after this function returns. Pinning it onto
    # context_param ensures the lifetime is tied to the param itself.
    def _forward_to_inner(_owner, _notice, _sender=None):
        val = context_param.value
        if val is not None and param.value != val:
            param.value = val

    context_param._loft_fwd = _forward_to_inner  # strong-ref pin
    context_param.add_callback(
        _forward_to_inner,
        notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_CHANGED,
        send_on_create=False,
        private=True,
    )


def _load_wire_inputs(prim: Usd.Prim, chunk: "core.ChunkBase") -> None:
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith(_INPUTS_PREFIX):
            continue
        param_path = name[len(_INPUTS_PREFIX):].replace(":", ".")

        # List group arrays: inputs:list_name:param_name = StringArray([...])
        # Detect by: parent resolves to a ChunkListGroupBase and value is a Vt array.
        parts = param_path.split(".")
        if len(parts) >= 2:
            parent_path = ".".join(parts[:-1])
            template_name = parts[-1]
            parent = chunk.get_param(parent_path) if parent_path else None
            if isinstance(parent, core.ChunkListGroupBase):
                val = attr.Get()
                if val is not None and hasattr(val, "__iter__") and not isinstance(val, str):
                    values = list(val)
                    if parent.size < len(values):
                        parent.size = len(values)
                    for i, v in enumerate(values):
                        # Setting size/values above can reactively rebuild the list
                        # (e.g. export_usd's stage-driven Layers to Export refresh
                        # fires mid-restore and resizes to the loading stage's layer
                        # count) — re-check the bound every iteration or a shrink
                        # leaves this loop indexing past the end.
                        if i >= len(parent.get_my_params()):
                            break
                        if px := parent.get_indexed_param(template_name, i):
                            px.value = v
                continue

        if param := chunk.get_param(param_path):
            param._connect_param_attr(attr)


def _resolve_pending_expressions(deferred, warn: bool) -> None:
    """Re-assign every @node.param@ expression that still has no live-update listener.

    `_load_wire_inputs` (via `_connect_param_attr`) resolves expressions as each chunk
    loads, but the chunk being referred to may not be registered yet at that point, and a
    param lofted in a CHUNK_LOADED callback does not exist until much later still. Either
    way resolution fails silently and no listener is attached -- a fresh `.value` read is
    correct, so the result looks right and only stops updating when the referenced param
    changes.

    Re-assigning `raw_value` re-runs resolution; anything that already has a listener is
    skipped, so this is idempotent and cheap to run more than once.

    warn only on the last pass. Reporting from an earlier one would flag every expression
    that is merely waiting for a chunk further down the file.
    """
    for _, c in deferred:
        for path, param in _iter_chunk_params(c):
            raw = getattr(param, "raw_value", None)
            if (
                isinstance(raw, str)
                and chunk_utils.has_expression(raw)
                and not getattr(param, "_expression_connections", None)
            ):
                param.value = raw
                if warn and not getattr(param, "_expression_connections", None):
                    logger.warning(
                        "load_chunks: expression %r on %s.%s refers to a param "
                        "that does not exist; it will not update live",
                        raw, c.name, path,
                    )


def _restore_dynamic_params(prim: Usd.Prim, chunk: "core.ChunkBase") -> None:
    """Restore saved values for params created dynamically inside a CHUNK_LOADED callback.

    Params added during execute (e.g. show_attribute) aren't present when the first
    _load_wire_inputs runs before execute, so their chunk-prim storage attrs are skipped.
    After execute fires we scan those attrs again: if a param now exists but is still
    wired to a real-prim attr (not the chunk-prim storage), we set its value from the
    saved attr, and author that value onto the real attr as well -- the write-through the
    param's own listener would normally do is damped while a load is running, and this
    runs inside one.  The resulting PARAM_CHANGED triggers _sync_param_input /
    _sync_vector_input which wires the param to chunk storage and authors the driven USD
    connection.
    """
    prim_path = str(prim.GetPath())
    stage = prim.GetStage()
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith(_INPUTS_PREFIX):
            continue
        param_path = name[len(_INPUTS_PREFIX):].replace(":", ".")

        # List-group arrays (inputs:list_name:param_name = Vt array of per-row values) must
        # be distributed across the list's indexed rows, exactly like _load_wire_inputs does
        # — never set whole onto the scalar row-template param (that raises a type error). A
        # list's rows can be (re)created during execute (e.g. Export USD rebuilding its layer
        # rows), so restoring the saved per-row values here, after execute, is what makes them
        # survive a load.
        parts = param_path.split(".")
        if len(parts) >= 2:
            parent = chunk.get_param(".".join(parts[:-1]))
            if isinstance(parent, core.ChunkListGroupBase):
                val = attr.Get()
                if val is not None and hasattr(val, "__iter__") and not isinstance(val, str):
                    values = list(val)
                    if parent.size < len(values):
                        parent.size = len(values)
                    template_name = parts[-1]
                    for i, v in enumerate(values):
                        if px := parent.get_indexed_param(template_name, i):
                            px.value = v
                continue

        param = chunk.get_param(param_path)
        if param is None:
            continue
        existing = getattr(param, "_param_attr_path", None)
        if existing and existing.startswith(prim_path):
            continue  # already wired to chunk storage from first _load_wire_inputs
        val = attr.Get(0)
        if val is None:
            continue
        param.value = val

        # Push the value to the real prim attribute by hand as well. Assigning the param
        # normally writes through on the ATTRIBUTE_CHANGED listener, but that dispatch is
        # damped for the duration of a load -- and this runs inside one. Without this the
        # param ends up holding the saved value while the prim it drives keeps its schema
        # default, which is a Light chunk reloading as intensity 1 on a 1x1 rectangle and
        # a render lit by whatever fallback the delegate supplies.
        #
        # Authored through the chunk's own edit context so it lands in the chunk's layer,
        # the same place its execute() would have put it, and is rebuilt with the rest on
        # the next cook rather than lingering as an override nobody wrote.
        if existing:
            bound = stage.GetAttributeAtPath(Sdf.Path(existing))
            if bound:
                with Usd.EditContext(stage, param.get_edit_context()):
                    bound.Set(val)


def _restore_lofted_params(stage: Usd.Stage, deferred: list) -> None:
    """Recreate Python-side lofted context params that were saved to USD.

    Scans the context prim's inputs: attributes and matches them to inner chunk
    params whose USD attribute has a connection pointing back to the context prim.
    Reconstructs the context_param clone and its forward-to-inner callback so the
    lofted UI and value forwarding work identically to a freshly lofted param.
    """
    seen_contexts: set = set()

    for _prim, chunk in deferred:
        context = chunk.get_context()
        if not context or id(context) in seen_contexts:
            continue

        context_prim_path = Sdf.Path(context.prim_path if context.prim_path else _CHUNKS_ROOT)
        context_prim = stage.GetPrimAtPath(context_prim_path)
        if not (context_prim and context_prim.IsValid()):
            continue

        seen_contexts.add(id(context))

        for context_attr in context_prim.GetAttributes():
            attr_name = context_attr.GetName()
            if not attr_name.startswith(_INPUTS_PREFIX):
                continue

            param_name = attr_name[len(_INPUTS_PREFIX):]
            # Only check direct ChunkParamBase children of context (already-lofted),
            # not nested params inside inner chunks which share the same name.
            if any(isinstance(p, core.ChunkParamBase) and p.name == param_name
                   for p in context.get_my_params()):
                continue

            context_attr_path_str = str(context_attr.GetPath())

            inner_param = None
            for inner_chunk in context.get_my_params():
                if not isinstance(inner_chunk, core.ChunkBase):
                    continue
                for _, param in _iter_chunk_params(inner_chunk):
                    if not param._param_attr_path:
                        continue
                    inner_attr = stage.GetAttributeAtPath(Sdf.Path(param._param_attr_path))
                    if not (inner_attr and inner_attr.IsValid()):
                        continue
                    if any(str(c) == context_attr_path_str for c in inner_attr.GetConnections()):
                        inner_param = param
                        break
                if inner_param:
                    break

            if not inner_param:
                continue

            # Named from the attribute, not from the inner param the clone came from.
            # The interface name is the file's to decide -- it is qualified at loft time
            # (Rim_base_color) and may have been renamed since -- whereas the clone would
            # arrive calling itself base_color. Getting this wrong reverts the name on
            # every reload, and also defeats the already-lofted guard above, which
            # compares against the attribute's name: the param would never match, so a
            # duplicate would be added each time the file was opened.
            context_param = inner_param.clone()
            context_param.name = param_name
            context_param._connect_param_attr(context_attr)
            context.add_param(context_param)

            def _forward_to_inner(_owner, _notice, _sender=None, _ip=inner_param, _cp=context_param):
                val = _cp.value
                if val is not None and _ip.value != val:
                    _ip.value = val

            context_param._loft_fwd = _forward_to_inner
            context_param.add_callback(
                _forward_to_inner,
                notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_CHANGED,
                send_on_create=False,
                private=True,
            )

            # Deliver the restored value down to the inner param once, by hand. The inner
            # param's own _connect_param_attr deliberately leaves its value alone when it
            # sees a loft connection, because a lofted value is meant to arrive through
            # this callback rather than be read from USD twice. On load nothing ever sent
            # it: _connect_param_attr above set the context param before the callback
            # existed, and send_on_create=False means registering it sends nothing. The
            # inner param was left holding its type default -- an empty file path on a
            # Reference chunk, which cooks to no reference and renders an empty stage --
            # while USD and the lofted UI both showed the saved value. Seeding here is
            # before the execute pass, so the first cook already sees the real value.
            _forward_to_inner(None, None)


def _live_exec_dependencies(chunk: "core.ChunkBase") -> list:
    """Return the chunks chunk's *live* inputs:chunk connection currently points at,
    read directly from the live session's stage.

    This is the authoritative wiring for a graph built interactively — the node-graph
    UI's drag-to-connect authors the USD-level connection directly, never calling
    add_dependency(), so chunk.get_dependencies() (the Python DAG) stays empty until a
    save+reload cycle reconstructs it (load_chunks does this same lookup). Without
    this, _author_prim's prev_chunk fallback would chain chunks in scene.get_chunks()
    iteration order instead of their real exec order — silently scrambling the graph
    on export (and breaking anything, like an Edit Target chunk, whose correctness
    depends on running in the right sequence relative to what it references).
    """
    stage = chunk.get_stage()
    if not stage:
        return []
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not prim.IsValid():
        return []
    # Every slot: a merge's branches past the first are real DAG dependencies, and missing
    # them here would leave reconcile_exec_dependencies removing edges it cannot see.
    exec_conns = exec_input_connections(prim)
    if not exec_conns:
        return []
    # Resolve by prim path, not chunk name — names are scene-wide and can be
    # uniquified/renamed, so a name lookup can bind to the wrong chunk (same
    # rationale as load_chunks' dependency restore).
    chunk_by_path = {
        get_chunk_prim_path(c): c for c in chunk_registry.ChunkRegistry().scene.get_chunks()
    }
    deps = []
    for conn_path in exec_conns:
        if dep := chunk_by_path.get(str(conn_path.GetPrimPath())):
            deps.append(dep)
    return deps


def _param_wire_source_paths(prim: "Usd.Prim") -> "set[str]":
    """The prim paths this chunk reads through a *param* wire, exec slots excluded."""
    exec_sources = {str(conn.GetPrimPath()) for conn in exec_input_connections(prim)}
    return _connected_source_paths(prim) - exec_sources


def _live_chunk_dependencies(chunk: "core.ChunkBase") -> list:
    """Every chunk this one reads from on the live stage — exec slots *and* param wires.

    _live_exec_dependencies answers the narrower question (which chunks feed my exec
    slots) and stays that way: it is what authors inputs:chunk, and a param wire must
    never be written out as an exec edge.

    This is the wider one, for the Python DAG. A param wire already propagates change --
    _on_ref_changed executes the destination param's own sender, which dirties its chunk
    through the ownership chain -- but it carries no dependency edge, so two chunks joined
    only that way both sit at depth 0: no INPUT_CHANGED between them, arbitrary commit
    order (hence commit_order_from_stage), and no guarantee the downstream one solves after
    the upstream one when a single propagation dirties both.

    Source of truth is _connected_source_paths, the same reader commit_order_from_stage
    orders by, so the two cannot drift — and its carve-outs (a self-reference, and an
    ancestor target, which is a loft into a group's interface rather than a sibling
    dependency) apply here unchanged.
    """
    stage = chunk.get_stage()
    if not stage:
        return []
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not prim.IsValid():
        return []
    sources = _connected_source_paths(prim)
    if not sources:
        return []
    # Resolve by prim path, not chunk name — same rationale as _live_exec_dependencies.
    chunk_by_path = {
        get_chunk_prim_path(c): c for c in chunk_registry.ChunkRegistry().scene.get_chunks()
    }
    return [dep for path in sources if (dep := chunk_by_path.get(path)) is not None]


def _would_cycle(chunk: "core.ChunkBase", dep: "core.ChunkBase") -> bool:
    """Whether making chunk depend on dep would close a loop.

    Adding the edge means dep must solve before chunk, so it cycles exactly when dep is
    already downstream of chunk.

    Worth the check rather than letting topo_order raise: a param wire cycle currently
    settles-and-warns (see _on_ref_changed, and _resolve_expression_string, both of which
    run during widget paint where a raise is far worse than a stale value), and CONTEXT.md
    records that tolerance as deliberate. Skipping the edge keeps cyclic wiring behaving
    exactly as it does today while acyclic wiring — the overwhelming majority — gains the
    ordering it was missing.
    """
    return dep.get_dag_node() in chunk_scheduler.downstream_closure(chunk.get_dag_node())


def exec_dependencies(chunk: "core.ChunkBase") -> list:
    """The chunks feeding this one's *exec* slots, read from the live stage.

    The public way for a chunk module to ask "what is immediately upstream of me on the
    exec chain". chunk.get_dependencies() is not that question: the Python DAG also
    mirrors param wires, which mean a value flows rather than that one chunk precedes
    another.

    Args:
        chunk (core.ChunkBase): The chunk to read the exec inputs of

    Returns:
        list: The upstream chunks, one per connected exec slot
    """
    return _live_exec_dependencies(chunk)


def reconcile_exec_dependencies(chunk: "core.ChunkBase") -> bool:
    """Make chunk's Python DAG dependencies match its live inputs:chunk connections.

    The stage is the authority for chunk-level exec wiring: the node-graph UI's
    drag-to-connect authors inputs:chunk directly and never calls add_dependency(), so
    without this the Python DAG holds no edge at all for an interactively built graph and
    INPUT_CHANGED never fires between two chunks the user visibly wired together.

    Adding the edge here also dirties the dependent, so an edge change propagates downstream
    exactly like a param change does.

    Only chunk-level dependencies are touched. Param-level dependencies (added via the
    c_param.add_dependency(a_param) API) have no inputs:chunk counterpart and are left alone.

    Args:
        chunk (core.ChunkBase): The chunk whose inputs:chunk connections changed

    Returns:
        bool: True if any dependency was added or removed
    """
    live = _live_chunk_dependencies(chunk)
    current = [dep for _, dep in chunk.get_dependencies() if isinstance(dep, core.ChunkBase)]

    live_ids = {id(dep) for dep in live}
    current_ids = {id(dep) for dep in current}

    changed = False
    for dep in current:
        if id(dep) not in live_ids:
            changed = chunk.remove_dependency(dep) or changed
    for dep in live:
        if id(dep) in current_ids:
            continue
        if _would_cycle(chunk, dep):
            logger.debug(
                "Skipping dependency %s -> %s: it would close a cycle, leaving the wire to "
                "settle as it does today", dep.name, chunk.name,
            )
            continue
        chunk.add_dependency(dep)
        changed = True
    return changed


def _author_prim(stage: Usd.Stage, chunk: "core.ChunkBase", prev_chunk: "core.ChunkBase | None" = None, index: int = 0) -> None:
    chunk_root = _resolve_chunk_root(chunk)
    _define_chunk_parent(stage, chunk_root)

    prim = stage.DefinePrim(Sdf.Path(f"{chunk_root}/{chunk.name}"), _CHUNK_PRIM_TYPENAME)

    ui_api = UsdUI.NodeGraphNodeAPI.Apply(prim)
    if not ui_api.GetPosAttr().HasAuthoredValue():
        ui_api.CreatePosAttr(Gf.Vec2f(index * _NODE_STEP, 0.0))

    config = chunk.get_config()
    if config and config.module:
        attr = prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, custom=False)
        attr.SetVariability(Sdf.VariabilityUniform)
        attr.Set(config.module)

    if not (config and config.terminal):
        exec_out = prim.CreateAttribute(_EXEC_OUTPUT, Sdf.ValueTypeNames.Token, custom=False)
        exec_out.SetVariability(Sdf.VariabilityUniform)

    if config and getattr(config, "outputs", None):
        for out_name, out_type_name in config.outputs:
            out_type = getattr(Sdf.ValueTypeNames, out_type_name, None)
            if out_type is not None:
                prim.CreateAttribute(f"outputs:{out_name}", out_type, custom=False)

    chunk_gizmo.author_gizmos(stage, prim, chunk)

    # The stage's exec wiring comes first, and the Python DAG is only the fallback for a
    # chunk that has none yet. The DAG now also holds edges reconciled from *param* wires
    # (see _live_chunk_dependencies), and authoring one of those as inputs:chunk would
    # invent an exec edge the user never drew -- so the fallback filters them back out.
    deps = _live_exec_dependencies(chunk)
    if not deps:
        wired = _param_wire_source_paths(prim)
        deps = [
            dep for _, dep in chunk.get_dependencies()
            if isinstance(dep, core.ChunkBase) and get_chunk_prim_path(dep) not in wired
        ]
    if not deps and prev_chunk:
        deps = [prev_chunk]
    if deps:
        exec_in = prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        exec_in.SetConnections([Sdf.Path(exec_output_path(dep)) for dep in deps])
        invalidate_edit_layer_memo()

    # Build a name → context_param map for lofted params (direct ChunkParamBase children of context)
    context = chunk.get_context()
    lofted: dict[str, "core.ChunkParamBase"] = {}
    if context:
        for p in context.get_my_params():
            if isinstance(p, core.ChunkParamBase) and getattr(p, "_param_attr_path", None):
                lofted[p.name] = p

    for path, param in _iter_chunk_params(chunk):
        store_value = param.raw_value
        if store_value is None:
            store_value = param.value  # proxy params delegate raw_value to the template (None); use value instead
        if store_value is None:
            continue
        if store_value == getattr(param, "default_value", None):
            continue
        sdf_type, usd_value = _sdf_type_and_value(store_value)
        if sdf_type is None:
            continue

        attr_name = f"{_INPUTS_PREFIX}{path.replace('.', ':')}"
        ctx_param = lofted.get(param.name)

        if ctx_param:
            # Param is lofted: write a connection on the inner attr and author the context prim's attr
            ctx_attr_path_str = ctx_param._param_attr_path  # e.g. "/chunks.inputs:mySlider"
            ctx_prim_str, ctx_attr_name = ctx_attr_path_str.rsplit(".", 1)
            ctx_prim = stage.GetPrimAtPath(Sdf.Path(ctx_prim_str))
            if not (ctx_prim and ctx_prim.IsValid()):
                ctx_prim = stage.DefinePrim(Sdf.Path(ctx_prim_str), _CHUNK_PRIM_TYPENAME)
            ctx_attr = ctx_prim.CreateAttribute(ctx_attr_name, sdf_type, custom=True)
            ctx_attr.Set(ctx_param.value if ctx_param.value is not None else param.value)

            inner_attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
            inner_attr.Set(ctx_param.value if ctx_param.value is not None else usd_value)
            inner_attr.SetConnections([Sdf.Path(ctx_attr_path_str)])
        else:
            attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
            attr.Set(usd_value)
            if isinstance(store_value, str) and chunk_utils.has_expression(store_value):
                attr.SetConnections([
                    Sdf.Path(f"{chunk_root}/{node_name}.inputs:{param_name}")
                    for node_name, param_name in chunk_utils.parse_expression(store_value)
                ])

    for vec_path, vector in _iter_chunk_vectors(chunk):
        raw = vector.raw_value
        is_vec_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
        if not is_vec_expr:
            val = vector.value
            if not val or not any(v is not None for v in val):
                continue
            if val == vector.default_value:
                continue
        prefix = "outputs:" if vector.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
        attr_name = f"{prefix}{vec_path.replace('.', ':')}"
        # If the vector is already wired to a real USD attr, preserve its exact type
        backing = getattr(vector, '_param_attr_path', None)
        if not is_vec_expr and backing:
            real_attr = stage.GetAttributeAtPath(Sdf.Path(backing))
            if real_attr and real_attr.IsValid() and real_attr.HasAuthoredValue():
                if (current_val := real_attr.Get()) is not None:
                    attr = prim.CreateAttribute(attr_name, real_attr.GetTypeName(), custom=False)
                    attr.Set(current_val)
                    continue
        if is_vec_expr:
            type_source = vector.default_value
            sdf_type, _ = _sdf_vector_type(type_source) if type_source else (None, None)
        else:
            sdf_type, usd_value = _sdf_vector_type(val)
        if sdf_type is None:
            continue
        attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
        if is_vec_expr:
            attr.SetConnections([
                Sdf.Path(f"{chunk_root}/{node_name}.inputs:{param_name}")
                for node_name, param_name in chunk_utils.parse_expression(raw)
            ])
            continue
        attr.Set(usd_value)

    # Serialize ListGroup template params as arrays: inputs:list_name:param_name = [v0, v1, ...]
    for lg_path, list_group in _iter_list_group_arrays(chunk):
        if not list_group.proxy_params:
            continue
        for proxy_param in list_group.proxy_params[0].get_my_params():
            if not isinstance(proxy_param, core.ChunkParamBase):
                continue
            values = [
                v for i in range(list_group.size)
                if (px := list_group.get_indexed_param(proxy_param.name, i)) is not None
                for v in [px.value]
                if v is not None
            ]
            if not values:
                continue
            sdf_type, usd_value = _sdf_type_and_value(values)
            if sdf_type is None:
                continue
            attr = prim.CreateAttribute(f"{_INPUTS_PREFIX}{lg_path.replace('.', ':')}:{proxy_param.name}", sdf_type, custom=False)
            attr.Set(usd_value)
