"""
USD I/O plugin for chunks.
Handles layer management, stage synchronization, and chunk serialization.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
from pathlib import Path
from typing import Generator, Optional, Tuple

from pxr import Gf, Sdf, Usd, UsdUI, Vt

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

    def initialize(self, stage: Usd.Stage, scene: "core.SceneSender") -> None:
        self._stage = stage
        self._scene = scene
        self._loaded_stage = None
        self._loaded_prim_paths: set[str] = set()
        self._chunk_prim_paths: dict[int, str] = {}  # id(chunk) → current prim path
        # Track the user's intended edit target separately from temporary anonymous-layer
        # contexts that chunks set during execute(). Anonymous layers are never a valid
        # target for persistent chunk-prim inputs: attrs.
        self._user_edit_layer = stage.GetEditTarget().GetLayer() if stage else None
        self._attr_changed_listener = NoticeConnection(
            self._on_attr_changed, ChunkNoticeAlias.ATTRIBUTE_CHANGED, globally=True
        )
        self._param_added_listener = NoticeConnection(
            self._on_param_added, ChunkNoticeAlias.PARAM_ADDED, globally=True
        )
        self._dependency_added_listener = NoticeConnection(
            self._on_dependency_added, ChunkNoticeAlias.DEPENDENCY_ADDED, globally=True
        )
        # Listen for edit-target changes but only update when the new layer is not
        # anonymous (chunk callbacks temporarily set anonymous layers during execute()).
        self._edit_target_listener = NoticeConnection(
            self._on_edit_target_changed, ChunkNoticeAlias.STAGE_EDIT_TARGET_CHANGED, globally=True
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

        if stage:
            self._loaded_stage = stage
            load_chunks(stage, scene, self._loaded_prim_paths)
        self.initialized = True

    def _on_edit_target_changed(self, _notice, _sender):
        if not self._stage:
            return
        layer = self._stage.GetEditTarget().GetLayer()
        if layer and not layer.anonymous:
            self._user_edit_layer = layer

    def _chunk_prim_edit_context(self):
        """Return an EditContext that targets the user's persistent edit layer."""
        if not self.initialized:
            return contextlib.nullcontext()
        if self._user_edit_layer and self._stage:
            return Usd.EditContext(self._stage, self._user_edit_layer)
        return contextlib.nullcontext()

    def clear_attr_connections(self, attr: Usd.Attribute) -> None:
        """Clear connections from a chunk prim attr in the correct edit layer."""
        with self._chunk_prim_edit_context():
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
        if self._user_edit_layer:
            spec = self._user_edit_layer.GetAttributeAtPath(path)
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
        if chunk_notice._usd_write_active or not self._scene:
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

    def reload_for_stage(self, stage: Usd.Stage) -> None:
        """Reinitialize for a newly opened stage (File > Open).  Safe to call from a
        deferred context (e.g. QTimer.singleShot) once the stage has fully settled."""
        if not stage or not self._scene:
            return
        loaded_root = self._loaded_stage.GetRootLayer().identifier if self._loaded_stage else None
        if stage.GetRootLayer().identifier == loaded_root:
            return
        self._loaded_stage = stage
        self._stage = stage
        chunk_registry.ChunkLayerManager().initialize(stage)
        for ctx in list(self._scene.chunks.get_my_params()):
            ctx.request_delete()
        self._loaded_prim_paths.clear()
        load_chunks(stage, self._scene, self._loaded_prim_paths)

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

        Renames the prim in every layer that actually authors it. _user_edit_layer can be
        None (the edit target is an anonymous session/graph layer, which _on_edit_target_changed
        deliberately doesn't record) — chunks still author fine because _chunk_prim_edit_context
        falls back to the current edit target, so the rename must use the same fallback rather
        than bail (bailing left the chunk renamed but the prim not, so the node vanished).
        """
        if not self._stage:
            return
        old_prim = self._stage.GetPrimAtPath(Sdf.Path(old_path))
        target_layers = []
        if old_prim and old_prim.IsValid():
            for spec in old_prim.GetPrimStack():
                if spec.layer not in target_layers:
                    target_layers.append(spec.layer)
        # Fallbacks if the prim couldn't be resolved: the tracked edit layer, else the target.
        if not target_layers:
            fallback = self._user_edit_layer or self._stage.GetEditTarget().GetLayer()
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
        if not self._user_edit_layer:
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
        self._user_edit_layer.Apply(edit)
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
            attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
            attr.Set(usd_value)
        finally:
            chunk_notice._usd_write_active = False

    def _on_edit_target_changed(self, notice, sender):
        pass  # Kept for compatibility; listener is disabled — see initialize()

    def _on_dependency_added(self, notice, sender):
        if not self.initialized or not self._stage:
            return
        dependent = sender
        dependency = notice.data
        if not isinstance(dependent, core.ChunkBase) or not isinstance(dependency, core.ChunkBase):
            return
        dep_prim = self._stage.GetPrimAtPath(Sdf.Path(f"{_resolve_chunk_root(dependent)}/{dependent.name}"))
        if not dep_prim.IsValid():
            return
        src_path = Sdf.Path(f"{_resolve_chunk_root(dependency)}/{dependency.name}.{_EXEC_OUTPUT}")
        with self._chunk_prim_edit_context():
            exec_in = dep_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
            existing = list(exec_in.GetConnections())
            if src_path not in existing:
                exec_in.SetConnections(existing + [src_path])

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
            with self._chunk_prim_edit_context():
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                attr.Set(current_val)
        else:
            # Unscoped (ambient edit target), matching _sync_param_input's equivalent
            # branch for scalars — must land in the same layer as ChunkVectorBase's own
            # later data_changed writes, or connections split across two layers and
            # clearing (ChunkStageSync.clear_attr_connections, which does target the
            # chunk layer) only removes the wrong one, leaving a stale composed opinion.
            attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
            if is_expr:
                attr.SetConnections(self._expr_connections(prim, raw))
            else:
                attr.Set(vector.value)

        vector._connect_param_attr(attr)

        if real_attr_path and real_attr_path != str(attr.GetPath()):
            if old_attr := self._stage.GetAttributeAtPath(Sdf.Path(real_attr_path)):
                if old_attr.IsValid():
                    with self._chunk_prim_edit_context():
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
            layer = self._user_edit_layer or self._stage.GetSessionLayer()
            prim_spec = layer.GetPrimAtPath(prim.GetPath())
            if prim_spec and attr_name in prim_spec.properties:
                del prim_spec.properties[attr_name]
            saved = prim.GetAttribute(attr_name)
        if saved.IsValid() and saved.Get() is not None:
            attr = saved
        else:
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
            prefix = "outputs:" if param.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
            attr_name = f"{prefix}{path.replace('.', ':')}"

            # If the prim already has a saved value (from a prior save/load cycle),
            # restore the param from it rather than overwriting with the current default.
            saved = prim.GetAttribute(attr_name)
            if saved.IsValid() and saved.GetTypeName() != sdf_type:
                layer = self._user_edit_layer or self._stage.GetSessionLayer()
                prim_spec = layer.GetPrimAtPath(prim.GetPath())
                if prim_spec and attr_name in prim_spec.properties:
                    del prim_spec.properties[attr_name]
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
                with self._chunk_prim_edit_context():
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
                with self._chunk_prim_edit_context():
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

        # All prim and attribute authoring must land on the user's chosen edit
        # target, never on a chunk's own anonymous execute layer (which gets
        # cleared on every execute run and is not saved to disk).
        with self._chunk_prim_edit_context():
            self._stage.DefinePrim(Sdf.Path(chunk_root), _CHUNK_PRIM_TYPENAME)

        root_prim = self._stage.GetPrimAtPath(Sdf.Path(chunk_root))
        index = sum(
            1 for child in (root_prim.GetChildren() if root_prim.IsValid() else [])
            if child.GetTypeName() == _CHUNK_PRIM_TYPENAME
        )

        with self._chunk_prim_edit_context():
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
    """Replace all occurrences of old with new in string attributes across the stage."""
    for prim in stage.TraverseAll():
        for attr in prim.GetAttributes():
            val = attr.Get()
            if isinstance(val, str) and old in val:
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
    """Persist chunk's pinned state as a custom ``chunk:pinned`` attribute on its prim."""
    stage = chunk.get_stage()
    if not stage:
        return
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not prim.IsValid():
        return
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
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
    for c in scene.get_chunks():
        if is_view_chunk(c) or not in_context(c):
            continue
        if is_group_chunk(c):
            if c in cook_set:
                recook_for_view(stage, scene, c, find_group_active_view(c, stage, scene))
            else:
                recook_for_view(stage, scene, c, None)
            continue
        if c in cook_set:
            c.set_blocked(False)
            if not c.has_state(disabled):
                mute(lm.get_layer(c), False)
        else:
            c.set_blocked(True)
            mute(lm.get_layer(c), True)

    for c in cook_list:
        if not c.has_state(disabled) and not is_group_chunk(c):
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
    for prim in stage.Traverse():
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
            continue
        if loaded_prim_paths is not None and str(prim.GetPath()) in loaded_prim_paths:
            continue
        chunk_type = prim.GetAttribute(_CHUNK_TYPE_ATTR).Get()
        if not chunk_type:
            continue
        mod = chunk_registry.ChunkRegistry().get_chunk_module(str(chunk_type))
        if not mod:
            logger.warning("load_chunks: no module for type %r", chunk_type)
            continue
        to_load.append((prim, mod))

    if not to_load:
        return

    # Save before pre-registration so initial-load detection is accurate
    is_initial_load = not loaded_prim_paths

    # Pre-register all prim paths before processing to prevent StageContentsChanged
    # callbacks (from add_layer subLayerPaths inserts) from triggering recursive loads
    # that find unregistered paths and create duplicate contexts.
    if loaded_prim_paths is not None:
        for prim, _ in to_load:
            loaded_prim_paths.add(str(prim.GetPath()))

    # Group prims by their source layer so each sublayer file gets its own context
    groups = {}
    for prim, mod in to_load:
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
            group_chunk_by_path: dict[str, "core.ChunkBase"] = {}
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
            chunk_by_name = {c.name: c for _, c in deferred}
            chunks_with_dependencies: set[int] = set()
            for prim, chunk in deferred:
                exec_attr = prim.GetAttribute(_EXEC_INPUT)
                if not (exec_attr and exec_attr.IsValid()):
                    continue
                for conn_path in exec_attr.GetConnections():
                    dep_name = str(conn_path.GetPrimPath()).rsplit("/", 1)[-1]
                    if dep := chunk_by_name.get(dep_name):
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
                for g_layer in lm.graph_layers.values():
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
    stage = Usd.Stage.CreateInMemory()
    prev_chunk = None
    for index, chunk in enumerate(scene.get_chunks()):
        _author_prim(stage, chunk, prev_chunk, index)
        prev_chunk = chunk
    stage.GetRootLayer().Export(str(path))


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
