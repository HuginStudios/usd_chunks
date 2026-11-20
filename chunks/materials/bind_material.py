from __future__ import annotations
from pxr import UsdShade
from usd_chunks import chunk, chunk_utils


# Stronger: this binding beats any its target's children have (a referenced asset's own
# materials). Weaker: the children's bindings win, as a mesh's face subsets need when the
# mesh is bound to one material and a subset to another.
_STRENGTHS = {
    "Stronger than Descendants": UsdShade.Tokens.strongerThanDescendants,
    "Weaker than Descendants": UsdShade.Tokens.weakerThanDescendants,
}


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
        # Absent for the moment is the delicate case above; absent for good is a typo or a
        # renamed material, which would otherwise look exactly like it.
        chunk_utils.warn_if_missing(
            node, mat_path, "there is no material to bind", node.get_param("material_path"))
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
    path_strs = []
    for path_str in prim_path_param.resolve_paths():
        if _target_defined(stage, path_str):
            path_strs.append(path_str)
        else:
            chunk_utils.warn_if_missing(
                node, chunk_utils.split_variant_path(path_str)[1], f"{mat_path} is not bound to it",
                prim_path_param)
    if not path_strs:
        return

    material = UsdShade.Material(stage.GetPrimAtPath(mat_path))
    strength = _STRENGTHS.get(node.get_param("strength").value, UsdShade.Tokens.strongerThanDescendants)
    for path_str in path_strs:
        with chunk_utils.author_in_variant_context(stage, path_str) as target_path:
            prim = stage.GetPrimAtPath(target_path)
            if prim and prim.IsValid():
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, strength)


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

    strength = chunk.Menu(
        "strength", "Strength", menu_items=list(_STRENGTHS), default_value="Stronger than Descendants",
        tooltip="Stronger: beats the materials bound below the target, such as an asset's own.\n"
        "Weaker: what is bound below wins, such as a face subset's material on a bound mesh.",
    )

    chunk_api.add_params([material_path, primpath, strength])
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
          - Strength: Stronger than Descendants (the default) beats whatever is bound below
            the target, such as a referenced asset's own materials. Weaker than Descendants
            lets those win: bind a mesh to bark, and its leaf face subset keeps its leaves.
        """,
    )
