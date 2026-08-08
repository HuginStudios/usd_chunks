"""
USD I/O plugin for chunks.
Handles layer management, stage synchronization, and chunk serialization.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
from pathlib import Path
from typing import Generator, Tuple

from pxr import Gf, Sdf, Usd, UsdUI, UsdUtils, Vt

from usd_chunks import chunk as _chunk_mod
from usd_chunks import chunk_registry
from usd_chunks import chunk_core as core
from usd_chunks import chunk_notice
from usd_chunks import chunk_utils
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection, RegistrySingleton
from usd_chunks.chunk_registry import ChunkLayerManager  # noqa: F401  re-exported

logger = logging.getLogger(__name__)

_CHUNK_TYPE_ATTR = "info:chunkType"
_INPUTS_PREFIX = "inputs:"
_EXEC_OUTPUT = "outputs:chunk"
_EXEC_INPUT = "inputs:chunk"
_CHUNK_PRIM_TYPENAME = "Chunk"
_CHUNKS_ROOT = "/chunks"

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

_SDF_TYPE_MAP = {
    bool: Sdf.ValueTypeNames.Bool,
    int: Sdf.ValueTypeNames.Int,
    float: Sdf.ValueTypeNames.Float,
    str: Sdf.ValueTypeNames.String,
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
            reconcile_edit_targets(stage, scene)

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
        if chunk_notice._usd_write_active or not self._scene:
            return
        stage = notice.GetStage()
        if not stage:
            return
        scene_stage = self._scene.get_stage()
        if not scene_stage or stage.GetRootLayer().identifier != scene_stage.GetRootLayer().identifier:
            return
        self._stage = stage
        load_chunks(stage, self._scene, self._loaded_prim_paths)

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

            root_sdf = Sdf.Path(chunk_root)
            root_prim = self._stage.GetPrimAtPath(root_sdf)
            if root_prim.IsValid() and not any(
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
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not prim.IsValid():
            return
        disabled = sender.has_state(ChunkFlags.State.DISABLED)
        if prim.IsActive() == disabled:
            prim.SetActive(not disabled)

    def _on_objects_changed(self, notice: Usd.Notice.ObjectsChanged, sender):
        if chunk_notice._usd_write_active or self._reconciling_exec or not self._scene:
            return
        scene_stage = self._scene.get_stage()
        if not scene_stage or sender.GetRootLayer().identifier != scene_stage.GetRootLayer().identifier:
            return
        self._stage = sender
        changed = {str(p) for p in notice.GetChangedInfoOnlyPaths()}
        # Include resynced paths for editors (e.g. usdview) that author active via the layer
        all_changed = changed | {str(p) for p in notice.GetResyncedPaths()}
        if not all_changed:
            return
        self._reconcile_changed_exec_inputs(all_changed)
        for chunk in self._scene.get_chunks():
            if changed:
                for _, param in _iter_chunk_params(chunk):
                    data_path = getattr(param, "_param_attr_path", None)
                    if not data_path or data_path not in changed:
                        continue
                    attr = sender.GetAttributeAtPath(Sdf.Path(data_path))
                    if attr and attr.IsValid():
                        val = attr.Get(0)
                        raw = getattr(param, 'raw_value', None)
                        if val is not None and val != param.value and not (isinstance(raw, str) and chunk_utils.has_expression(raw)):
                            param.value = val
                for _, vector in _iter_chunk_vectors(chunk):
                    data_path = getattr(vector, "_param_attr_path", None)
                    if not data_path or data_path not in changed:
                        continue
                    attr = sender.GetAttributeAtPath(Sdf.Path(data_path))
                    if attr and attr.IsValid():
                        val = attr.Get(0)
                        if val is not None:
                            vector.value = val
            chunk_prim_path = self._chunk_prim_paths.get(id(chunk))
            if not chunk_prim_path or chunk_prim_path not in all_changed:
                continue
            prim = sender.GetPrimAtPath(Sdf.Path(chunk_prim_path))
            if not prim.IsValid():
                continue
            is_active = prim.IsActive()
            is_disabled = chunk.has_state(ChunkFlags.State.DISABLED)
            if not is_active and not is_disabled:
                chunk.set_state(ChunkFlags.State.DISABLED)
            elif is_active and is_disabled:
                chunk.unset_state(ChunkFlags.State.DISABLED)

    def _reconcile_changed_exec_inputs(self, all_changed: "set[str]") -> None:
        """Mirror any changed inputs:chunk connection into the Python DAG.

        A connection edit is reported against the *attribute* path
        (/chunks/b.inputs:chunk), not the prim — verified against Usd.Notice.ObjectsChanged,
        which puts it in GetChangedInfoOnlyPaths(). The rest of _on_objects_changed matches
        on prim paths only, which is why this wiring went unnoticed there for so long.

        Routing this through the stage notice rather than the node graph's
        _author_connection keeps every authoring path in sync — the Qt graph, the CLI, a
        USD-level edit in usdview, and any future frontend — instead of each one having to
        remember to call add_dependency itself.
        """
        exec_suffix = "." + _EXEC_INPUT
        touched_prims = {path[: -len(exec_suffix)] for path in all_changed if path.endswith(exec_suffix)}
        if not touched_prims:
            return

        chunks_by_prim = {prim_path: chunk_id for chunk_id, prim_path in self._chunk_prim_paths.items()}
        wanted_ids = {chunks_by_prim[p] for p in touched_prims if p in chunks_by_prim}
        if not wanted_ids:
            return

        self._reconciling_exec = True
        try:
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
        with self._chunk_prim_edit_context(dependent):
            exec_in = dep_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
            existing = list(exec_in.GetConnections())
            if src_path not in existing:
                exec_in.SetConnections(existing + [src_path])
        # Wiring a chunk into the exec chain can change the Edit Target that now applies at
        # its position (e.g. it's now downstream of an Edit Target chunk) — re-home it and
        # everything downstream to honor the target at that place.
        reconcile_edit_targets(self._stage, self._scene)

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
                    if sdf_type == Sdf.ValueTypeNames.String:
                        attr.Set(raw)
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
                        if sdf_type == Sdf.ValueTypeNames.String:
                            attr.Set(raw)
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
            exec_in = prim.GetAttribute(_EXEC_INPUT)
            if exec_in.IsValid():
                for conn in exec_in.GetConnections():
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
            self._stage.DefinePrim(Sdf.Path(chunk_root), _CHUNK_PRIM_TYPENAME)

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
                    if sdf_type == Sdf.ValueTypeNames.String:
                        attr.Set(raw)
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
            if not (isinstance(val, str) and old in val):
                continue
            home = chunk_utils.get_attr_home_layer(attr)
            if not home:
                continue
            with Usd.EditContext(stage, home):
                attr.Set(val.replace(old, new))


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
    """Return True if chunk is a chunkgroup (a CONTEXT chunk with a module — a subnet node)."""
    cfg = chunk.get_config()
    return bool(chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT) and cfg and cfg.module)


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
            exec_in = prim.GetAttribute(_EXEC_INPUT)
            if not exec_in.IsValid():
                continue
            if any(str(conn.GetPrimPath()) in frontier for conn in exec_in.GetConnections()):
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
            if ident not in excluded_ids and ident not in layer_ids:
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
    exec_in = del_prim.GetAttribute(_EXEC_INPUT)
    pred_conns = list(exec_in.GetConnections()) if (exec_in and exec_in.IsValid()) else []
    out_path = f"{deleted_path}.{_EXEC_OUTPUT}"
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        for prim in stage.Traverse():
            if prim.GetPath() == deleted:
                continue
            succ = prim.GetAttribute(_EXEC_INPUT)
            if not (succ and succ.IsValid()):
                continue
            if any(str(c) == out_path for c in succ.GetConnections()):
                if pred_conns:
                    succ.SetConnections(pred_conns)
                else:
                    succ.ClearConnections()
    finally:
        chunk_notice._usd_write_active = prev_w


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
    start, yielding each reachable (path, prim) exactly once, start itself excluded.
    prim may be invalid (dangling connection) — such nodes are yielded but not walked
    through. Shared by every "look upstream" consumer so the traversal semantics
    (dedup, ordering, dangling handling) live in one place.

    Deliberately exec-only (inputs:chunk), NOT the exec+data walk get_exec_upstream_chunks
    does: Edit Target resolution follows the execution chain that actually defines
    authoring position, and must ignore incidental data wiring — a chunk reading another's
    points shouldn't drag its Edit Target upstream. The two are separate on purpose."""
    seen = {start}
    frontier = [start]
    while frontier:
        next_frontier: list = []
        for path in frontier:
            p = stage.GetPrimAtPath(path)
            if path != start:
                yield path, p
            if not (p and p.IsValid()):
                continue
            exec_in = p.GetAttribute(_EXEC_INPUT)
            if exec_in and exec_in.IsValid():
                for conn in exec_in.GetConnections():
                    conn_prim = conn.GetPrimPath()
                    if conn_prim not in seen:
                        seen.add(conn_prim)
                        next_frontier.append(conn_prim)
        frontier = next_frontier


def nearest_upstream_edit_layer(stage: "Usd.Stage", prim: "Usd.Prim") -> "Sdf.Layer | None":
    """Return the layer a chunk at prim's position should author into: the layer published
    by the nearest Edit Target chunk reachable by walking the exec chain (inputs:chunk)
    upstream from prim, or the session layer if there is no upstream Edit Target.

    A pure function of graph topology — this is what replaces the old global
    ChunkStageSync._user_edit_layer state. prim's own marker is deliberately skipped (an
    Edit Target chunk defines the target for everything *downstream* of it, not for its own
    bookkeeping — that follows whatever is upstream of it, or the session layer).
    """
    if not stage:
        return None
    session = stage.GetSessionLayer()
    if not (prim and prim.IsValid()):
        return session
    for _path, p in _iter_exec_upstream(stage, prim.GetPath()):
        if not (p and p.IsValid()):
            continue
        layer = _prim_edit_target_layer(stage, p)
        if layer is not None:
            return layer
    return session


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


def _migrate_chunk_bookkeeping(stage: "Usd.Stage", prim_path: str, dst_layer: "Sdf.Layer") -> bool:
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
    internal = chunk_utils.chunk_internal_layer_ids()
    holders = [
        layer for layer in stage.GetLayerStack(includeSessionLayers=True)
        if layer.identifier not in internal and layer.GetPrimAtPath(sdf_path)
    ]
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


def reconcile_edit_targets(stage: "Usd.Stage", scene: "core.SceneSender") -> None:
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
    """
    global _reconciling
    if _reconciling or not (stage and scene):
        return
    _reconciling = True
    try:
        for chunk in scene.get_chunks():
            if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                continue
            prim_path = get_chunk_prim_path(chunk)
            prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
            if not prim.IsValid():
                continue
            dst = nearest_upstream_edit_layer(stage, prim)
            if dst is None:
                continue
            if _migrate_chunk_bookkeeping(stage, prim_path, dst):
                # The chunk moved layers — re-run it so its driven content re-nests under
                # the new target (bookkeeping is hand-moved above; driven content follows
                # via ChunkLayerManager on execute).
                chunk.execute()
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
            1 for path, _p in _iter_exec_upstream(stage, start) if str(path) in et_paths
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
        return srcs

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
) -> None:
    """Apply view-driven execution for one context.

    With a view_chunk: cook only its upstream tree (roots-first); every other non-view
    chunk in the context is blocked + muted (dormant, hidden). With view_chunk=None (no active
    view): block + mute everything — nothing cooks until a view is activated.

    Blocking a chunk both hides its result (its layer is muted) and stops it reacting to
    param changes (see ChunkBase.value_changed) — so being outside the active view's tree
    means "does not execute", live.
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

    if view_chunk is None:
        cook_list: "list[core.ChunkBase]" = []
        cook_set: set = set()
    else:
        cook_list = get_exec_upstream_chunks(view_chunk, stage, scene)
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
    for c in scene.get_chunks():
        if is_view_chunk(c) or not in_context(c):
            continue
        if is_group_chunk(c):
            if c in cook_set:
                recook_for_view(stage, scene, c, find_group_active_view(c, stage, scene))
            else:
                recook_for_view(stage, scene, c, None)
            continue
        c.set_blocked(c not in cook_set)
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
    ident_rank: dict = {}
    for c in cook_list:
        layer = lm.get_layer(c)
        if layer is not None:
            ident_rank[layer.identifier] = rank[id(c)]
    for g_layer in lm.graph_layers.values():
        ordered = sorted(
            g_layer.subLayerPaths,
            key=lambda ident: ident_rank.get(ident, -1),
            reverse=True,
        )
        if list(g_layer.subLayerPaths) != ordered:
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
        for c in scene.get_chunks():
            if is_view_chunk(c):
                continue
            cp = get_chunk_prim_path(c)
            # Keep the focused group itself and its descendants — recook_for_view handles those.
            if cp == group_path or cp.startswith(inside_prefix):
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
        exec_in = prim.GetAttribute(_EXEC_INPUT)
        if exec_in.IsValid():
            for conn in exec_in.GetConnections():
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
        exec_in = prim.GetAttribute(_EXEC_INPUT)
        if not exec_in.IsValid():
            continue
        for conn in exec_in.GetConnections():
            if str(conn.GetPrimPath()) in candidate_paths:
                has_non_view_input.add(get_chunk_prim_path(c))
    return [c for c in candidates if get_chunk_prim_path(c) not in has_non_view_input]


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


def load_chunks(stage: Usd.Stage, scene: "core.SceneSender", loaded_prim_paths: set[str] | None = None) -> None:
    if not stage:
        return

    to_load = []
    # Top-level (direct child of the pseudo-root) Chunk-typed prims with no chunkType —
    # a structural context prim (a Tab/ChunkGroup saved via export_chunks), not a
    # module-backed chunk. Nested ChunkGroups use a separate parent-context mechanism
    # (_parent_context) not covered by this reload path yet.
    context_prims = []
    for prim in stage.Traverse():
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
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
                        c.prim_path = str(prim.GetPath())
                        object.__setattr__(c, '_exec_prim_path', str(prim.GetPath()))
                        scene.add_chunk(c)
                        if c.get_config() and c.get_config().module:
                            group_chunk_by_path[str(prim.GetPath())] = c
                    elif ancestor_group is not None:
                        # Sub-chunk of a ChunkGroup: restore saved values before
                        # add_param so PARAM_ADDED widgets see the correct values.
                        _load_wire_inputs(prim, c)
                        ancestor_group.add_param(c, notify=False)
                        object.__setattr__(c, '_exec_prim_path', str(prim.GetPath()))
                        deferred.append((prim, c))
                    else:
                        # Restore saved param values before scene.add_chunk so the
                        # PARAM_ADDED widget sees the correct values on creation.
                        _load_wire_inputs(prim, c)
                        if added := scene.add_chunk(c, notify=False):
                            object.__setattr__(added, '_exec_prim_path', str(prim.GetPath()))
                            deferred.append((prim, added))
                            top_level_chunk_ids.add(id(added))

            # _load_wire_inputs above (via _connect_param_attr) resolves @node.param@
            # expressions to set up the live-update listener (_expression_connections),
            # but the referenced chunk may not have been registered yet at that point
            # in this same loop — resolution silently fails and no listener gets
            # attached, so later edits to the referenced chunk never notify this
            # param/widget even though a fresh .value read is correct. Now that every
            # chunk in this batch is registered, retry once for anything still empty.
            for _, c in deferred:
                for _, param in _iter_chunk_params(c):
                    raw = getattr(param, 'raw_value', None)
                    if (
                        isinstance(raw, str)
                        and chunk_utils.has_expression(raw)
                        and not getattr(param, '_expression_connections', None)
                    ):
                        param.value = raw

            # Restore DAG dependencies from inputs:chunk connections so execution
            # order is driven by the explicit graph, not just prim appearance order.
            # Resolve connection targets by prim path — names are scene-wide and can
            # be uniquified/renamed, so a name lookup can bind to the wrong chunk.
            chunk_by_path = {str(p.GetPath()): c for p, c in deferred}
            chunks_with_dependencies: set[int] = set()
            for prim, chunk in deferred:
                exec_attr = prim.GetAttribute(_EXEC_INPUT)
                if not (exec_attr and exec_attr.IsValid()):
                    continue
                existing_deps = {id(d) for _, d in chunk.get_dependencies()}
                for conn_path in exec_attr.GetConnections():
                    dep = chunk_by_path.get(str(conn_path.GetPrimPath()))
                    if dep is not None and id(dep) not in existing_deps:
                        chunk.add_dependency(dep)
                        chunks_with_dependencies.add(id(chunk))

            _restore_lofted_params(stage, deferred)

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

            # Re-sort graph sublayers so downstream chunks (higher sort_key after
            # wiring) end up with stronger layers than their upstream dependencies.
            # Layers are created early (during _load_wire_inputs) when sort_keys
            # are all 0, so the initial insertion order needs correction here.
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

            for prim, added in deferred:
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

    dst.Export(str(path))


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


def loft_param_to_context(param: "core.ChunkParamBase", context: "core.ChunkBase") -> None:
    """Loft a param up to the context prim's public interface.

    Creates ``inputs:{param.name}`` on the context prim, connects the inner
    storage attribute to it, and re-binds the Python param to track the context
    attribute — so reads and writes flow through the context prim interface.
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
    attr_name = f"{prefix}{param.name}"
    context_prim_path = Sdf.Path(context.prim_path if context.prim_path else _CHUNKS_ROOT)
    context_attr_path = context_prim_path.AppendProperty(attr_name)

    context_prim = stage.GetPrimAtPath(context_prim_path)
    if not (context_prim and context_prim.IsValid()):
        context_prim = stage.DefinePrim(context_prim_path)

    if not (context_prim and context_prim.IsValid()):
        logger.warning("loft_param_to_context: could not get/create context prim at %s", context.prim_path)
        return

    context_attr = context_prim.CreateAttribute(attr_name, inner_attr.GetTypeName(), custom=True)
    if (val := param.value) is not None:
        context_attr.Set(val)
    inner_attr.SetConnections([context_attr_path])

    # Clone before any state changes so the context widget is fully enabled.
    # _param_attr_path and _driven_attr_paths are NO_COPY so the clone starts unbound;
    # we immediately wire it to the context attr.
    context_param = param.clone()
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


def _restore_dynamic_params(prim: Usd.Prim, chunk: "core.ChunkBase") -> None:
    """Restore saved values for params created dynamically inside a CHUNK_LOADED callback.

    Params added during execute (e.g. show_attribute) aren't present when the first
    _load_wire_inputs runs before execute, so their chunk-prim storage attrs are skipped.
    After execute fires we scan those attrs again: if a param now exists but is still
    wired to a real-prim attr (not the chunk-prim storage), we set its value from the
    saved attr.  The resulting PARAM_CHANGED triggers _sync_param_input / _sync_vector_input
    which wires the param to chunk storage and authors the driven USD connection.
    """
    prim_path = str(prim.GetPath())
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
        if val is not None:
            param.value = val


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

            context_param = inner_param.clone()
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
    exec_attr = prim.GetAttribute(_EXEC_INPUT)
    if not (exec_attr and exec_attr.IsValid()):
        return []
    # Resolve by prim path, not chunk name — names are scene-wide and can be
    # uniquified/renamed, so a name lookup can bind to the wrong chunk (same
    # rationale as load_chunks' dependency restore).
    chunk_by_path = {
        get_chunk_prim_path(c): c for c in chunk_registry.ChunkRegistry().scene.get_chunks()
    }
    deps = []
    for conn_path in exec_attr.GetConnections():
        if dep := chunk_by_path.get(str(conn_path.GetPrimPath())):
            deps.append(dep)
    return deps


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
    live = _live_exec_dependencies(chunk)
    current = [dep for _, dep in chunk.get_dependencies() if isinstance(dep, core.ChunkBase)]

    live_ids = {id(dep) for dep in live}
    current_ids = {id(dep) for dep in current}

    changed = False
    for dep in current:
        if id(dep) not in live_ids:
            changed = chunk.remove_dependency(dep) or changed
    for dep in live:
        if id(dep) not in current_ids:
            chunk.add_dependency(dep)
            changed = True
    return changed


def _author_prim(stage: Usd.Stage, chunk: "core.ChunkBase", prev_chunk: "core.ChunkBase | None" = None, index: int = 0) -> None:
    chunk_root = _resolve_chunk_root(chunk)
    stage.DefinePrim(Sdf.Path(chunk_root), _CHUNK_PRIM_TYPENAME)

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

    deps = [dep for _, dep in chunk.get_dependencies() if isinstance(dep, core.ChunkBase)]
    if not deps:
        deps = _live_exec_dependencies(chunk)
    if not deps and prev_chunk:
        deps = [prev_chunk]
    if deps:
        exec_in = prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        exec_in.SetConnections([Sdf.Path(exec_output_path(dep)) for dep in deps])

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
