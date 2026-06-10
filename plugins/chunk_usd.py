"""
USD I/O plugin for chunks.
Handles layer management, stage synchronization, and chunk serialization.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Generator, Optional, Tuple

from pxr import Gf, Sdf, Usd, UsdUI, Vt

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

_SDF_TYPE_MAP = {
    bool: Sdf.ValueTypeNames.Bool,
    int: Sdf.ValueTypeNames.Int,
    float: Sdf.ValueTypeNames.Float,
    str: Sdf.ValueTypeNames.String,
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


def _sdf_type_and_value(val):
    """Return (Sdf value type, USD-safe value) for val, or (None, None) if unsupported."""
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
        self._attr_changed_listener = NoticeConnection(
            self._on_attr_changed, ChunkNoticeAlias.ATTRIBUTE_CHANGED, globally=True
        )
        self._param_added_listener = NoticeConnection(
            self._on_param_added, ChunkNoticeAlias.PARAM_ADDED, globally=True
        )
        self._dependency_added_listener = NoticeConnection(
            self._on_dependency_added, ChunkNoticeAlias.DEPENDENCY_ADDED, globally=True
        )
        # StageEditTargetChanged fires on every Usd.EditContext enter/exit (used in
        # handle_callback for every chunk callback), so it is not a reliable signal for
        # detecting a new file being loaded.  Keep disabled until a better mechanism exists.
        # self._edit_target_listener = NoticeConnection(
        #     self._on_edit_target_changed, ChunkNoticeAlias.STAGE_EDIT_TARGET_CHANGED, globally=True
        # )
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
        
        if stage:
            self._loaded_stage = stage
            load_chunks(stage, scene, self._loaded_prim_paths)
        self.initialized = True

    def _on_contents_changed(self, notice, sender):
        if not self._scene:
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
        else:
            chunk_root = _resolve_chunk_root(sender)
            sdf_path = Sdf.Path(f"{chunk_root}/{sender.name}")
        for layer in self._stage.GetLayerStack(includeSessionLayers=True):
            parent = layer.GetPrimAtPath(sdf_path.GetParentPath())
            if parent and sdf_path.name in parent.nameChildren:
                del parent.nameChildren[sdf_path.name]

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
        if not changed:
            return
        for chunk in self._scene.get_chunks():
            for _, param in _iter_chunk_params(chunk):
                data_path = getattr(param, "_param_attr_path", None)
                if not data_path or data_path not in changed:
                    continue
                attr = sender.GetAttributeAtPath(Sdf.Path(data_path))
                if attr and attr.IsValid():
                    val = attr.Get(0)
                    if val is not None and val != param.value:
                        param.value = val

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
            new_path = f"{_resolve_chunk_root(owner)}/{owner.name}"
            self._handle_rename(
                old_path=old_path,
                new_path=new_path,
                old_name=old_name,
                new_name=owner.name,
            )
            self._chunk_prim_paths[id(owner)] = new_path
        elif isinstance(owner, core.ChunkParamBase):
            self._handle_param_rename(owner, old_name, owner.name)

    def _handle_rename(self, old_path: str, new_path: str, old_name: str, new_name: str) -> None:
        """Rename a chunk prim and patch all references to it."""
        layer = self._stage.GetEditTarget().GetLayer()
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(Sdf.Path(old_path), Sdf.Path(new_path))
        if not layer.Apply(edit):
            logger.warning("Failed to rename prim %s → %s", old_path, new_path)
            return
        _patch_expressions(self._stage, f"@{old_name}.", f"@{new_name}.")
        self._loaded_prim_paths.discard(old_path)
        self._loaded_prim_paths.add(new_path)

    def _handle_param_rename(self, param: "core.ChunkParamBase", old_name: str, new_name: str) -> None:
        """Rename a param's USD attribute and patch expression strings."""
        chunk = next(
            (c for c in self._scene.get_chunks() if param in list(_iter_chunk_params(c))[1::2]),
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
        layer = self._stage.GetEditTarget().GetLayer()
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(Sdf.Path(f"{prim_path}.{old_attr}"), Sdf.Path(f"{prim_path}.{new_attr}"))
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
        exec_in = dep_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        existing = list(exec_in.GetConnections())
        if src_path not in existing:
            exec_in.SetConnections(existing + [src_path])

    def _on_param_added(self, notice, sender):
        if not self.initialized or not self._stage:
            return
        chunk = notice.data
        if not isinstance(chunk, core.ChunkBase):
            return
        is_group = chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT)
        if is_group:
            # For CONTEXT group chunks, prim lives at parent_root/name, not under
            # _resolve_chunk_root (which would give prim_path/chunks — the sub-root).
            parent_ctx = getattr(chunk, "_parent_context", None)
            parent_ctx = parent_ctx() if parent_ctx else None
            if parent_ctx and getattr(parent_ctx, "prim_path", None):
                parent_root = f"{parent_ctx.prim_path}/chunks"
            else:
                parent_root = _CHUNKS_ROOT
            prim_path = f"{parent_root}/{chunk.name}"
        else:
            prim_path = f"{_resolve_chunk_root(chunk)}/{chunk.name}"
        # If the path is already tracked, this chunk is being loaded from USD
        # (load_chunks pre-registers paths before add_chunk). Skip _define_prim —
        # the prim already exists in the sublayer; load_chunks handles _wire_inputs.
        if prim_path in self._loaded_prim_paths:
            return
        self._loaded_prim_paths.add(prim_path)
        self._chunk_prim_paths[id(chunk)] = prim_path

        if is_group:
            pc = getattr(chunk, "_parent_context", None)
            tail_context = pc() if pc else None
        else:
            tail_context = chunk.get_context()
        prev = self._find_dag_tail(tail_context, exclude=chunk)
        prim = self._define_prim(chunk, prev)
        self._wire_inputs(prim, chunk)
        # Set prim_path on group contexts so sub-chunks nest correctly under them.
        if is_group:
            chunk.prim_path = prim_path

    def _find_dag_tail(self, context, exclude=None) -> "core.ChunkBase | None":
        """Return the tail of the exec DAG for this context — the chunk whose outputs:chunk
        is not referenced by any other chunk's inputs:chunk. Used as prev_chunk when no
        interactive history exists (e.g. first add after loading a .usda)."""
        # Regular chunks in this context.
        context_chunks = [
            c for c in self._scene.get_chunks()
            if c.get_context() == context and c is not exclude
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
        chunk_root = _resolve_chunk_root(chunk)
        self._stage.DefinePrim(Sdf.Path(chunk_root), _CHUNK_PRIM_TYPENAME)

        root_prim = self._stage.GetPrimAtPath(Sdf.Path(chunk_root))
        index = sum(
            1 for child in (root_prim.GetChildren() if root_prim.IsValid() else [])
            if child.GetTypeName() == _CHUNK_PRIM_TYPENAME
        )

        prim = self._stage.DefinePrim(Sdf.Path(f"{chunk_root}/{chunk.name}"), _CHUNK_PRIM_TYPENAME)

        ui_api = UsdUI.NodeGraphNodeAPI.Apply(prim)
        if not ui_api.GetPosAttr().HasAuthoredValue():
            ui_api.CreatePosAttr(Gf.Vec2f(index * _NODE_STEP, 0.0))

        config = chunk.get_config()
        if config and config.module:
            attr = prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, custom=False)
            attr.SetVariability(Sdf.VariabilityUniform)
            if not attr.Get():
                attr.Set(config.module)

        exec_out = prim.CreateAttribute(_EXEC_OUTPUT, Sdf.ValueTypeNames.Token, custom=False)
        exec_out.SetVariability(Sdf.VariabilityUniform)

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
            sdf_type = _SDF_TYPE_MAP.get(type(raw))
            if sdf_type is None:
                continue
            attr_name = f"{_INPUTS_PREFIX}{path.replace('.', ':')}"
            attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
            if not attr.Get():
                attr.Set(raw)
            if isinstance(raw, str) and chunk_utils.has_expression(raw):
                attr.SetConnections([
                    Sdf.Path(f"{chunk_root}/{node}.inputs:{attr_name_ref}")
                    for node, attr_name_ref in chunk_utils.parse_expression(raw)
                ])

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
    if context and getattr(context, "prim_path", None):
        return f"{context.prim_path}/chunks"
    return _CHUNKS_ROOT


def _iter_chunk_params(
    obj: "core.ChunkCommon", prefix: str = ""
) -> Generator[Tuple[str, "core.ChunkParamBase"], None, None]:
    for param in obj.get_my_params():
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkParamBase):
            yield path, param
        elif isinstance(param, core.ChunkListGroupBase):
            pass  # handled as arrays by _iter_list_group_arrays
        else:
            yield from _iter_chunk_params(param, path)


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
                        scene.add_chunk(c)
                        if c.get_config() and c.get_config().module:
                            group_chunk_by_path[str(prim.GetPath())] = c
                    elif ancestor_group is not None:
                        # Sub-chunk of a ChunkGroup: add directly to that group.
                        ancestor_group.add_param(c, notify=False)
                        _load_wire_inputs(prim, c)
                        deferred.append((prim, c))
                    else:
                        if added := scene.add_chunk(c, notify=False):
                            _load_wire_inputs(prim, added)
                            deferred.append((prim, added))

            # Restore DAG dependencies from inputs:chunk connections so execution
            # order is driven by the explicit graph, not just prim appearance order.
            chunk_by_name = {c.name: c for _, c in deferred}
            for prim, chunk in deferred:
                exec_attr = prim.GetAttribute(_EXEC_INPUT)
                if not (exec_attr and exec_attr.IsValid()):
                    continue
                for conn_path in exec_attr.GetConnections():
                    dep_name = str(conn_path.GetPrimPath()).rsplit("/", 1)[-1]
                    if dep := chunk_by_name.get(dep_name):
                        chunk.add_dependency(dep)

            _restore_lofted_params(stage, deferred)

            # Fire CHUNK_LOADED only for terminal nodes (no DAG dependents) so that
            # execution propagates from the end of the chain rather than every node.
            for _, added in deferred:
                if not added.get_dependents():
                    lm = ChunkLayerManager()
                    if lm.initialized:
                        if layer := lm.get_or_create_layer(added):
                            layer.Clear()
                    chunk_notice.send_notice(
                        chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, added, data=added
                    )
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
    layer = Sdf.Layer.CreateAnonymous()
    prev_chunk = None
    for index, chunk in enumerate(scene.get_chunks()):
        _author_prim(layer, chunk, prev_chunk, index)
        prev_chunk = chunk
    layer.Export(str(path))


def rename_context_prim_path(context: "core.ChunkBase", new_prim_path: "str | None") -> None:
    """Move all chunk prims in context to a new root using Sdf.CopySpec, then remove the old specs."""
    sync = ChunkStageSync()
    if not sync.initialized or not sync._stage:
        context.prim_path = new_prim_path
        return

    old_root = f"{context.prim_path}/chunks" if context.prim_path else _CHUNKS_ROOT
    new_root = f"{new_prim_path}/chunks" if new_prim_path else _CHUNKS_ROOT

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
        sync._loaded_prim_paths.discard(f"{old_root}/{chunk_obj.name}")
        sync._loaded_prim_paths.add(f"{new_root}/{chunk_obj.name}")

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
        logger.warning("loft_param_to_context: param %r has no USD attribute bound", param.name)
        return

    inner_attr = stage.GetAttributeAtPath(Sdf.Path(param._param_attr_path))
    if not (inner_attr and inner_attr.IsValid()):
        logger.warning("loft_param_to_context: attribute not found at %s", param._param_attr_path)
        return

    attr_name = f"{_INPUTS_PREFIX}{param.name}"
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
            context.add_param(context_param)
            context_param._connect_param_attr(context_attr)

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


def _author_prim(layer: Sdf.Layer, chunk: "core.ChunkBase", prev_chunk: "core.ChunkBase | None" = None, index: int = 0) -> None:
    chunk_root = _resolve_chunk_root(chunk)
    root_sdf_path = Sdf.Path(chunk_root)
    if not layer.GetPrimAtPath(root_sdf_path):
        root_spec = Sdf.CreatePrimInLayer(layer, root_sdf_path)
        root_spec.typeName = _CHUNK_PRIM_TYPENAME
        root_spec.specifier = Sdf.SpecifierDef

    prim_path = Sdf.Path(f"{chunk_root}/{chunk.name}")
    prim_spec = Sdf.CreatePrimInLayer(layer, prim_path)
    prim_spec.typeName = _CHUNK_PRIM_TYPENAME
    prim_spec.specifier = Sdf.SpecifierDef

    schemas = Sdf.TokenListOp()
    schemas.prependedItems = ["NodeGraphNodeAPI"]
    prim_spec.SetInfo("apiSchemas", schemas)
    if "ui:nodegraph:node:pos" not in prim_spec.attributes:
        pos_spec = Sdf.AttributeSpec(prim_spec, "ui:nodegraph:node:pos", Sdf.ValueTypeNames.Float2)
        pos_spec.default = Gf.Vec2f(index * _NODE_STEP, 0.0)

    config = chunk.get_config()
    if config and config.module:
        attr_spec = Sdf.AttributeSpec(prim_spec, _CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token)
        attr_spec.default = config.module
        attr_spec.variability = Sdf.VariabilityUniform

    exec_out_spec = Sdf.AttributeSpec(prim_spec, _EXEC_OUTPUT, Sdf.ValueTypeNames.Token)
    exec_out_spec.variability = Sdf.VariabilityUniform

    deps = [dep for _, dep in chunk.get_dependencies() if isinstance(dep, core.ChunkBase)]
    if not deps and prev_chunk:
        deps = [prev_chunk]
    if deps:
        exec_in_spec = Sdf.AttributeSpec(prim_spec, _EXEC_INPUT, Sdf.ValueTypeNames.Token)
        exec_in_spec.connectionPathList.explicitItems = [
            Sdf.Path(f"{_resolve_chunk_root(dep)}/{dep.name}.{_EXEC_OUTPUT}") for dep in deps
        ]

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
        sdf_type, usd_value = _sdf_type_and_value(store_value)
        if sdf_type is None:
            continue

        attr_spec_name = f"{_INPUTS_PREFIX}{path.replace('.', ':')}"
        ctx_param = lofted.get(param.name)

        if ctx_param:
            # Param is lofted: write a connection on the inner attr and author the context prim's attr
            ctx_attr_path_str = ctx_param._param_attr_path  # e.g. "/chunks.inputs:mySlider"
            ctx_prim_str, ctx_attr_name = ctx_attr_path_str.rsplit(".", 1)
            ctx_prim_sdf = Sdf.Path(ctx_prim_str)

            if not layer.GetPrimAtPath(ctx_prim_sdf):
                ctx_prim_spec = Sdf.CreatePrimInLayer(layer, ctx_prim_sdf)
                ctx_prim_spec.typeName = _CHUNK_PRIM_TYPENAME
                ctx_prim_spec.specifier = Sdf.SpecifierDef
            ctx_prim_spec = layer.GetPrimAtPath(ctx_prim_sdf)

            if ctx_attr_name not in ctx_prim_spec.attributes:
                ctx_attr_spec = Sdf.AttributeSpec(ctx_prim_spec, ctx_attr_name, sdf_type)
                val = ctx_param.value if ctx_param.value is not None else param.value
                ctx_attr_spec.default = val

            attr_spec = Sdf.AttributeSpec(prim_spec, attr_spec_name, sdf_type)
            attr_spec.default = ctx_param.value if ctx_param.value is not None else usd_value
            attr_spec.connectionPathList.explicitItems = [Sdf.Path(ctx_attr_path_str)]
        else:
            attr_spec = Sdf.AttributeSpec(prim_spec, attr_spec_name, sdf_type)
            attr_spec.default = usd_value
            if isinstance(store_value, str) and chunk_utils.has_expression(store_value):
                conn_paths = [
                    Sdf.Path(f"{chunk_root}/{node_name}.inputs:{param_name}")
                    for node_name, param_name in chunk_utils.parse_expression(store_value)
                ]
                attr_spec.connectionPathList.explicitItems = conn_paths

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
            attr_spec_name = f"{_INPUTS_PREFIX}{lg_path.replace('.', ':')}:{proxy_param.name}"
            attr_spec = Sdf.AttributeSpec(prim_spec, attr_spec_name, sdf_type)
            attr_spec.default = usd_value
