"""Value Clips (Template) chunk — sets up USD Value Clips on a target prim.

Primary use case: you have animated data in separate USD files (e.g. a sim cache
with per-frame point positions) and want to apply that animation onto a static
prim already in the scene.

This chunk authors the template-based clip metadata set on the target prim,
enabling the stage to resolve attribute values from clip files at each frame.

The template approach (clipTemplateAssetPath) is the most common for sim caches.
"""

from __future__ import annotations

import os
import re

from pxr import Usd, Sdf, Vt, Gf

from usd_chunks import chunk, chunk_utils


def _validate_template(template: str) -> bool:
    """Validate that the template has '#' padding characters for frame numbers."""
    return bool(re.search(r"#+", template))


def _resolve_clip_path(stage: Usd.Stage, path_str: str) -> str:
    """Resolve a clip asset path to be findable from the stage.

    Relative paths are resolved against the stage's root layer directory.
    If already absolute, returned as-is.
    """
    if not path_str:
        return path_str

    # If it's already absolute, use as-is
    if os.path.isabs(path_str):
        return path_str.replace("\\", "/")

    # Resolve relative to the root layer's directory
    root_layer = stage.GetRootLayer()
    if root_layer and root_layer.realPath:
        root_dir = os.path.dirname(root_layer.realPath)
        # For templates with #, we can't use os.path.abspath directly on them
        # but we can prepend the root dir
        resolved = os.path.normpath(os.path.join(root_dir, path_str))
        return resolved.replace("\\", "/")

    # Fallback: return as-is (will likely fail for anonymous layers)
    return path_str.replace("\\", "/")


def _make_active_clips(start: float, end: float):
    """Create the 'active' metadata: list of (time, clip_index) pairs.

    For template clips, this is simply [[startTime, 0], [endTime, 0]]
    indicating the single clip set is active for the entire range.
    """
    return Vt.Vec2dArray([Gf.Vec2d(start, 0), Gf.Vec2d(end, 0)])


def _set_stage_frame_range(stage: Usd.Stage, start_frame: float, end_frame: float):
    """Set the stage time code range on the session layer so usdview shows the timeline."""
    session = stage.GetSessionLayer()
    session.startTimeCode = start_frame
    session.endTimeCode = end_frame


def _author_clip_metadata(stage: Usd.Stage, prim: Usd.Prim,
                          template_path: str, start_frame: float,
                          end_frame: float, stride: float,
                          clip_prim_path: str, manifest_path: str = ""):
    """Author all value clip template metadata on the target prim."""
    if not prim.IsValid():
        return

    # Resolve paths so they work from any layer (including anonymous chunk layers)
    resolved_template = _resolve_clip_path(stage, template_path)
    resolved_manifest = _resolve_clip_path(stage, manifest_path) if manifest_path else ""

    # USD Value Clips are authored as metadata on the prim.
    clip_set_name = "default"

    # Get or create the clips metadata dictionary
    clips = prim.GetMetadata("clips") or {}
    clips = dict(clips)

    clip_set = clips.get(clip_set_name, {})
    clip_set = dict(clip_set) if clip_set else {}

    # Template asset path — the pattern with # for frame numbers
    clip_set["templateAssetPath"] = resolved_template

    # Frame range
    clip_set["templateStartTime"] = float(start_frame)
    clip_set["templateEndTime"] = float(end_frame)
    clip_set["templateStride"] = float(stride)

    # The prim path inside the clip files that maps to this prim
    clip_set["primPath"] = clip_prim_path

    # Manifest (optional) — identifies which attributes are clipped
    if resolved_manifest:
        clip_set["manifestAssetPath"] = Sdf.AssetPath(resolved_manifest)

    # Active — the time range for which this clip set is active
    clip_set["active"] = _make_active_clips(start_frame, end_frame)

    clips[clip_set_name] = clip_set
    prim.SetMetadata("clips", clips)

    # Also set the stage frame range so the timeline is visible
    _set_stage_frame_range(stage, start_frame, end_frame)


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_param = node.get_param("target_prim")
    template_path = node.get_param("template_path").value or ""
    start_frame = node.get_param("start_frame").value
    end_frame = node.get_param("end_frame").value
    stride = node.get_param("stride").value
    clip_prim_path = node.get_param("clip_prim_path").value or ""
    manifest_path = node.get_param("manifest_path").value or ""

    if not primpath_param.value or not template_path:
        return

    if not _validate_template(template_path):
        return

    for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            prim = stage.OverridePrim(target_path)

        _author_clip_metadata(
            stage, prim, template_path,
            start_frame, end_frame, stride,
            clip_prim_path or target_path,
            manifest_path,
        )


def _on_change(owner, notice, sender):
    """Callback for the chunk panel — author clip metadata."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage or not _chunk:
        return

    prim_param = _chunk.get_param("target_prim")
    template_path = _chunk.get_param("template_path").value or ""
    start_frame = _chunk.get_param("start_frame").value
    end_frame = _chunk.get_param("end_frame").value
    stride = _chunk.get_param("stride").value
    clip_prim_path = _chunk.get_param("clip_prim_path").value or ""
    manifest_path = _chunk.get_param("manifest_path").value or ""

    prims = prim_param.get_prims()
    if not prims or not template_path:
        return

    if not _validate_template(template_path):
        return

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    for prim in prims:
        prim_path = str(prim.GetPath())
        _author_clip_metadata(
            stage, prim, template_path,
            start_frame, end_frame, stride,
            clip_prim_path or prim_path,
            manifest_path,
        )


def main():
    chunk_api = chunk.Chunk("Value Clips")

    target_prim = chunk.LineEdit(
        "target_prim",
        "Target Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prim to apply value clips to (the static geo in the scene).",
    )
    target_prim.add_callback(_on_change)
    chunk_api.add_param(target_prim)

    template_path = chunk.LineEdit(
        "template_path",
        "Clip Template",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip=(
            "Template asset path with # padding for frame numbers.\n"
            "e.g. ./clips/points.###.usd or C:/cache/sim.####.usdc\n"
            "Relative paths are resolved against the stage's root layer directory."
        ),
    )
    template_path.add_callback(_on_change)
    chunk_api.add_param(template_path)

    clip_prim_path = chunk.LineEdit(
        "clip_prim_path",
        "Clip Prim Path",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip=(
            "Which prim path inside the clip files maps to the target prim.\n"
            "If empty, uses the target prim's own path."
        ),
    )
    clip_prim_path.add_callback(_on_change)
    chunk_api.add_param(clip_prim_path)

    # Frame range group
    frame_grp = chunk.Group(
        "frame_range",
        "Frame Range",
        flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED,
    )

    start_frame = chunk.Spinbox(
        "start_frame",
        "Start",
        default_value=1.0,
        min_value=0.0,
        max_value=100000.0,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="First frame of the clip range.",
    )
    start_frame.add_callback(_on_change)
    frame_grp.add_param(start_frame)

    end_frame = chunk.Spinbox(
        "end_frame",
        "End",
        default_value=100.0,
        min_value=0.0,
        max_value=100000.0,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Last frame of the clip range.",
    )
    end_frame.add_callback(_on_change)
    frame_grp.add_param(end_frame)

    stride = chunk.Spinbox(
        "stride",
        "Stride",
        default_value=1.0,
        min_value=0.01,
        max_value=1000.0,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Frame step between consecutive clip files (usually 1.0).",
    )
    stride.add_callback(_on_change)
    frame_grp.add_param(stride)

    chunk_api.add_param(frame_grp)

    # Optional manifest
    manifest_path = chunk.LineEdit(
        "manifest_path",
        "Manifest",
        default_value="",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip=(
            "Optional: path to manifest file listing which attributes are clipped.\n"
            "e.g. ./clips/manifest.usda\n"
            "If not provided, USD determines clipped attributes automatically."
        ),
    )
    manifest_path.add_callback(_on_change)
    chunk_api.add_param(manifest_path)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Core USD",
        description="""
        Value Clips (Template) — applies template-based USD Value Clips to a prim.

        Use case: animated point data in separate USD files (sim cache, alembic
        export split per-frame) applied onto a static geometry in the scene.

        Parameters:
          - Target Prim: the prim to apply clips to (static geo in scene).
          - Clip Template: template asset path with # padding for frame numbers,
            e.g. ./clips/points.###.usd (### = 3-digit zero-padded frame).
            Relative paths resolved against the root layer directory.
          - Clip Prim Path: which prim in the clip files maps to the target.
            If empty, uses the target prim's own path.
          - Frame Range: start, end, stride for the clip sequence.
            Also sets the stage time range on the session layer.
          - Manifest: optional manifest file path (speeds up attribute resolution).

        Authors the following metadata on the target prim:
          - clips["default"].templateAssetPath (resolved to absolute)
          - clips["default"].templateStartTime
          - clips["default"].templateEndTime
          - clips["default"].templateStride
          - clips["default"].primPath
          - clips["default"].manifestAssetPath (if provided, resolved)
          - clips["default"].active

        All edits go to the chunk's own layer (non-destructive).
        Stage frame range set on the session layer for timeline visibility.
        """,
    )
