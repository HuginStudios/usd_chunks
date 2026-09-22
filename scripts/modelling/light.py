from usd_chunks import chunk, chunk_notice, chunk_stage, chunk_utils
from pxr import UsdGeom

# The UsdLux schemas this chunk can define. Module-level so config() can build one menu
# entry per type from the same list main() fills the menu from.
_LIGHT_TYPES = ["DiskLight", "RectLight", "DomeLight", "DistantLight", "SphereLight", "CylinderLight"]


def _label(light_type: str) -> str:
    """'DomeLight' -> 'Dome Light', the name the menu shows for a variant.

    Builtins only: config() also runs at registry discovery, where a module's own imports
    (other than usd_chunks) have not been executed, so `re` would not be there.
    """
    return "".join(f" {c}" if c.isupper() and i else c for i, c in enumerate(light_type))


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
        menu_items=list(_LIGHT_TYPES),
    )
    chunk_api.add_param(light_type)

    attribute_group = chunk.Group("attr_grp", flags=chunk.ChunkFlags.GroupFlags.FRAMED)
    chunk_api.add_param(attribute_group)

    return chunk_api


def execute(node):
    stage = node.get_stage()
    raw_path = node.get_param("primpath_location").value
    light_type = node.get_param("light_type").value

    chunk_layer = stage.GetEditTarget().GetLayer()

    # Gated off: leave the cook for when the branch comes back.
    #
    # A muted layer contributes nothing to composition, so DefinePrim below hands back an
    # INVALID prim -- and show_attribute clears this chunk's params and returns when it gets
    # one. The params would survive (they are restored from the chunk prim), but the
    # binding between them and the light's attributes would not: with nothing bound,
    # push_chunk_values has nothing to push when the gate re-opens, and the cache set here
    # would then say the cook is already done. Live-found on demo scene 10, where a light
    # on a shot-gated branch rendered at intensity 1 -- its schema default -- on every shot
    # that switched it on, with the real 68 sitting in the chunk prim the whole time.
    if chunk_layer and stage.IsLayerMuted(chunk_layer.identifier):
        return

    # The cache says "this type is already at this path, nothing to redo" -- true only
    # while the params it built are still bound to that prim's attributes. A cook that ran
    # against a muted layer built none, and without the binding half of this test the cache
    # kept claiming the work was done long after the gate re-opened, so the light drove
    # nothing for the life of the scene.
    #
    # Not during a load, though: the restore of this chunk's saved values is what does the
    # binding, and it is still in flight. Re-running show_attribute underneath it clears the
    # params it is half way through filling in. The cook that matters here is the one after
    # the gate re-opens, which is long past loading.
    if (light_type == getattr(node, '_last_light_type', None)
            and raw_path == getattr(node, '_last_light_path', None)
            and (chunk_notice._loading_stage or chunk_stage.params_are_bound(node))):
        return

    node._last_light_type = light_type

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
        category=chunk.StandardCategory.MODELLING,
        # One searchable menu entry per light type -- "Dome Light" is found and created
        # directly rather than by creating Light and re-picking the type. Each still
        # creates this one chunk; the entry just presets light_type.
        variants=[(_label(t), {"light_type": t}) for t in _LIGHT_TYPES],
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

