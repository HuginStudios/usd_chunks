"""
Node package — re-exports all node classes and the registry for the tab menu.

Import from this package exactly as you would from the old nodes.py module.
"""
from PySide6 import QtCore
from usd_chunks import chunk_core, chunk_registry
from usd_chunks import chunk
from usd_chunks.chunk import Button

# ── Shared primitives (re-exported for graph_widget / graph_executor) ──────
from ._base import (
    UsdNodeBase as UsdNodeBase,
    ChunkWrapperBase as ChunkWrapperBase,
    DynamicChunkNode as DynamicChunkNode,
    PORT_COLORS as PORT_COLORS,
    CATEGORY_COLORS as CATEGORY_COLORS,
    TRIGGER as TRIGGER,
    _TriggerSignal as _TriggerSignal,
    _get_node_port_types as _get_node_port_types,
    _safe_set_port_visible as _safe_set_port_visible,
    _PrimThumbWidget as _PrimThumbWidget,
    _NodePreviewWidget as _NodePreviewWidget,
    _PrimThumbLabel as _PrimThumbLabel,
)

# ── Input ──────────────────────────────────────────────────────────────────
from ._input import PrimPathNode, SelectedPrimNode, StringNode, FloatNode, BoolNode

# ── USD query / inspection ─────────────────────────────────────────────────
from ._query import (
    GetVariantsNode, SetVariantNode, GetPrimInfoNode, GetChildrenNode,
    GetAttributeNode, SetAttributeNode, FilterChildrenNode, SelectPrimNode,
    SetVisibilityNode, SetPurposeNode, IsVisibleNode, IsPrimValidNode,
    HasVariantsNode, HasChildrenNode, HasMaterialNode, HasAttributeNode,
    HasReferenceNode, FindByTypeNode, FindByPatternNode, StageInfoNode,
    GetRelationshipsNode, GetMetadataNode, SetMetadataNode,
    GetPrimLayerStackNode, ListPrimsNode, PrintNode,
)

# ── Transform ──────────────────────────────────────────────────────────────
from ._transform import (
    SetTransformNode, GetWorldTransformNode, ResetTransformNode,
    GetBoundingBoxNode, ConstrainTransformNode,
)

# ── Hierarchy ──────────────────────────────────────────────────────────────
from ._hierarchy import (
    CreatePrimNode, DeletePrimNode, ReparentPrimNode, DuplicatePrimNode, RenamePrimNode,
)

# ── Composition ────────────────────────────────────────────────────────────
from ._composition import (
    AddReferenceNode, AddPayloadNode, GetLayerStackNode, MergeLayersNode, ReferenceAtNode,
)

# ── Create ─────────────────────────────────────────────────────────────────
from ._create import (
    CreateCubeNode, CreateSphereNode, CreateCylinderNode, CreateConeNode,
    CreateCapsuleNode, CreatePlaneNode, CreateXformNode, PointInstancerNode,
    MakeInstanceableNode,
)

# ── Lighting ───────────────────────────────────────────────────────────────
from ._lighting import (
    CreateRectLightNode, CreateSphereLightNode, CreateDistantLightNode,
    CreateDomeLightNode, CreateDiskLightNode,
)

# ── Camera ─────────────────────────────────────────────────────────────────
from ._camera import CreateCameraNode, SetActiveCameraNode

# ── Animation ──────────────────────────────────────────────────────────────
from ._animation import SetKeyNode, GetTimeRangeNode, SetTimeCodeNode

# ── Material / shading ─────────────────────────────────────────────────────
from ._material import (
    SetDisplayColorNode, AssignMaterialNode, CreatePreviewSurfaceNode,
    TextureFileNode, CreateUVTextureNode, CreatePrimvarReaderNode,
    GetMaterialShadersNode, GetShaderInputNode, SetShaderInputNode,
    ConnectShaderNode, SetUVTransformNode, GetPrimMaterialNode,
)

# ── Utility / logic ────────────────────────────────────────────────────────
from ._utility import (
    ListIndexNode, ListLengthNode, ForEachNode, MergeNode, CompareNode,
    SwitchNode, ExportUsdNode, CounterNode, StringFormatNode, StringSplitNode,
    StringJoinNode, StringReplaceNode, BoolAndNode, BoolOrNode, BoolNotNode,
    CoalesceNode,
)

# ── Math ───────────────────────────────────────────────────────────────────
from ._math import (
    MathAddNode, MathSubtractNode, MathMultiplyNode, MathDivideNode,
    MathClampNode, MathRemapNode,
)

# ── Code ───────────────────────────────────────────────────────────────────
from ._code import PythonNode

# ── Event / trigger ────────────────────────────────────────────────────────
from ._event import (
    _TriggerNodeBase as _TriggerNodeBase,
    OnSelectionChangedNode, OnFrameChangedNode, OnTimerNode,
    OnStageChangedNode, OnPrimChangedNode, OnAttributeChangedNode,
)

# ── Registry ───────────────────────────────────────────────────────────────

ALL_NODES = [
    # Input
    PrimPathNode, SelectedPrimNode, StringNode, FloatNode, BoolNode,
    # USD Query
    GetVariantsNode, SetVariantNode, GetPrimInfoNode, GetChildrenNode,
    GetAttributeNode, SetAttributeNode, FilterChildrenNode, SelectPrimNode,
    SetVisibilityNode, SetPurposeNode, IsVisibleNode, IsPrimValidNode,
    HasVariantsNode, HasChildrenNode, HasMaterialNode, HasAttributeNode,
    HasReferenceNode, FindByTypeNode, FindByPatternNode, StageInfoNode,
    GetRelationshipsNode, GetMetadataNode, SetMetadataNode,
    GetPrimLayerStackNode, ListPrimsNode,
    # Transform
    SetTransformNode, GetWorldTransformNode, ResetTransformNode,
    GetBoundingBoxNode, ConstrainTransformNode,
    # Hierarchy
    CreatePrimNode, DeletePrimNode, ReparentPrimNode, DuplicatePrimNode, RenamePrimNode,
    # Composition
    AddReferenceNode, AddPayloadNode, GetLayerStackNode, MergeLayersNode, ReferenceAtNode,
    # Create
    CreateCubeNode, CreateSphereNode, CreateCylinderNode, CreateConeNode,
    CreateCapsuleNode, CreatePlaneNode, CreateXformNode, PointInstancerNode,
    MakeInstanceableNode,
    # Lighting
    CreateRectLightNode, CreateSphereLightNode, CreateDistantLightNode,
    CreateDomeLightNode, CreateDiskLightNode,
    # Camera
    CreateCameraNode, SetActiveCameraNode,
    # Animation
    SetKeyNode, GetTimeRangeNode, SetTimeCodeNode,
    # Material
    SetDisplayColorNode, AssignMaterialNode, CreatePreviewSurfaceNode,
    TextureFileNode, CreateUVTextureNode, CreatePrimvarReaderNode,
    GetMaterialShadersNode, GetShaderInputNode, SetShaderInputNode,
    ConnectShaderNode, SetUVTransformNode, GetPrimMaterialNode,
    # Utility
    ListIndexNode, ListLengthNode, ForEachNode, MergeNode, CompareNode,
    SwitchNode, ExportUsdNode, CounterNode, StringFormatNode, StringSplitNode,
    StringJoinNode, StringReplaceNode, BoolAndNode, BoolOrNode, BoolNotNode,
    CoalesceNode,
    # Math
    MathAddNode, MathSubtractNode, MathMultiplyNode, MathDivideNode,
    MathClampNode, MathRemapNode,
    # Code
    PythonNode,
    # Chunks
    DynamicChunkNode,
    # Debug
    PrintNode,
    # Event
    OnSelectionChangedNode, OnFrameChangedNode, OnTimerNode,
    OnStageChangedNode, OnPrimChangedNode, OnAttributeChangedNode,
]

# Category → [node_class] mapping for the tab menu
NODE_CATEGORIES: dict = {}
for _n in ALL_NODES:
    _cat = getattr(_n, "CATEGORY", "Other")
    NODE_CATEGORIES.setdefault(_cat, []).append(_n)

# ── Chunk node auto-generation ─────────────────────────────────────────────

_SKIP_MODULES = {
    "node_graph_chunk", "usd_mcp_server", "kiro_chunk_web", "kiro_chunk",
    "render_preview", "ai_chat.chunk",
}


def _make_chunk_node(module_name, chunk_name):
    """Dynamically create a node class that wraps a chunk module."""

    class _AutoChunkNode(ChunkWrapperBase):
        __identifier__ = "usd_chunks"
        NODE_NAME = chunk_name
        CATEGORY = "Chunks"
        _chunk_module = module_name
        _chunk_display_name = chunk_name

        def _ensure_chunk(self):
            my_chunk = getattr(self, "_my_chunk", None)
            if my_chunk is not None:
                return my_chunk
            try:
                created = chunk.create_chunks(self._chunk_module)
            except Exception as e:
                print(f"[NodeGraph] ensure_chunk err: {e}")
                created = None
            if not created:
                return None
            my_chunk = created[0]
            self._my_chunk = my_chunk
            self._chunk_display_name = my_chunk.name
            try:
                self.set_name(my_chunk.name)
            except Exception:
                pass
            return my_chunk

        def __init__(self):
            super().__init__()
            self.add_output("prim", color=PORT_COLORS["prim"])
            self.add_output("layer", color=PORT_COLORS["layer"])
            _needs_prim = False
            try:
                reg = chunk_registry.ChunkRegistry()
                mod = None
                for cm in reg.get_chunk_modules():
                    if cm.module_name == self._chunk_module:
                        mod = cm.module
                        break
                if mod and hasattr(mod, "main"):
                    tmp = mod.main()
                    if tmp:
                        _F = chunk_core.ChunkFlags.ParamType
                        _TYPE_COLORS = {
                            _F.FLOAT: PORT_COLORS["float"],
                            _F.INT: PORT_COLORS["float"],
                            _F.STRING: PORT_COLORS["string"],
                            _F.BOOL: PORT_COLORS["bool"],
                        }

                        def _walk_params(params):
                            for p in params:
                                if isinstance(p, chunk_core.ChunkGroupBase):
                                    _walk_params(p.get_params())
                                elif isinstance(p, chunk_core.ChunkVectorBase):
                                    for sub in p:
                                        self.add_text_input(sub.name, sub.name,
                                                            text=str(sub.value if sub.value else 0))
                                        ap = self.get_input(f"_{sub.name}")
                                        if ap:
                                            ap.color = PORT_COLORS["float"]
                                        out_name = f"_{sub.name}"
                                        existing_out = {op.name() for op in self.output_ports()}
                                        if out_name not in existing_out:
                                            self.add_output(out_name, color=PORT_COLORS["float"])
                                elif isinstance(p, Button):
                                    pass
                                elif isinstance(p, chunk_core.ChunkParamBase):
                                    self._add_chunk_port(p, _TYPE_COLORS)
                                    if hasattr(p, 'has_flag') and p.has_flag(
                                            chunk_core.ChunkFlags.ParamFlags.PRIM_SELECTION):
                                        nonlocal _needs_prim
                                        _needs_prim = True
                        _walk_params(tmp.get_params())
            except Exception:
                pass
            if _needs_prim:
                self.add_input("prim", color=PORT_COLORS["prim"])
            for w in self.view._widgets.values():
                w.setVisible(False)
            self._folded = True
            self.view._folded_state = True

            def _fold_ports():
                try:
                    for port in self.input_ports():
                        if port.name().startswith("_"):
                            port.set_visible(False)
                    for port in self.output_ports():
                        if port.name().startswith("_"):
                            port.set_visible(False)
                    self.view.draw_node()
                except Exception:
                    pass
            QtCore.QTimer.singleShot(0, _fold_ports)

        def _add_chunk_port(self, p, type_colors):
            if isinstance(p, (Button, chunk_core.ChunkGroupBase, chunk_core.ChunkVectorBase)):
                return
            _RESERVED = {"width", "height", "name", "color", "icon", "id",
                         "selected", "disabled", "visible", "pos"}
            if p.name in _RESERVED:
                return
            try:
                self.add_text_input(p.name, p.name, text=str(p.value) if p.value else "")
                port_color = PORT_COLORS.get("any", (140, 150, 170))
                pt = p.evaluate_type()
                for base_type, color in type_colors.items():
                    if pt & base_type:
                        port_color = color
                        break
                ap = self.get_input(f"_{p.name}")
                if ap:
                    ap.color = port_color
                out_name = f"_{p.name}"
                existing_out = {op.name() for op in self.output_ports()}
                if out_name not in existing_out:
                    self.add_output(out_name, color=port_color)
            except Exception:
                pass

        def execute(self, stage):
            from ..chunk_adapter import ChunkAdapter
            prim = self._get_input_value("prim")

            my_chunk = self._ensure_chunk()
            if not hasattr(self, '_cached_adapter') or not self._cached_adapter.chunk:
                self._cached_adapter = ChunkAdapter(
                    self._chunk_display_name,
                    module_name=self._chunk_module,
                    chunk=my_chunk,
                )
            adapter = self._cached_adapter
            if not adapter.chunk:
                return {"prim": prim}

            self._subscribe_to_chunk_notices(adapter.chunk)

            self._executing = True
            try:
                old_sel = None
                if prim:
                    scene = chunk_registry.ChunkRegistry().scene
                    ctrl = scene.usdviewAPI._UsdviewApi__appController
                    old_sel = scene.get_selected_prims()
                    ctrl._dataModel.selection.setPrim(prim)

                def _push_params(params):
                    for p in params:
                        if isinstance(p, chunk_core.ChunkGroupBase):
                            try:
                                _push_params(p.get_params())
                            except Exception:
                                pass
                        elif isinstance(p, chunk_core.ChunkVectorBase):
                            for sub in p:
                                v = self._get_input_value(f"_{sub.name}")
                                if v is not None:
                                    adapter.set_param(sub.name, float(v))
                        elif isinstance(p, Button):
                            pass
                        elif hasattr(p, "value"):
                            v = self._get_input_value(f"_{p.name}")
                            if v is not None:
                                adapter.set_param(p.name, v)
                            else:
                                existing_ref = getattr(p, "value_reference", None)
                                prop_val = self.get_property(p.name)
                                if isinstance(prop_val, str) and prop_val.startswith("@"):
                                    adapter.set_param(p.name, prop_val)
                                elif existing_ref and not (
                                        isinstance(prop_val, str) and prop_val.startswith("@")):
                                    try:
                                        self.set_property(p.name, existing_ref)
                                    except Exception:
                                        pass

                try:
                    _push_params(adapter.chunk.get_params())
                except Exception:
                    _push_params(adapter.get_params())

                try:
                    from usd_chunks import chunk_notice as _cn
                    _ic_notice = _cn.get_notice(_cn.ChunkNoticeAlias.INPUT_CHANGED)()

                    def _fire_ref_params(container):
                        try:
                            my_params = container.get_my_params()
                        except Exception:
                            return
                        group_had_ref = False
                        for p in my_params:
                            if isinstance(p, chunk_core.ChunkGroupBase):
                                _fire_ref_params(p)
                            elif isinstance(p, chunk_core.ChunkVectorBase):
                                for sub in p:
                                    if getattr(sub, "value_reference", None):
                                        try:
                                            sub.data_changed(_ic_notice)
                                            group_had_ref = True
                                        except Exception:
                                            pass
                            else:
                                if getattr(p, "value_reference", None):
                                    try:
                                        p.data_changed(_ic_notice)
                                        group_had_ref = True
                                    except Exception:
                                        pass
                        if group_had_ref and hasattr(container, "send_self"):
                            try:
                                container.send_self()
                            except Exception:
                                pass

                    _fire_ref_params(adapter.chunk)
                except Exception as e:
                    print(f"[NodeGraph] ref refresh err: {e}")

                out_prim = prim
                try:
                    layer = adapter.chunk.get_edit_context()
                    if layer:
                        for prim_spec in layer.rootPrims:
                            resolved = stage.GetPrimAtPath(prim_spec.path)
                            if resolved and resolved.IsValid():
                                out_prim = resolved
                                break
                except Exception:
                    pass
                if out_prim is prim:
                    def _find_output_prim(params):
                        for p in params:
                            if isinstance(p, chunk_core.ChunkGroupBase):
                                r = _find_output_prim(p.get_params())
                                if r:
                                    return r
                                continue
                            if not hasattr(p, 'value') or not p.value:
                                continue
                            sval = str(p.value)
                            if sval.startswith("/") and not sval.startswith("//"):
                                resolved = stage.GetPrimAtPath(sval)
                                if resolved and resolved.IsValid():
                                    return resolved
                        return None
                    found = _find_output_prim(adapter.chunk.get_params())
                    if found:
                        out_prim = found

                if prim and old_sel:
                    ctrl._dataModel.selection.setPrim(old_sel[0])

                out_layer = None
                try:
                    out_layer = adapter.chunk.get_edit_context()
                except Exception:
                    pass

                result = {"prim": out_prim, "layer": out_layer}
                try:
                    def _export_params(params):
                        for p in params:
                            if isinstance(p, chunk_core.ChunkGroupBase):
                                _export_params(p.get_params())
                            elif isinstance(p, chunk_core.ChunkVectorBase):
                                for sub in p:
                                    if hasattr(sub, 'value'):
                                        result[f"_{sub.name}"] = sub.value
                            elif hasattr(p, 'name') and hasattr(p, 'value'):
                                result[f"_{p.name}"] = p.value
                    _export_params(adapter.chunk.get_params())
                except Exception:
                    pass
                return result
            finally:
                self._executing = False

    _AutoChunkNode.__name__ = f"Auto_{module_name.replace('.', '_')}"
    _AutoChunkNode.__qualname__ = _AutoChunkNode.__name__
    return _AutoChunkNode


def discover_chunk_nodes():
    """Scan loaded chunk modules and generate node classes."""
    discovered = []
    try:
        reg = chunk_registry.ChunkRegistry()
        if not reg.initialized:
            return discovered
        for cm in reg.get_chunk_modules():
            if cm.module_name in _SKIP_MODULES:
                continue
            if not cm.module or not hasattr(cm.module, "main"):
                continue
            if any(cm.module_name in getattr(n, '_chunk_module', '') for n in ALL_NODES):
                continue
            try:
                scene = reg.scene
                chunk_name = None
                if scene:
                    for ch in scene.get_chunks():
                        if cm.module_name in str(getattr(ch, '_module_name', '')):
                            chunk_name = ch.name
                            break
                if not chunk_name:
                    chunk_name = cm.module_name.split('.')[-1].replace('_', ' ').title()
                node_cls = _make_chunk_node(cm.module_name, chunk_name)
                discovered.append(node_cls)
            except Exception:
                pass
    except Exception:
        pass
    return discovered
