from __future__ import annotations
from pxr import UsdShade
from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()

    mat_path = node.get_param("material_path").value
    prim_path_param = node.get_param("primpath_location")

    if not mat_path or not prim_path_param.value:
        # The user actually emptied a field -- that IS an instruction to unbind, and the
        # layer the framework cleared for this cook already holds no binding.
        return

    # Nothing to bind to right now, so author nothing. This used to be the delicate case:
    # view-driven execution (chunk_stage.recook_for_view) mutes and unmutes layers one at a
    # time, so the mesh a binding targets, or the material itself, is routinely absent for
    # part of a recook -- and a recook re-ran every chunk, destroying a good binding each
    # time. It now re-runs only a stale chunk, so a binding that landed is left alone, and
    # the cook a change asks for is the one that meets this.
    #
    # IsDefined(), not IsValid(): some other layer's `over` on the target (a Transform's,
    # say) still reads as a valid prim once the layer that actually defines the mesh is
    # muted.
    if not UsdShade.Material(stage.GetPrimAtPath(mat_path)):
        return
    # The path STRINGS, variant selections and all -- not the stripped target paths
    # author_in_variant_contexts yields. That generator yields from inside the variant
    # edit context and leaves it as soon as the next path is pulled, so collecting its
    # results into a list and binding afterwards authored every binding OUTSIDE the
    # variant: a Prim Path of "/asset{color=red}/geo" put the binding on /asset/geo for
    # every colour. Each path is re-entered below, at the moment its binding is written.
    #
    # Definedness is checked inside the context too: a prim that only exists in the
    # selected variant is not defined until that variant is selected.
    path_strs = [
        path_str
        for path_str in prim_path_param.resolve_paths()
        if _target_defined(stage, path_str)
    ]
    if not path_strs:
        return

    material = UsdShade.Material(stage.GetPrimAtPath(mat_path))
    for path_str in path_strs:
        with chunk_utils.author_in_variant_context(stage, path_str) as target_path:
            prim = stage.GetPrimAtPath(target_path)
            if prim and prim.IsValid():
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, UsdShade.Tokens.strongerThanDescendants)


def _target_defined(stage, path_str) -> bool:
    with chunk_utils.author_in_variant_context(stage, path_str) as target_path:
        prim = stage.GetPrimAtPath(target_path)
        return bool(prim) and prim.IsDefined()


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
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MATERIALS,
        icon="sphere",
        description="""
        Bind Material chunk — binds an existing material prim to one or more target prims.

        Parameters:
          - Material Path: USD path to an existing UsdShade.Material prim (e.g. /materials/Gold).
            Use an expression to reference another chunk's output, e.g. @AssignMaterial.material_path@.
          - Prim Path: comma-separated prim paths or glob patterns to bind the material to.
            Supports variant context authoring and prim selection from the DCC.
        """,
    )
