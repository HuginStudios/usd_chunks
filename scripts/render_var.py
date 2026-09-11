"""Render Var chunk — creates a UsdRender.Var prim describing a single AOV.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

from pxr import Sdf, UsdRender

from usd_chunks import chunk, chunk_notice, chunk_utils


# color4f is what V-Ray asks for on every one of its render elements, whatever the
# element actually measures -- the type is how the renderer decides the channel
# names it writes, not a description of the data. Missing from this list, the Menu
# rejected the value and silently kept color3f, so a var authored for V-Ray came out
# as something it does not recognise.
_DATA_TYPES = ["color3f", "color4f", "float", "normal3f", "vector3f", "point3f", "int"]
_SOURCE_TYPES = ["raw", "primvar", "lpe", "intrinsic"]

# UsdRender.Var itself carries only dataType/sourceName/sourceType. Everything a
# driver needs on top of that -- the channel name it writes, its bit depth, and any
# renderer-specific switch -- travels as extra namespaced attributes under
# driver:parameters:aov:. That namespace is husk's convention, not USD's, and the
# renderer-scoped tail (driver:parameters:aov:<renderer>:<setting>) is what husk
# strips down to <setting> before handing it to the delegate.
_DRIVER_PREFIX = "driver:parameters:aov:"
_DRIVER_TYPES = ["string", "token", "bool", "int", "float"]
_SDF_TYPES = {
    "string": Sdf.ValueTypeNames.String,
    "token": Sdf.ValueTypeNames.Token,
    "bool": Sdf.ValueTypeNames.Bool,
    "int": Sdf.ValueTypeNames.Int,
    "float": Sdf.ValueTypeNames.Float,
}


def _coerce(type_name: str, text: str):
    if type_name == "bool":
        return text.strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(float(text))
    if type_name == "float":
        return float(text)
    return text


def main():
    chunk_api = chunk.Chunk("Render Var")

    # A literal, not get_unique_path(stage, ...). A default computed from whatever the stage
    # holds at construction is a value wearing a default's clothes: the save path leaves
    # defaults out on the grounds that running main() again reproduces them, and running
    # main() again does not reproduce this one -- on reload no prim exists yet, so every
    # instance is handed the same path back. execute() does the uniquifying and records the
    # result as a value, which is what survives a save.
    prim_path = "/Render/Vars/beauty"

    chunk_api.add_params([
        chunk.LineEdit(
            "primpath_location", "Prim Path",
            default_value=prim_path,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.Menu("data_type", "Data Type", menu_items=_DATA_TYPES, default_value="color3f"),
        chunk.LineEdit("source_name", "Source Name", default_value="Ci"),
        chunk.Menu("source_type", "Source Type", menu_items=_SOURCE_TYPES, default_value="raw"),
    ])

    driver_params = chunk.ListGroup("driver_params", "Driver Parameters")
    driver_params.add_params([
        chunk.LineEdit("dp_name", "Name", flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Menu("dp_type", "Type", menu_items=_DRIVER_TYPES, default_value="string"),
        chunk.LineEdit("dp_value", "Value", flags=chunk.ChunkFlags.ParamFlags.LABELED),
    ])
    chunk_api.add_param(driver_params)

    return chunk_api


def _author_driver_params(node, prim):
    rows = node.get_param("driver_params", chunk.is_inst(chunk.ListGroup))
    if rows is None:
        return

    for i in range(rows.size):
        name = rows.get_indexed_param("dp_name", i)
        name = (name.value or "").strip() if name else ""
        if not name:
            continue

        type_param = rows.get_indexed_param("dp_type", i)
        type_name = (type_param.value if type_param else None) or "string"
        value_param = rows.get_indexed_param("dp_value", i)
        text = (value_param.value if value_param else None) or ""

        sdf_type = _SDF_TYPES.get(type_name)
        if sdf_type is None:
            continue
        try:
            value = _coerce(type_name, text)
        except ValueError:
            continue

        # A row may spell out the whole attribute (driver:parameters:husk:foo, which is
        # not under the aov: scope at all), so only prefix a bare setting name.
        attr_name = name if name.startswith("driver:parameters:") else _DRIVER_PREFIX + name
        prim.CreateAttribute(attr_name, sdf_type, custom=False).Set(value)


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    raw_path = node.get_param("primpath_location").value
    if not raw_path:
        return

    _, _, var_selections = chunk_utils.split_variant_path(raw_path)
    if not var_selections and not chunk_notice._loading_stage:
        raw_path = chunk_utils.get_unique_path(stage, raw_path)
        # Recorded as a value, not left as a local. This is the only thing telling this
        # chunk apart from another of the same kind, and a saved scene carries chunk-prim
        # values -- without it two cameras reopened both claiming /cameras/camera1 and
        # authored over one prim. Guarded so a path that did not move is not written back
        # on every cook.
        if raw_path != node.get_param("primpath_location").value:
            node.get_param("primpath_location").value = raw_path

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        render_var = UsdRender.Var.Define(stage, Sdf.Path(target_path))
        if not render_var:
            return

        render_var.CreateDataTypeAttr().Set(node.get_param("data_type").value)
        render_var.CreateSourceNameAttr().Set(node.get_param("source_name").value)
        render_var.CreateSourceTypeAttr().Set(node.get_param("source_type").value)
        _author_driver_params(node, render_var.GetPrim())

        authored = render_var.GetPrim().GetPath().pathString
        if not var_selections and authored != target_path:
            node.get_param("primpath_location").value = authored


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Render Var chunk — creates a UsdRender.Var prim, the USD description of a
        single AOV (arbitrary output variable).

        Parameters:
          - Prim Path: stage path for the RenderVar prim. Auto-initialised to a
            unique /Render/Vars/beauty path.
          - Data Type: the type of the channel this var produces (color3f for a
            beauty or colour AOV, float for depth or alpha, normal3f/vector3f for
            geometric AOVs).
          - Source Name: the renderer-side name of the value being written. What
            is valid here depends on the render delegate — "Ci" and "a" for
            RenderMan-style output, an LPE string, or a primvar name.
          - Source Type: how Source Name should be interpreted — raw (a direct
            renderer output), primvar, lpe (light path expression), or intrinsic.
          - Driver Parameters (list): a repeatable Name / Type / Value row authoring
            an extra attribute on the RenderVar for the image driver to read. A bare
            name is scoped to driver:parameters:aov: — so "name" authors
            driver:parameters:aov:name, and "vray:zDepth:depth_white" authors
            driver:parameters:aov:vray:zDepth:depth_white. A name already starting
            with "driver:parameters:" is used verbatim. Rows with an empty Name, or
            a Value that will not parse as the chosen Type, are skipped.

            This namespace is husk's convention rather than part of UsdRender: husk
            reads name/format/clearValue/multiSampled itself, and forwards a
            renderer-scoped tail (aov:<renderer>:<setting>) to that delegate as
            <setting>. Other renderers ignore what they do not recognise, so the
            rows are additive and safe to leave in place.

        Name this var in a Render Product's Ordered Vars to include the AOV in
        that product's output; the chunks themselves are ordered by the exec
        chain. The result is standard UsdRender scene
        description, readable by any renderer whether or not usd_chunks is
        installed.
        """,
    )
