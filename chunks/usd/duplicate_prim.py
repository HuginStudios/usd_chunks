"""Duplicate Prim chunk -- deep copies a prim (full subtree) to a new path.

Copies all attributes, metadata, and children from the source prim's composed
view to the destination path. The copy is authored on the chunk's own layer.

Handles name collisions by auto-incrementing a numeric suffix.
"""

from __future__ import annotations

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_utils, sdf_authoring


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

    _copy_prim_recursive(stage, source_prim, dest_path)


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

    _copy_prim_recursive(stage, source_prim, dest_path)


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

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        description="""
        Duplicate Prim — deep copies a prim and its full subtree to a new path.

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

        In the node graph: accepts prim input for source, outputs the new prim.
        """,
    )
