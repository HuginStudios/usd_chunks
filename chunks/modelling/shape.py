"""Shape chunk -- one of USD's own shape prims: Cube, Sphere, Cylinder, Cone, Capsule, Plane.

Where Mesh builds polygons, these are UsdGeom's implicit shapes: a Cube prim with a size, a
Sphere with a radius, which renderers tessellate themselves. A scene that asks for "a Cube"
in the USD sense (typeName Cube) needs these, and three agents in a benchmark each wrote a
chunk of their own for it.
"""

from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks import chunk, chunk_utils

_SHAPES = ["Cube", "Sphere", "Cylinder", "Cone", "Capsule", "Plane"]
_ROUND = ("Cylinder", "Cone", "Capsule")
_HIDE = chunk.ChunkFlags.State.HIDDEN | chunk.ChunkFlags.State.DISABLED
#: Which param group (by label) each shape shows.
_GROUP_OF = {"Cube": "Cube", "Sphere": "Sphere", "Plane": "Plane", **{s: "Round" for s in _ROUND}}


def _f(node, name: str) -> float:
    return node.get_param(name, chunk.is_param_t(float)).value


def _show(owner, shape: str) -> None:
    """Show the picked shape's params, and Axis where the shape has one."""
    for g in owner.get_my_params(chunk.is_group):
        if g.get_label() == _GROUP_OF.get(shape):
            g.unset_state(_HIDE)
        else:
            g.set_state(_HIDE)
    axis = owner.get_param("axis")
    if axis is not None:
        if shape in _ROUND or shape == "Plane":
            axis.unset_state(_HIDE)
        else:
            axis.set_state(_HIDE)


def _on_type_changed(owner, notice, sender) -> None:
    _show(owner, sender.value)


def _author(node, stage, path, shape: str) -> None:
    axis = node.get_param("axis", chunk.is_param_t(str)).value
    if shape == "Cube":
        prim = UsdGeom.Cube.Define(stage, path)
        prim.CreateSizeAttr(_f(node, "cube_size"))
    elif shape == "Sphere":
        prim = UsdGeom.Sphere.Define(stage, path)
        prim.CreateRadiusAttr(_f(node, "sphere_radius"))
    elif shape in _ROUND:
        prim = getattr(UsdGeom, shape).Define(stage, path)
        prim.CreateRadiusAttr(_f(node, "round_radius"))
        prim.CreateHeightAttr(_f(node, "round_height"))
        prim.CreateAxisAttr(axis)
    else:
        prim = UsdGeom.Plane.Define(stage, path)
        prim.CreateWidthAttr(_f(node, "plane_width"))
        prim.CreateLengthAttr(_f(node, "plane_length"))
        prim.CreateAxisAttr(axis)
    prim.CreateExtentAttr(UsdGeom.Boundable.ComputeExtentFromPlugins(prim, Usd.TimeCode.Default()))


def execute(node) -> None:
    """Define the picked shape at Prim Path, in every variant context it is authored in."""
    is_s = chunk.is_param_t(str)
    shape = node.get_param("shape_type", is_s).value
    _show(node, shape)
    stage = node.get_stage()
    if stage is None or shape not in _SHAPES:
        return
    for path in chunk_utils.author_in_variant_contexts(stage, node.get_param("prim_path", is_s)):
        _author(node, stage, path, shape)


def main():
    chunk_api = chunk.Chunk("Shape")
    shape_type = chunk.Menu("shape_type", "Shape", menu_items=_SHAPES, default_value="Cube")
    chunk_api.add_params([
        chunk.LineEdit("prim_path", "Prim Path", default_value="/shape"),
        shape_type,
        chunk.Menu("axis", "Axis", menu_items=["X", "Y", "Z"], default_value="Y",
                   tooltip="The axis a cylinder, cone or capsule runs along, and a plane's normal."),
    ])
    groups = {
        "Cube": [chunk.Slider("cube_size", "Size", min_value=0.0, max_value=100.0, default_value=1.0,
                              tooltip="Edge length, centred on the prim's origin.")],
        "Sphere": [chunk.Slider("sphere_radius", "Radius", min_value=0.0, max_value=100.0, default_value=0.5)],
        "Round": [chunk.Slider("round_radius", "Radius", min_value=0.0, max_value=100.0, default_value=0.5),
                  chunk.Slider("round_height", "Height", min_value=0.0, max_value=100.0, default_value=1.0,
                               tooltip="Along Axis, centred on the prim's origin.")],
        "Plane": [chunk.Slider("plane_width", "Width", min_value=0.0, max_value=100.0, default_value=1.0),
                  chunk.Slider("plane_length", "Length", min_value=0.0, max_value=100.0, default_value=1.0)],
    }
    for label, params in groups.items():
        group = chunk.Group(label.lower(), label)
        group.add_params(params)
        group.set_state(_HIDE)
        chunk_api.add_param(group)
    chunk_api.add_callback(_on_type_changed, sender=shape_type)
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="cube",
        description="""
        USD's own shape prims: Cube, Sphere, Cylinder, Cone, Capsule, Plane; not polygon meshes.

        Defines a UsdGeom Cube, Sphere, Cylinder, Cone, Capsule or Plane prim at Prim Path,
        which renderers tessellate themselves. For a polygon mesh with UVs, use Mesh. Each menu
        entry (USD Cube, USD Sphere, ...) is this chunk with the shape already set.

        Shapes are centred on the prim's origin. Size params, by shape (script names in brackets):
          - Cube: edge length [cube_size], 1 by default.
          - Sphere: radius [sphere_radius], 0.5.
          - Cylinder, Cone, Capsule: radius and height [round_radius, round_height], 0.5 and 1,
            along Axis [axis].
          - Plane: width and length [plane_width, plane_length], 1 by 1, facing along Axis.
        Axis is Y by default, the up axis of a Y-up stage. Move a shape with a Transform below.
        """,
        # The shapes spelled out here: config() runs on its own, before the module's constants.
        variants=[(f"USD {s}", {"shape_type": s, "prim_path": f"/{s.lower()}"})
                  for s in ("Cube", "Sphere", "Cylinder", "Cone", "Capsule", "Plane")],
    )
