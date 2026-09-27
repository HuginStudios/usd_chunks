from __future__ import annotations
from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    primpath_param = node.get_param("primpath_location")
    prim_type = node.get_param("prim_type").value
    for path in chunk_utils.author_in_variant_contexts(stage, primpath_param, unique=True):
        stage.DefinePrim(path, prim_type)


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
        description="""
        Prim chunk — defines a new USD prim at a given path on the active stage.

        Parameters:
          - Prim Path: the stage path where the prim will be created. Auto-initialised
            to a unique /prim path on load. If the path is already taken (and no
            variant selection is embedded), a unique suffix is chosen and the field
            is updated to reflect the actual path.
          - Prim Type: the USD prim type to define — either Xform, Scope or Class.

        When the path contains an embedded variant selection (e.g. /root{var=val}/child),
        uniqueness is not enforced and the prim is authored directly inside that
        variant via author_in_variant_contexts. Outside of variants the chunk clears
        its edit-target layer before each execution so re-runs are idempotent.
        """,
    )
