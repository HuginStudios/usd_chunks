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
    # unmutes layers one at a time while recooking (chunk_usd.recook_for_view), so the mesh
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
    target_paths = [
        path
        for path in chunk_utils.iter_prim_targets(stage, prim_path_param)
        if (prim := stage.GetPrimAtPath(path)) and prim.IsDefined()
    ]
    if not target_paths:
        return

    layer.Clear()

    # Re-resolve AFTER the Clear, and keep paths (not prim handles) across it: clearing the
    # layer resyncs the stage, which expires every Usd.Prim taken before it -- using one
    # afterwards raises "expired prim".
    material = UsdShade.Material(stage.GetPrimAtPath(mat_path))
    if not material:
        return
    for path in target_paths:
        prim = stage.GetPrimAtPath(path)
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
        category=chunk.StandardCategory.USD,
        description="""
        Bind Material chunk — binds an existing material prim to one or more target prims.

        Parameters:
          - Material Path: USD path to an existing UsdShade.Material prim (e.g. /materials/Gold).
            Use an expression to reference another chunk's output, e.g. @AssignMaterial.material_path@.
          - Prim Path: comma-separated prim paths or glob patterns to bind the material to.
            Supports variant context authoring and prim selection from the DCC.
        """,
    )
