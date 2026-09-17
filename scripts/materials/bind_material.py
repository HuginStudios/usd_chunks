from pxr import UsdShade
from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    layer = stage.GetEditTarget().GetLayer()

    mat_path = node.get_param("material_path").value
    prim_path_param = node.get_param("primpath_location")

    if not mat_path or not prim_path_param.value:
        # The user actually emptied a field -- that IS an instruction to unbind.
        layer.Clear()
        return

    # Check resolvability BEFORE clearing. This used to Clear() first and only then bail
    # out when the material or the target prim could not be found -- which silently
    # destroyed a perfectly good binding whenever this ran at a moment when the stage did
    # not hold them. That is not a rare edge: view-driven execution deliberately mutes and
    # unmutes layers one at a time while recooking (chunk_stage.recook_for_view), so the mesh
    # a binding targets, or the material itself, is routinely absent for part of a recook.
    # Nothing re-runs this chunk once the stage settles -- its own params never changed --
    # so the loss was permanent, and looked like the applied material randomly reverting to
    # the original one. Params pointing at something unresolvable right now is a transient
    # (or user-error) state, not an instruction to throw away the last good result.
    #
    # IsDefined(), not IsValid(): the binding this chunk authored last time is an `over` on
    # the target, and an over-only prim still reads as a valid prim. So IsValid() stays true
    # purely because of our OWN leftover opinion, even once the layer that actually defines
    # the mesh is muted -- which is precisely the case this guard exists to catch.
    if not UsdShade.Material(stage.GetPrimAtPath(mat_path)):
        return
    # The path STRINGS, variant selections and all -- not the stripped target paths
    # iter_prim_targets yields. That generator yields from inside the variant edit context
    # and leaves it as soon as the next path is pulled, so collecting its results into a
    # list and binding afterwards authored every binding OUTSIDE the variant: a Prim Path of
    # "/asset{color=red}/geo" put the binding on /asset/geo for every colour. Each path is
    # re-entered below, at the moment its binding is written.
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

    layer.Clear()

    # Re-resolve AFTER the Clear, and keep paths (not prim handles) across it: clearing the
    # layer resyncs the stage, which expires every Usd.Prim taken before it -- using one
    # afterwards raises "expired prim".
    material = UsdShade.Material(stage.GetPrimAtPath(mat_path))
    if not material:
        return
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
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.MATERIALS,
        description="""
        Bind Material chunk — binds an existing material prim to one or more target prims.

        Parameters:
          - Material Path: USD path to an existing UsdShade.Material prim (e.g. /materials/Gold).
            Use an expression to reference another chunk's output, e.g. @AssignMaterial.material_path@.
          - Prim Path: comma-separated prim paths or glob patterns to bind the material to.
            Supports variant context authoring and prim selection from the DCC.
        """,
    )
