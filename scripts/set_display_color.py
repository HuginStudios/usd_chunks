"""Set Display Color chunk — sets displayColor on the selected prim(s).

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

from pxr import Gf, UsdGeom, Vt

from usd_chunks import chunk


def _run(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    prims = _chunk.get_param("prim_path").get_prims()
    if not prims:
        return

    is_f = chunk.is_param_t(float)
    r = _chunk.get_param("color_r", is_f).value
    g = _chunk.get_param("color_g", is_f).value
    b = _chunk.get_param("color_b", is_f).value
    color = Vt.Vec3fArray([Gf.Vec3f(r, g, b)])

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # Callbacks already run inside the chunk's edit context (NoticeConnection).
    for prim in prims:
        gprim = UsdGeom.Gprim(prim)
        if gprim:
            gprim.GetDisplayColorAttr().Set(color)


def execute(node: chunk.Chunk) -> None:
    """Reserved top-level reactive hook -- load_chunks calls this once per chunk after
    bulk-restoring its param values, so a reloaded Set Display Color actually re-authors
    displayColor instead of leaving the target prim uncoloured (its real work lives in
    _run, an add_callback handler, which load's value-restore deliberately does not
    trigger)."""
    _run(node, None, None)


def main():
    chunk_api = chunk.Chunk("Set Display Color")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prim to colour — pick it in the viewport or type a path.",
    )
    chunk_api.add_param(prim_path)
    prim_path.add_callback(_run)

    color_grp = chunk.Group("Color")
    color_grp.add_param(chunk.Spinbox("color_r", "R", default_value=0.5, tooltip="Red channel (0–1)."))
    color_grp.add_param(chunk.Spinbox("color_g", "G", default_value=0.5, tooltip="Green channel (0–1)."))
    color_grp.add_param(chunk.Spinbox("color_b", "B", default_value=0.5, tooltip="Blue channel (0–1)."))
    chunk_api.add_param(color_grp)

    for name in ("color_r", "color_g", "color_b"):
        chunk_api.get_param(name).add_callback(_run)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Material",
        description=(
            "Authors a uniform displayColor (primvars:displayColor) on the target prim —\n"
            "a quick flat colour for previews or for tagging prims, without needing a\n"
            "material."
        ),
    )
