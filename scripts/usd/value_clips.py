"""Value Clips chunk -- stacks named clip sets on a target prim.

A clip set is one source of time-sampled data for a prim: a single clip file, or a
per-frame template such as `points.###.usd`. The chunk holds an ordered list of them and
authors USD's `clips` metadata plus the `clipSets` list-op that fixes their strength, so
a wave clip can sit *over* a walk cycle instead of alongside it. Without that list-op USD
consults clip sets alphabetically, which is nobody's intended order.

Stacking is what makes sparse clips useful. A clip authored as array edits -- OpenUSD
26.03's `edit [write q to [i]]` -- changes the elements it names and leaves the rest to
the next clip set down, so a layered performance can be exactly the joints it moves. See
`scripts/usd/test_value_clips/make_wave_clip.py` for a clip written that way.

Everything is authored on the chunk's own layer: absolute asset paths resolve from an
anonymous layer just fine, and two chunks on one prim then compose their clip sets
instead of overwriting each other's `clips` dictionary. The list-op is *prepended*, not
explicit, for the same reason: an explicit list in the stronger chunk's layer would
replace the weaker chunk's and drop its clip sets from resolution altogether.
"""

from __future__ import annotations

import glob
import logging
import os
import re

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_utils

logger = logging.getLogger(__name__)

_TEMPLATE_PADDING = re.compile(r"#+")


def _is_template(path: str) -> bool:
    """A clip path with `#` padding is a per-frame template, not a single file."""
    return bool(_TEMPLATE_PADDING.search(path))


def _resolve_clip_path(stage: Usd.Stage, path_str: str) -> str:
    """Resolve a clip asset path to be findable from the stage.

    Relative paths are resolved against the stage's root layer directory.
    If already absolute, returned as-is. Strips file: URI prefix if present.
    """
    if not path_str:
        return path_str

    # Strip file: or file:/ or file:// or file:/// URI prefix
    cleaned = path_str
    if cleaned.startswith("file:"):
        cleaned = cleaned[5:]
        while cleaned.startswith("/") and len(cleaned) > 1 and cleaned[1] == "/":
            cleaned = cleaned[1:]
        # On Windows, file:///C:/... becomes /C:/... — strip leading /
        if len(cleaned) > 2 and cleaned[0] == "/" and cleaned[2] == ":":
            cleaned = cleaned[1:]

    # If it's already absolute, use as-is
    if os.path.isabs(cleaned):
        return cleaned.replace("\\", "/")

    # Resolve relative to the root layer's directory
    root_layer = stage.GetRootLayer()
    if root_layer and root_layer.realPath:
        root_dir = os.path.dirname(root_layer.realPath)
        resolved = os.path.normpath(os.path.join(root_dir, cleaned))
        return resolved.replace("\\", "/")

    # Fallback: return as-is
    return cleaned.replace("\\", "/")


def _default_name(path: str) -> str:
    """A clip set name from the file: `HumanFemale.walk.usd` -> walk, `clip.###.usda` -> clip."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"\.?#+", "", stem)
    return re.sub(r"\W+", "_", stem.rsplit(".", 1)[-1]) or "clips"


def _clip_default_prim(path: str) -> str:
    """The clip file's defaultPrim as a path, when it names one."""
    if _is_template(path):
        return ""
    layer = Sdf.Layer.FindOrOpen(path)
    if layer and layer.defaultPrim:
        return "/" + layer.defaultPrim
    return ""


def _file_range(path: str) -> tuple[float, float] | None:
    """The frames a single clip file covers, from its metadata or failing that its samples."""
    layer = Sdf.Layer.FindOrOpen(path)
    if not layer:
        return None
    if layer.HasStartTimeCode() and layer.HasEndTimeCode():
        return layer.startTimeCode, layer.endTimeCode
    times = layer.ListAllTimeSamples()
    if not times:
        return None
    return min(times), max(times)


def _template_range(path: str) -> tuple[float, float] | None:
    """The frames a template covers, read off the files that exist on disk."""
    pattern = _TEMPLATE_PADDING.sub("*", path)
    prefix, suffix = re.split(_TEMPLATE_PADDING, path, maxsplit=1)
    frames = []
    for found in glob.glob(pattern):
        found = found.replace("\\", "/")
        middle = found[len(prefix):len(found) - len(suffix)] if suffix else found[len(prefix):]
        if middle.isdigit():
            frames.append(float(middle))
    if not frames:
        return None
    return min(frames), max(frames)


def _loop_times(start: float, end: float, clip_start: float, clip_end: float) -> list:
    """Clip `times` that play the clip from `start` and repeat it until `end`.

    A repeat is a jump discontinuity: two entries at the same stage time, the first
    landing on the clip's last frame and the second back on its first.
    """
    length = clip_end - clip_start
    times = [(start, clip_start)]
    at = start
    while True:
        times.append((at + length, clip_end))
        at += length
        if at >= end:
            return times
        times.append((at, clip_start))


def _author_file_set(clips: Usd.ClipsAPI, name: str, path: str, prim_path: str,
                     start: float, end: float, loop: bool) -> tuple[float, float] | None:
    span = _file_range(path)
    if span is None:
        logger.warning("Value Clips: %s has no time samples, skipping clip set %r", path, name)
        return None
    clip_start, clip_end = span
    if clip_end <= clip_start:
        logger.warning("Value Clips: %s is a single frame, skipping clip set %r", path, name)
        return None
    if end <= start:
        end = start + (clip_end - clip_start)
    clips.SetClipAssetPaths([Sdf.AssetPath(path)], name)
    clips.SetClipPrimPath(prim_path, name)
    clips.SetClipActive([(start, 0)], name)
    if loop:
        clips.SetClipTimes(_loop_times(start, end, clip_start, clip_end), name)
    else:
        clips.SetClipTimes([(start, clip_start), (start + clip_end - clip_start, clip_end)], name)
    return start, end


def _author_template_set(clips: Usd.ClipsAPI, name: str, path: str, prim_path: str,
                         start: float, end: float, stride: float) -> tuple[float, float] | None:
    if end <= start:
        span = _template_range(path)
        if span is None:
            logger.warning("Value Clips: no files match %s, skipping clip set %r", path, name)
            return None
        start, end = span
    clips.SetClipTemplateAssetPath(path, name)
    clips.SetClipTemplateStartTime(start, name)
    clips.SetClipTemplateEndTime(end, name)
    clips.SetClipTemplateStride(stride, name)
    clips.SetClipPrimPath(prim_path, name)
    # No `active`: with template metadata USD derives it, and ignores an authored one.
    return start, end


def _rows(node):
    """The clip set rows, strongest first, as dicts of plain values."""
    rows = node.get_param("clip_sets", chunk.is_inst(chunk.ListGroup))
    out = []
    for i in range(rows.size):
        def value(name):
            return rows.get_indexed_param(name, i).value

        path = str(value("clip_path") or "").strip()
        if not path:
            continue
        out.append({
            "name": (value("name") or "").strip() or _default_name(path),
            "path": path,
            "prim_path": (value("clip_prim_path") or "").strip(),
            "start": float(value("start_frame") or 0),
            "end": float(value("end_frame") or 0),
            "stride": float(value("stride") or 1.0),
            "loop": bool(value("loop")),
            "manifest": (value("manifest_path") or "").strip(),
        })
    return out


def _set_stage_frame_range(stage: Usd.Stage, start_frame: float, end_frame: float):
    """Widen the stage time code range, on the session layer, to cover the clips.

    Widen, never narrow: another Value Clips chunk or a solver bake may have set the
    range too, and each cook of this chunk must not undo theirs.

    Note: usdview's timeline widget only reads the time range at stage-open
    time. If the scene doesn't already have startTimeCode/endTimeCode, the
    timeline won't appear until the scene is reopened. For scenes that already
    have a time range, this updates it correctly.
    """
    if stage.HasAuthoredTimeCodeRange():
        start_frame = min(start_frame, stage.GetStartTimeCode())
        end_frame = max(end_frame, stage.GetEndTimeCode())
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(end_frame)


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_param = node.get_param("target_prim")
    if not primpath_param.value:
        return
    rows = _rows(node)
    if not rows:
        return

    covered = []
    for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
        prim = stage.OverridePrim(target_path)
        clips = Usd.ClipsAPI(prim)
        names = []
        for row in rows:
            name = row["name"]
            while name in names:
                name += "_"
            path = _resolve_clip_path(stage, row["path"])
            prim_path = row["prim_path"] or _clip_default_prim(path) or str(target_path)
            if _is_template(path):
                span = _author_template_set(clips, name, path, prim_path,
                                            row["start"], row["end"], row["stride"])
            else:
                span = _author_file_set(clips, name, path, prim_path,
                                        row["start"], row["end"], row["loop"])
            if span is None:
                continue
            if row["manifest"]:
                clips.SetClipManifestAssetPath(Sdf.AssetPath(_resolve_clip_path(stage, row["manifest"])), name)
            names.append(name)
            covered.append(span)
        # Rows are strongest-first, and so is the list-op. Prepended rather than explicit:
        # list-ops compose across layers, so a second chunk's sets on this prim slot in
        # after (or before) these instead of being replaced wholesale.
        order = Sdf.StringListOp()
        order.prependedItems = names
        clips.SetClipSets(order)

    if covered and node.get_param("set_stage_range").value:
        _set_stage_frame_range(stage, min(s for s, _ in covered), max(e for _, e in covered))


def main():
    chunk_api = chunk.Chunk("Value Clips")

    target_prim = chunk.LineEdit(
        "target_prim",
        "Target Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prim the clips animate (the static geo, or a SkelAnimation).",
    )
    chunk_api.add_param(target_prim)

    chunk_api.add_param(chunk.Checkbox(
        "set_stage_range",
        "Set Stage Range",
        default_value=True,
        tooltip="Widen the stage's start/end time codes to cover every clip set.",
    ))

    clip_sets = chunk.ListGroup("clip_sets", "Clip Sets (strongest first)")
    clip_sets.add_params([
        chunk.LineEdit(
            "name", "Name",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Clip set name. Empty: taken from the file name (HumanFemale.walk.usd -> walk).",
        ),
        chunk.FileBrowser(
            "clip_path", "Clip",
            extensions=[".usd", ".usda", ".usdc", ".usdz"],
            tooltip=(
                "One clip file, or a per-frame template with # padding:\n"
                "./clips/points.###.usd, C:/cache/sim.####.usdc\n"
                "Relative paths resolve against the stage's root layer directory."
            ),
        ),
        chunk.LineEdit(
            "clip_prim_path", "Clip Prim Path",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip=(
                "The prim inside the clip that maps onto the target.\n"
                "Empty: the clip file's defaultPrim, else the target prim's own path."
            ),
        ),
        chunk.Slider(
            "start_frame", "Start", default_value=1, is_int=True, min_value=0, max_value=100000,
            tooltip="Stage frame the clip begins on.",
        ),
        chunk.Slider(
            "end_frame", "End", default_value=0, is_int=True, min_value=0, max_value=100000,
            tooltip=(
                "Stage frame the clip set ends on. 0: the clip's own length\n"
                "(for a template, whatever frames exist on disk)."
            ),
        ),
        chunk.Checkbox(
            "loop", "Loop", default_value=False,
            tooltip="Repeat a single clip file until End. Templates play their frames as-is.",
        ),
        chunk.Slider(
            "stride", "Stride", default_value=1.0, min_value=0.01, max_value=1000.0,
            tooltip="Templates only: frame step between consecutive clip files.",
        ),
        chunk.LineEdit(
            "manifest_path", "Manifest",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Optional manifest listing the clipped attributes. Empty: USD generates one.",
        ),
    ])
    clip_sets.size = 1
    chunk_api.add_param(clip_sets)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.USD,
        description="""
        Value Clips — stacks one or more USD clip sets on a prim.

        Use cases: a sim cache split per frame applied onto static geometry; a
        walk cycle from one file; a sparse override clip (array edits) layered
        over it so only the joints it names change.

        Parameters:
          - Target Prim: the prim the clips animate. Patterns and collections
            work; each match gets the same clip sets.
          - Set Stage Range: widen the stage's time range (session layer) to
            cover every clip set.
          - Clip Sets (list, first row strongest):
              - Name: clip set name; empty derives it from the file name.
              - Clip: one file, or a `#`-padded per-frame template.
              - Clip Prim Path: prim inside the clip; empty uses the clip's
                defaultPrim, else the target path.
              - Start / End: stage frames. End 0 means the clip's own length
                (a template's: the frames found on disk).
              - Loop: repeat a single-file clip until End, as jump
                discontinuities in the clip `times`.
              - Stride: templates only.
              - Manifest: optional per-set manifest file.

        Authors, on the chunk's own layer, `clips[<name>]` per row (assetPaths /
        primPath / active / times for a file; templateAssetPath / templateStart /
        End / Stride for a template) and a prepended `clipSets` list-op in row
        order — that list-op is what puts the first row on top; without it USD
        orders clip sets alphabetically. Prepended so that a second chunk's list
        on the same prim composes with this one instead of replacing it.

        Within the layer stack, clips are weaker than local time samples and
        stronger than samples coming across references or variants. A clip set
        never falls through to the one below it at frames outside its range: it
        holds its edge value, so a partial override should be written as a clip
        that covers the whole range and is a no-op where it has nothing to say.
        """,
    )
