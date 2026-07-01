from pxr import UsdShade
from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    stage.GetEditTarget().GetLayer().Clear()

    mat_path = node.get_param("material_path").value
    prim_path_param = node.get_param("primpath_location")

    if not mat_path or not prim_path_param.value:
        return

    material = UsdShade.Material(stage.GetPrimAtPath(mat_path))
    if not material:
        return

    for path_str in prim_path_param.resolve_paths():
        with chunk_utils.author_in_variant_context(stage, path_str) as target_path:
            prim = stage.GetPrimAtPath(target_path)
            if prim and prim.IsValid():
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, UsdShade.Tokens.strongerThanDescendants)


def main():
    chunk_api = chunk.Chunk("BindMaterial")

    material_path = chunk.LineEdit(
        "material_path",
        "Material Path",
        default_value="/materials",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )

    chunk_api.add_params([material_path, primpath])
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Bind Material chunk — binds an existing material prim to one or more target prims.

        Parameters:
          - Material Path: USD path to an existing UsdShade.Material prim (e.g. /materials/Gold).
            Use an expression to reference another chunk's output, e.g. @AssignMaterial.material_path@.
          - Prim Path: comma-separated prim paths or glob patterns to bind the material to.
            Supports variant context authoring and prim selection from the DCC.
        """,
    )
