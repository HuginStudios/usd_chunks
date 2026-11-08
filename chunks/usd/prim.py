from __future__ import annotations
from pxr import Sdf
from usd_chunks import chunk, chunk_utils, sdf_authoring


def execute(node):
    stage = node.get_stage()

    primpath_param = node.get_param("primpath_location")
    prim_type = node.get_param("prim_type").value
    for path in chunk_utils.author_in_variant_contexts(stage, primpath_param, unique=True):
        if prim_type == "Class":
            # A `class` specifier, not a type: an abstract prim for Inherit and Specialize
            # to point at. DefinePrim(path, "Class") made a concrete prim typed "Class".
            stage.CreateClassPrim(path)
            sdf_authoring.Writer(stage).over(path).specifier = Sdf.SpecifierClass
            continue
        stage.DefinePrim(path, prim_type)  # defines any missing parents too
        # Into this chunk's own layer even when another layer already defines the prim the
        # same way: DefinePrim writes nothing then, and the layer's publish lost the def.
        sdf_authoring.Writer(stage).define(path, prim_type)


def main():
    chunk_api = chunk.Chunk("Prim")

    # prim location
    stage = chunk_api.get_stage()
    prim_path = chunk_utils.get_unique_path(stage, "/prim")
    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value=prim_path,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    chunk_api.add_param(primpath)
    prim_type = chunk.Menu(
        "prim_type",
        "Prim type",
        menu_items=["Xform", "Scope", "Class"],
    )
    chunk_api.add_param(prim_type)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="cube",
        description="""
        Prim chunk — defines a new USD prim at a given path on the active stage.

        Parameters:
          - Prim Path: the stage path where the prim will be created. Auto-initialised
            to a unique /prim path on load. If the path is already taken (and no
            variant selection is embedded), a unique suffix is chosen and the field
            is updated to reflect the actual path.
          - Prim Type: Xform or Scope defines a prim of that type. Class makes an
            abstract `class` prim (no type, not rendered or traversed) for Inherit and
            Specialize chunks to point at.

        When the path contains an embedded variant selection (e.g. /root{var=val}/child),
        uniqueness is not enforced and the prim is authored directly inside that
        variant via author_in_variant_contexts.
        """,
    )
