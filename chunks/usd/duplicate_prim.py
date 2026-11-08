"""Duplicate Prim chunk -- deep copies a prim (full subtree) to a new path.

Copies all attributes, metadata, and children from the source prim's composed
view to the destination path. The copy is authored on the chunk's own layer.

Handles name collisions by auto-incrementing a numeric suffix.

With Copies above 1 it makes that many copies under the destination, each moved by the
per-copy offset one more time than the last: a row, a ring, a spiral staircase.

As Instances makes each copy an instanceable reference to the source instead, so USD
composes the source's subtree once and every copy shares it.
"""

from __future__ import annotations

import logging

from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_utils, sdf_authoring

logger = logging.getLogger(__name__)


def _generate_dest_path(stage: Usd.Stage, source_path: str) -> str:
    """Generate a unique destination path based on source path + '_copy' suffix."""
    base = source_path.rstrip("/") + "_copy"
    if not stage.GetPrimAtPath(base).IsValid():
        return base

    count = 1
    while stage.GetPrimAtPath(f"{base}{count}").IsValid():
        count += 1
    return f"{base}{count}"


def _copy_prim_recursive(stage: Usd.Stage, source: Usd.Prim, dest_path: str):
    """Recursively copy a prim and its subtree to dest_path.

    Copies from the composed view: all authored attributes with their values,
    metadata, type name, and children.

    Written as specs in one change block (sdf_authoring): an animated asset is thousands of
    prims times their time samples, and through the Usd API each one recomposed the stage.
    Everything read is the source's composed view, never the copy, so nothing reads back
    what the block has not composed yet.
    """
    writer = sdf_authoring.Writer(stage)
    with Sdf.ChangeBlock():
        _copy_spec(writer, source, dest_path)


def _vec(owner, name, default):
    param = owner.get_param(name)
    value = param.value if param is not None else None
    return Gf.Vec3d(*value) if value is not None else Gf.Vec3d(*default)


def _step_matrix(owner) -> Gf.Matrix4d:
    """One copy's offset, in its parent's space: scale and rotate (X, then Y, then Z) around
    the pivot, then translate."""
    t, r = _vec(owner, "copy_translate", (0, 0, 0)), _vec(owner, "copy_rotate", (0, 0, 0))
    s, p = _vec(owner, "copy_scale", (1, 1, 1)), _vec(owner, "copy_pivot", (0, 0, 0))
    rotation = (Gf.Rotation(Gf.Vec3d.XAxis(), r[0]) * Gf.Rotation(Gf.Vec3d.YAxis(), r[1])
                * Gf.Rotation(Gf.Vec3d.ZAxis(), r[2]))
    return (Gf.Matrix4d().SetTranslate(-p) * Gf.Matrix4d().SetScale(s) * Gf.Matrix4d().SetRotate(rotation)
            * Gf.Matrix4d().SetTranslate(p) * Gf.Matrix4d().SetTranslate(t))


def _set_matrix(spec: Sdf.PrimSpec, matrix: Gf.Matrix4d) -> None:
    """Replace a copied prim's transform ops with one matrix op."""
    for name in [n for n in spec.attributes.keys() if n.startswith("xformOp:") or n == "xformOpOrder"]:
        spec.RemoveProperty(spec.attributes[name])
    sdf_authoring.attribute(spec, "xformOp:transform", Sdf.ValueTypeNames.Matrix4d, matrix)
    sdf_authoring.attribute(spec, "xformOpOrder", Sdf.ValueTypeNames.TokenArray, ["xformOp:transform"], uniform=True)


def _duplicate(stage: Usd.Stage, owner, source: Usd.Prim, dest_path: str) -> None:
    """One copy at dest_path, or with Copies above 1, that many under it.

    Copy i is named after the source with a zero-padded index and placed at the source's own
    transform with the per-copy offset applied i times, so copy 0 sits on the source.
    """
    copies_param = owner.get_param("copies")
    copies = max(1, int(copies_param.value)) if copies_param is not None else 1
    instances_param = owner.get_param("as_instances")
    as_instances = bool(instances_param.value) if instances_param is not None else False
    if as_instances and not source.GetChildren():
        logger.warning("%s has no children: an instance shares only what is below its root, so "
                       "instancing it saves nothing. A PointInstancer (Scatter Points) suits leaf "
                       "prims.", source.GetPath())
    writer = sdf_authoring.Writer(stage)
    if copies == 1:
        if as_instances:
            with Sdf.ChangeBlock():
                _instance_spec(writer, source, dest_path)
        else:
            _copy_prim_recursive(stage, source, dest_path)
        return
    local = UsdGeom.Xformable(source).GetLocalTransformation() if source.IsA(UsdGeom.Xformable) else Gf.Matrix4d(1)
    step, width = _step_matrix(owner), max(2, len(str(copies - 1)))
    with Sdf.ChangeBlock():
        if not stage.GetPrimAtPath(dest_path):
            writer.define(dest_path, "Xform")
        placed = Gf.Matrix4d(local)
        for i in range(copies):
            path = f"{dest_path}/{source.GetName()}_{i:0{width}d}"
            if as_instances:
                _instance_spec(writer, source, path)
            else:
                _copy_spec(writer, source, path)
            _set_matrix(writer.layer.GetPrimAtPath(writer.spec_path(path)), placed)
            placed = placed * step


def _instance_spec(writer: sdf_authoring.Writer, source: Usd.Prim, dest_path: str) -> None:
    """An instanceable internal reference to source at dest_path: its type, properties and
    children all come through the reference, and USD shares the subtree between instances."""
    spec = writer.define(dest_path)
    spec.referenceList.Prepend(Sdf.Reference(primPath=source.GetPath()))
    spec.instanceable = True


def _copy_spec(writer: sdf_authoring.Writer, source: Usd.Prim, dest_path: str):
    if not source.IsValid():
        return

    # Define the destination prim with the same type
    type_name = source.GetTypeName()
    dest_prim = writer.define(dest_path, type_name)

    # Copy metadata
    for key in source.GetAllAuthoredMetadata():
        # Skip path-specific metadata that shouldn't be copied
        if key in ("typeName",):
            continue
        val = source.GetMetadata(key)
        if val is not None:
            try:
                dest_prim.SetInfo(key, val)
            except Exception:
                pass

    # Copy authored attributes. Custom unless the source says otherwise, as
    # Prim.CreateAttribute makes them.
    for attr in source.GetAuthoredAttributes():
        # Copy default value
        dest_attr = sdf_authoring.attribute(
            dest_prim, attr.GetName(), attr.GetTypeName(), attr.Get(Usd.TimeCode.Default()), custom=True
        )

        # Copy time samples
        time_samples = attr.GetTimeSamples()
        for t in time_samples:
            val = attr.Get(t)
            if val is not None:
                writer.layer.SetTimeSample(dest_attr.path, t, val)

        # Copy attribute metadata
        for meta_key in ("interpolation", "custom", "variability"):
            meta_val = attr.GetMetadata(meta_key)
            if meta_val is not None:
                try:
                    dest_attr.SetInfo(meta_key, meta_val)
                except Exception:
                    pass

    # Copy authored relationships
    for rel in source.GetAuthoredRelationships():
        rel_name = rel.GetName()
        targets = rel.GetTargets()
        dest_rel = dest_prim.relationships[rel_name] if rel_name in dest_prim.relationships \
            else Sdf.RelationshipSpec(dest_prim, rel_name, True)
        if targets:
            dest_rel.targetPathList.explicitItems = [writer.target_path(t) for t in targets]

    # Recursively copy children
    for child in source.GetChildren():
        child_name = child.GetName()
        child_dest_path = f"{dest_path}/{child_name}"
        _copy_spec(writer, child, child_dest_path)


def execute(node):
    stage = node.get_stage()

    source_param = node.get_param("source_prim")
    dest_param = node.get_param("dest_path")

    source_path = source_param.value.strip() if source_param.value else ""
    if not source_path:
        return

    # If using prim selection, get first resolved path
    resolved = source_param.resolve_paths() if hasattr(source_param, "resolve_paths") else []
    if resolved:
        source_path = resolved[0]

    source_prim = stage.GetPrimAtPath(source_path)
    if not source_prim.IsValid():
        return

    # Determine destination path. Only an empty field gets filled in; _generate_dest_path
    # already steps around anything occupying the stage, so a suggestion never collides.
    #
    # A destination that is already set is an instruction, not a suggestion, and is left
    # exactly as it is -- even when a prim already stands at it. Saving bakes the copy into
    # the file, so on reopen the copy genuinely is on the stage; treating that as a
    # collision and renaming the destination would leave anything downstream (a Transform
    # aimed at the copy) pointing at a prim that no longer exists.
    #
    # Re-authoring over the baked result is safe: the copy goes onto this chunk's own layer,
    # which the framework clears before every cook, so a reopen reproduces the same prim
    # rather than accumulating another one.
    dest_path = dest_param.value.strip() if dest_param.value else ""
    if not dest_path:
        dest_path = _generate_dest_path(stage, source_path)
        dest_param.value = dest_path
    else:
        # The copy made last cook does not read as a collision and walk the suffix up on
        # every execute: the framework cleared this chunk's layer before the cook. The
        # param, not its value, so an expression comes back resolved.
        dest_path = chunk_utils.get_unique_path(stage, dest_param, own_layer=node.get_edit_context())
        # Never write back over an expression. get_unique_path resolves one to its current
        # value, so assigning that would replace the wire with the path it happened to
        # produce, and the connection is gone. Same guard prim.py uses.
        raw = dest_param.raw_value
        if not chunk_utils.has_expression(raw or "") and dest_path != (raw or ""):
            dest_param.value = dest_path

    _duplicate(stage, node, source_prim, dest_path)


def _on_change(owner, notice, sender):
    """Callback for the chunk panel -- duplicate the prim on any param change."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    source_param = _chunk.get_param("source_prim")
    dest_param = _chunk.get_param("dest_path")

    # Only act when we have a source
    prims = source_param.get_prims()
    if not prims:
        return

    source_prim = prims[0]
    source_path = str(source_prim.GetPath())

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # Determine destination
    dest_path = dest_param.value.strip() if dest_param.value else ""
    if not dest_path:
        dest_path = _generate_dest_path(stage, source_path)
        dest_param.value = dest_path

    _duplicate(stage, _chunk, source_prim, dest_path)


def _on_source_change(owner, notice, sender):
    """When source changes, auto-suggest a destination path."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    source_param = _chunk.get_param("source_prim")
    dest_param = _chunk.get_param("dest_path")

    prims = source_param.get_prims()
    if prims:
        source_path = str(prims[0].GetPath())
        suggested = _generate_dest_path(stage, source_path)
        dest_param.value = suggested

    # Trigger the actual copy
    _on_change(owner, notice, sender)


def main():
    chunk_api = chunk.Chunk("Duplicate Prim")

    source_prim = chunk.LineEdit(
        "source_prim",
        "Source Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prim to duplicate (pick in viewport or type a path).",
    )
    source_prim.add_callback(_on_source_change)
    chunk_api.add_param(source_prim)

    dest_path = chunk.LineEdit(
        "dest_path",
        "Destination Path",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Where to place the copy. Auto-generated if empty (source_copy or source_copy1, etc.).",
    )
    dest_path.add_callback(_on_change)
    chunk_api.add_param(dest_path)

    chunk_api.add_param(chunk.Checkbox(
        "as_instances", "As Instances", default_value=False,
        tooltip="Each copy an instanceable reference to the source: light, and it follows the "
        "source, but nothing under a copy can be edited on its own. Off makes full copies.",
    ))

    copies = chunk.Group("copies_grp", "Copies")
    copies.add_params([
        chunk.Slider("copies", "Copies", min_value=1, max_value=100, default_value=1, is_int=True,
                     tooltip="How many copies. Above 1 they go under the destination, named after "
                     "the source: step_00, step_01, ..."),
        chunk.Vector("copy_translate", "Translate", param=chunk.Slider(), length=3, suffixes="XYZ",
                     default_value=(0.0, 0.0, 0.0), tooltip="Moved this much more for each copy."),
        chunk.Vector("copy_rotate", "Rotate", param=chunk.Slider(), length=3, suffixes="XYZ",
                     default_value=(0.0, 0.0, 0.0), tooltip="Turned this many degrees more for each copy."),
        chunk.Vector("copy_scale", "Scale", param=chunk.Slider(), length=3, suffixes="XYZ",
                     default_value=(1.0, 1.0, 1.0), tooltip="Scaled by this once more for each copy."),
        chunk.Vector("copy_pivot", "Pivot", param=chunk.Slider(), length=3, suffixes="XYZ",
                     default_value=(0.0, 0.0, 0.0), tooltip="The point copies rotate and scale around."),
    ])
    chunk_api.add_param(copies)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="cube",
        description="""
        Copies a prim N times in a row, ring or spiral (stairs); full copies or shared instances.

        Each copy is a deep copy of the prim and its full subtree, or with As Instances a
        light instance of it. One Duplicate Prim lays its copies along one line, ring or
        spiral; for a grid, duplicate the row it made with a second one.

        Parameters:
          - Source Prim: the prim to copy (supports viewport picking).
          - Destination Path: where to place the duplicate. If empty, auto-generates
            a unique path using the source name + '_copy' suffix.

        The copy is a true deep copy from the composed view:
          - All authored attributes with their values (default + time samples)
          - All metadata (kind, customData, documentation, etc.)
          - All relationships and their targets
          - All child prims (recursive full subtree)

        Handles name collisions by auto-incrementing a numeric suffix.
        All edits authored on the chunk's own layer (non-destructive).

        Copies [copies]: above 1, makes that many copies under the destination (an Xform),
        named after the source with an index -- step_00, step_01, ... Copy i is the source
        moved by the per-copy offset i times, so copy 0 sits on the source. The offset, in
        the copies' parent space: Translate [copy_translate], Rotate in degrees, X then Y
        then Z [copy_rotate], Scale [copy_scale], around Pivot [copy_pivot]. A row is a
        Translate; a ring a Rotate around the ring's centre; a spiral staircase both, e.g.
        Rotate Y 18 and Translate Y 0.2 for 20 copies. Each copy's transform is written as
        one matrix op.

        As Instances [as_instances]: each copy is an instanceable reference to the source
        rather than a deep copy. USD composes the source's subtree once and shares it, and
        the copies follow later changes to the source. Below a copy's root nothing can be
        edited on its own (instance proxies are read-only), and only children are shared,
        so instancing a leaf Mesh saves nothing -- a PointInstancer suits those.

        In the node graph: accepts prim input for source, outputs the new prim.
        """,
    )
