from usd_chunks import chunk, chunk_utils
from pxr import UsdGeom


def main():
    chunk_api = chunk.Chunk("Light")

    # prim location
    # A literal, not get_unique_path(stage, ...). A default computed from whatever the stage
    # holds at construction is a value wearing a default's clothes: the save path leaves
    # defaults out on the grounds that running main() again reproduces them, and running
    # main() again does not reproduce this one -- on reload no prim exists yet, so every
    # instance is handed the same path back. execute() does the uniquifying and records the
    # result as a value, which is what survives a save.
    prim_path = "/lights/light1"
    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value=prim_path,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    chunk_api.add_param(primpath)

    light_type = chunk.Menu(
        "light_type",
        "Light type",
        menu_items=["DiskLight", "RectLight", "DomeLight", "DistantLight", "SphereLight", "CylinderLight"],
    )
    chunk_api.add_param(light_type)

    attribute_group = chunk.Group("attr_grp", flags=chunk.ChunkFlags.GroupFlags.FRAMED)
    chunk_api.add_param(attribute_group)

    return chunk_api


def execute(node):
    stage = node.get_stage()
    raw_path = node.get_param("primpath_location").value
    light_type = node.get_param("light_type").value

    if light_type == getattr(node, '_last_light_type', None) and raw_path == getattr(node, '_last_light_path', None):
        return

    node._last_light_type = light_type

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    _, _, var_selections = chunk_utils.split_variant_path(raw_path)
    if not var_selections:
        raw_path = chunk_utils.get_unique_path(stage, raw_path)
        # Recorded as a value, not left as a local. This is the only thing telling this
        # chunk apart from another of the same kind, and a saved scene carries chunk-prim
        # values -- without it two cameras reopened both claiming /cameras/camera1 and
        # authored over one prim. Guarded so a path that did not move is not written back
        # on every cook.
        if raw_path != node.get_param("primpath_location").value:
            node.get_param("primpath_location").value = raw_path

    node._last_light_path = raw_path

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        prim = stage.DefinePrim(target_path, light_type)
        attr = UsdGeom.VisibilityAPI.Apply(prim).CreateGuideVisibilityAttr()
        attr.Set(UsdGeom.Tokens.inherited)
        if not var_selections and prim.GetPath().pathString != target_path:
            node.get_param("primpath_location").value = prim.GetPath().pathString
            node._last_light_path = prim.GetPath().pathString
        attr_grp = node.get_param("attr_grp", chunk.is_group)
        chunk_utils.show_attribute(prim, attr_grp)


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description="""
        Light chunk — creates a USD light prim of a chosen type at a given path
        on the active stage.

        Parameters:
          - Prim Path: stage path for the light prim. Auto-initialised to a unique
            /lights/light1 path on load. Updated in-place if the path was already
            taken (outside of variant contexts).
          - Light Type: the UsdLux light schema to define — DiskLight, RectLight,
            DomeLight, DistantLight, SphereLight, or CylinderLight.

        On any change the edit-target layer is cleared and the prim re-defined,
        keeping authoring idempotent. GuideVisibility is explicitly set to
        inherited so the light is visible in DCC guide modes. Variant-path
        authoring is supported via author_in_variant_context — uniqueness checks
        are skipped when a variant selection is embedded in the path.
        """,
    )

