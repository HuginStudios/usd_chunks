"""
Node package — re-exports all node classes and the registry for the tab menu.

Import from this package exactly as you would from the old nodes.py module.
"""

from usd_chunks import chunk_registry

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
    _NodePreviewWidget as _NodePreviewWidget,
)

# ── Input ──────────────────────────────────────────────────────────────────
from ._input import PrimPathNode, SelectedPrimNode, StringNode, FloatNode, BoolNode

# ── USD query / inspection ─────────────────────────────────────────────────
from ._query import (
    GetVariantsNode,
    SetVariantNode,
    GetPrimInfoNode,
    GetChildrenNode,
    GetAttributeNode,
    SetAttributeNode,
    FilterChildrenNode,
    SelectPrimNode,
    SetVisibilityNode,
    SetPurposeNode,
    IsVisibleNode,
    IsPrimValidNode,
    HasVariantsNode,
    HasChildrenNode,
    HasMaterialNode,
    HasAttributeNode,
    HasReferenceNode,
    FindByTypeNode,
    FindByPatternNode,
    StageInfoNode,
    GetRelationshipsNode,
    GetMetadataNode,
    SetMetadataNode,
    GetPrimLayerStackNode,
    ListPrimsNode,
    PrintNode,
)

# ── Transform ──────────────────────────────────────────────────────────────
from ._transform import (
    SetTransformNode,
    OffsetTransformNode,
    GetWorldTransformNode,
    ResetTransformNode,
    GetBoundingBoxNode,
    ConstrainTransformNode,
)

# ── Hierarchy ──────────────────────────────────────────────────────────────
from ._hierarchy import (
    CreatePrimNode,
    DeletePrimNode,
    ReparentPrimNode,
    DuplicatePrimNode,
    RenamePrimNode,
)

# ── Composition ────────────────────────────────────────────────────────────
from ._composition import (
    AddReferenceNode,
    AddPayloadNode,
    GetLayerStackNode,
    MergeLayersNode,
    ReferenceAtNode,
)

# ── Create ─────────────────────────────────────────────────────────────────
from ._create import (
    CreateCubeNode,
    CreateSphereNode,
    CreateCylinderNode,
    CreateConeNode,
    CreateCapsuleNode,
    CreatePlaneNode,
    CreateXformNode,
    PointInstancerNode,
    MakeInstanceableNode,
    ScatterPointsNode,
)

# ── Simulation ─────────────────────────────────────────────────────────────
from ._simulation import (
    RBDSolverNode,
    CollisionSetNode,
    WindForceNode,
    DragForceNode,
    TurbulenceForceNode,
)

# ── Lighting ───────────────────────────────────────────────────────────────
from ._lighting import (
    CreateRectLightNode,
    CreateSphereLightNode,
    CreateDistantLightNode,
    CreateDomeLightNode,
    CreateDiskLightNode,
)

# ── Camera ─────────────────────────────────────────────────────────────────
from ._camera import CreateCameraNode, SetActiveCameraNode

# ── Animation ──────────────────────────────────────────────────────────────
from ._animation import SetKeyNode, GetTimeRangeNode, SetTimeCodeNode

# ── Material / shading ─────────────────────────────────────────────────────
from ._material import (
    SetDisplayColorNode,
    AssignMaterialNode,
    CreatePreviewSurfaceNode,
    TextureFileNode,
    CreateUVTextureNode,
    CreatePrimvarReaderNode,
    GetMaterialShadersNode,
    GetShaderInputNode,
    SetShaderInputNode,
    ConnectShaderNode,
    SetUVTransformNode,
    GetPrimMaterialNode,
)

# ── Utility / logic ────────────────────────────────────────────────────────
from ._utility import (
    ListIndexNode,
    ListLengthNode,
    ForEachNode,
    MergeNode,
    CompareNode,
    SwitchNode,
    ExportUsdNode,
    CounterNode,
    StringFormatNode,
    StringSplitNode,
    StringJoinNode,
    StringReplaceNode,
    BoolAndNode,
    BoolOrNode,
    BoolNotNode,
    CoalesceNode,
)

# ── Math ───────────────────────────────────────────────────────────────────
from ._math import (
    MathAddNode,
    MathSubtractNode,
    MathMultiplyNode,
    MathDivideNode,
    MathClampNode,
    MathRemapNode,
)

# ── Code ───────────────────────────────────────────────────────────────────
from ._code import PythonNode

# ── Event / trigger ────────────────────────────────────────────────────────
from ._event import (
    _TriggerNodeBase as _TriggerNodeBase,
    OnSelectionChangedNode,
    OnFrameChangedNode,
    OnTimerNode,
    OnStageChangedNode,
    OnPrimChangedNode,
    OnAttributeChangedNode,
)

# ── Registry ───────────────────────────────────────────────────────────────

ALL_NODES = [
    # Input
    PrimPathNode,
    SelectedPrimNode,
    StringNode,
    FloatNode,
    BoolNode,
    # USD Query
    GetVariantsNode,
    SetVariantNode,
    GetPrimInfoNode,
    GetChildrenNode,
    GetAttributeNode,
    SetAttributeNode,
    FilterChildrenNode,
    SelectPrimNode,
    SetVisibilityNode,
    SetPurposeNode,
    IsVisibleNode,
    IsPrimValidNode,
    HasVariantsNode,
    HasChildrenNode,
    HasMaterialNode,
    HasAttributeNode,
    HasReferenceNode,
    FindByTypeNode,
    FindByPatternNode,
    StageInfoNode,
    GetRelationshipsNode,
    GetMetadataNode,
    SetMetadataNode,
    GetPrimLayerStackNode,
    ListPrimsNode,
    # Transform
    SetTransformNode,
    OffsetTransformNode,
    GetWorldTransformNode,
    ResetTransformNode,
    GetBoundingBoxNode,
    ConstrainTransformNode,
    # Hierarchy
    CreatePrimNode,
    DeletePrimNode,
    ReparentPrimNode,
    DuplicatePrimNode,
    RenamePrimNode,
    # Composition
    AddReferenceNode,
    AddPayloadNode,
    GetLayerStackNode,
    MergeLayersNode,
    ReferenceAtNode,
    # Create
    CreateCubeNode,
    CreateSphereNode,
    CreateCylinderNode,
    CreateConeNode,
    CreateCapsuleNode,
    CreatePlaneNode,
    CreateXformNode,
    PointInstancerNode,
    MakeInstanceableNode,
    ScatterPointsNode,
    # Simulation
    RBDSolverNode,
    CollisionSetNode,
    WindForceNode,
    DragForceNode,
    TurbulenceForceNode,
    # Lighting
    CreateRectLightNode,
    CreateSphereLightNode,
    CreateDistantLightNode,
    CreateDomeLightNode,
    CreateDiskLightNode,
    # Camera
    CreateCameraNode,
    SetActiveCameraNode,
    # Animation
    SetKeyNode,
    GetTimeRangeNode,
    SetTimeCodeNode,
    # Material
    SetDisplayColorNode,
    AssignMaterialNode,
    CreatePreviewSurfaceNode,
    TextureFileNode,
    CreateUVTextureNode,
    CreatePrimvarReaderNode,
    GetMaterialShadersNode,
    GetShaderInputNode,
    SetShaderInputNode,
    ConnectShaderNode,
    SetUVTransformNode,
    GetPrimMaterialNode,
    # Utility
    ListIndexNode,
    ListLengthNode,
    ForEachNode,
    MergeNode,
    CompareNode,
    SwitchNode,
    ExportUsdNode,
    CounterNode,
    StringFormatNode,
    StringSplitNode,
    StringJoinNode,
    StringReplaceNode,
    BoolAndNode,
    BoolOrNode,
    BoolNotNode,
    CoalesceNode,
    # Math
    MathAddNode,
    MathSubtractNode,
    MathMultiplyNode,
    MathDivideNode,
    MathClampNode,
    MathRemapNode,
    # Code
    PythonNode,
    # Chunks
    DynamicChunkNode,
    # Debug
    PrintNode,
    # Event
    OnSelectionChangedNode,
    OnFrameChangedNode,
    OnTimerNode,
    OnStageChangedNode,
    OnPrimChangedNode,
    OnAttributeChangedNode,
]

# Category → [node_class] mapping for the tab menu
NODE_CATEGORIES: dict = {}
for _n in ALL_NODES:
    _cat = getattr(_n, "CATEGORY", "Other")
    NODE_CATEGORIES.setdefault(_cat, []).append(_n)

# ── Chunk node auto-generation ─────────────────────────────────────────────
from ._chunk_factory import make_chunk_node

_SKIP_MODULES = {
    "node_graph_chunk",
    "usd_mcp_server",
    "kiro_chunk_web",
    "kiro_chunk",
    "render_preview",
    "ai_chat.chunk",
}


def discover_chunk_nodes() -> list:
    """Scan loaded chunk modules and generate node classes for the tab menu."""
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
            if any(
                cm.module_name in getattr(n, "_chunk_module", "") for n in ALL_NODES
            ):
                continue
            try:
                scene = reg.scene
                chunk_name = None
                if scene:
                    for ch in scene.get_chunks():
                        if cm.module_name in str(getattr(ch, "_module_name", "")):
                            chunk_name = ch.name
                            break
                if not chunk_name:
                    chunk_name = cm.module_name.split(".")[-1].replace("_", " ").title()
                discovered.append(make_chunk_node(cm.module_name, chunk_name))
            except Exception:
                pass
    except Exception:
        pass
    return discovered
