from pxr import Sdf, Usd, UsdShade
from usd_chunks import chunk


_READER_TYPES = ["float2", "float", "float3", "color3f", "normal3f", "vector3f", "int", "string"]
_SURFACE_INPUTS = [
    "(none)",
    "diffuseColor", "roughness", "metallic", "opacity",
    "normal", "emissiveColor", "specularColor",
    "clearcoat", "clearcoatRoughness", "occlusion",
]

_SDF_TYPE = {
    "float2":   Sdf.ValueTypeNames.Float2,
    "float":    Sdf.ValueTypeNames.Float,
    "float3":   Sdf.ValueTypeNames.Float3,
    "color3f":  Sdf.ValueTypeNames.Color3f,
    "normal3f": Sdf.ValueTypeNames.Normal3f,
    "vector3f": Sdf.ValueTypeNames.Vector3f,
    "int":      Sdf.ValueTypeNames.Int,
    "string":   Sdf.ValueTypeNames.String,
}
_READER_ID = {
    "float2":   "UsdPrimvarReader_float2",
    "float":    "UsdPrimvarReader_float",
    "float3":   "UsdPrimvarReader_float3",
    "color3f":  "UsdPrimvarReader_float3",
    "normal3f": "UsdPrimvarReader_float3",
    "vector3f": "UsdPrimvarReader_float3",
    "int":      "UsdPrimvarReader_int",
    "string":   "UsdPrimvarReader_string",
}


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_location     = node.get_param("primpath_location").value
    varname      = node.get_param("varname").value or "st"
    reader_type  = node.get_param("reader_type").value
    target_input = node.get_param("target_input").value

    if not primpath_location:
        return

    sdf_type  = _SDF_TYPE.get(reader_type, Sdf.ValueTypeNames.Float2)
    reader_id = _READER_ID.get(reader_type, "UsdPrimvarReader_float2")

    reader = UsdShade.Shader.Define(stage, f"{primpath_location}")
    reader.CreateIdAttr(reader_id)
    reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set(varname)
    reader_out = reader.CreateOutput("result", sdf_type)

    if target_input and target_input != "(none)":
        surface = UsdShade.Shader.Get(stage, f"{primpath_location}/surface")
        if surface:
            surface.CreateInput(target_input, sdf_type).ConnectToSource(reader_out)

    _prim_path = getattr(node, '_exec_prim_path', None) or getattr(node, 'prim_path', None)
    chunk_prim = stage.GetPrimAtPath(_prim_path) if _prim_path else None
    if chunk_prim and chunk_prim.IsValid():
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            attr = chunk_prim.GetAttribute("outputs:result")
            if attr and attr.IsValid():
                attr.SetConnections([Sdf.Path(f"{primpath_location}.outputs:result")])


def main():
    chunk_api = chunk.Chunk("Primvar Reader")

    primpath_location = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/materials/surface/PrimvarReader",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_TARGET,
    )
    
    varname = chunk.LineEdit(
        "varname", "Primvar Name",
        default_value="st",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    reader_type = chunk.Menu(
        "reader_type", "Type",
        menu_items=_READER_TYPES,
        default_value="float2",
    )
    target_input = chunk.Menu(
        "target_input", "Connect To",
        menu_items=_SURFACE_INPUTS,
        default_value="(none)",
    )

    chunk_api.add_params([primpath_location, varname, reader_type, target_input])
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Material",
        outputs=[("result", "Float2")],
        description="""
        Primvar Reader chunk — adds a UsdPrimvarReader shader of the chosen type into
        an existing material network and optionally connects its output to a
        UsdPreviewSurface input.

        primpath_location should point to the material (use @PreviewSurface.primpath_location@).
        varname is the primvar to read (e.g. 'st' for UVs, 'Cd' or 'displayColor'
        for vertex colours). When Connect To is not '(none)', the reader output is
        wired directly to that surface shader input.
        """,
    )
