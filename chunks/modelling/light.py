from __future__ import annotations
import logging

from pxr import UsdGeom

from usd_chunks import chunk, chunk_utils

# Parented under "usd_chunks" rather than left at the bare module name: a chunk module is
# imported under its own file name, so getLogger(__name__) lands outside the package tree.
logger = logging.getLogger("usd_chunks.light")

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
    light_type = node.get_param("light_type").value

    primpath_param = node.get_param("primpath_location")
    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param, unique=True):
        prim = stage.DefinePrim(target_path, light_type)
        attr = UsdGeom.VisibilityAPI.Apply(prim).CreateGuideVisibilityAttr()
        attr.Set(UsdGeom.Tokens.inherited)

        # Re-fetch by path rather than reusing the handle from DefinePrim. Applying an API
        # schema above resyncs the prim, and a resync EXPIRES the handle -- reading an
        # attribute off it then raises, which is not a crash that stays local: this cook can
        # be running inside a gate's own evaluation (a Stage Expression Switch writes its
        # muted record, that write cascades, and this light is what it cascades into), so
        # the exception would abort the gate half way and leave the branch it was
        # suppressing on the stage.
        prim_path = prim.GetPath()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            logger.debug("Light %s: prim at %s is gone mid-cook, leaving it for the next "
                         "cook", node.name, prim_path)
            return

        attr_grp = node.get_param("attr_grp", chunk.is_group)
        chunk.show_attribute(prim, attr_grp)


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="sun",
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

