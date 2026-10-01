"""Batched sweeps: cook every variant, stitch them into frames, commit once.

`usdchunks sweep` used to run each variant as its own `usdchunks run`: start Python, load
the network, cook, commit. For a network that renders, commit is where the time went --
a renderer session brought up per variant, a dataset writer importing its libraries per
variant -- and the load before it was paid again every time. On the KITTI dataset network
that was 26 s per variant, of which the render itself was one or two.

A batch pays each of those once:

1. **Prepare.** Load the network once and, per variant, set the wedge's index, let the
   change cook, and export the variant -- the same `variant_NNNNN/variant.usda` a
   per-variant sweep writes, byte for byte.
2. **Stitch.** Merge the variants into one layer where variant i is frame i: every
   attribute that differs between them gets a time sample per variant. Only attribute
   values can vary over time, so anything else that differs -- a relationship, a prim's
   type, a prim present in some variants only -- makes the sweep fall back to one process
   per variant, and says why.
3. **Commit.** Put the stitched layer on top of the live stage and run the commit hooks
   once, over every frame. A renderer renders frame after frame in one session; a writer
   writes them in one pass.

That last step only works for commit hooks that work per frame: those of chunks whose
Frames param follows the wedge's Index, which is how a network says "each variant is one
frame" (Render, KITTI Writer). Any other commit -- Export USD writes one file per variant --
cannot be shared, and the sweep falls back.
"""

from __future__ import annotations

import os

from pxr import Sdf, Usd

from usd_chunks import chunk_scheduler
from usd_chunks.chunk_scene import all_chunks
from usd_chunks.chunk_stage import flatten_without_chunks
from usd_chunks.chunk_stage import commit_order_from_stage, recook_focus

#: The wedge param a sweep drives.
INDEX_PARAM = "current_index"

#: The file the stitched variants are written to, beside the variant directories.
STITCHED_NAME = "stitched.usda"


def variant_path(out: str, index: int) -> str:
    return os.path.join(out, f"variant_{index:05d}", "variant.usda")


def frame_consumers(chunks, wedge_name: str) -> tuple[list, str | None]:
    """The chunks whose commit works per frame, or why the network cannot be batched.

    Args:
        chunks: Every chunk in the network, sub-chunks included.
        wedge_name: The name of the wedge chunk the sweep drives.

    Returns:
        (consumers, None) when every commit hook can run once over all frames, or
        ([], reason) naming the first chunk whose commit has to run once per variant.
    """
    consumers = []
    seen = set()
    follows = f"@{wedge_name}.{INDEX_PARAM}@"
    for c in chunks:
        # A chunk inside a chunkgroup is reached both directly and through the group.
        if id(c) in seen:
            continue
        seen.add(id(c))
        if c._module_hook("commit") is None or c.is_blocked():
            continue
        frames = c.get_param("frames")
        raw = getattr(frames, "raw_value", None) if frames is not None else None
        if not (isinstance(raw, str) and raw.strip() == follows):
            return [], (f"{c.name} commits once per variant: it has no Frames following "
                        f"{wedge_name}'s Index")
        consumers.append(c)
    return consumers, None


def prepare(scene, wedge, indices, out, view=None) -> list[int]:
    """Cook and export each variant in turn, in this one process.

    What `usdchunks run` does per variant, minus its commit: set the index, recook the
    active view if the network has one, export. A change after the first only cooks what
    it reaches, so a variant costs a fraction of a second rather than a process start and
    a load.

    Args:
        scene: The loaded network.
        wedge: The wedge chunk.
        indices: The variants to prepare.
        out: The sweep's output directory.
        view: The network's active view, or None.

    Returns:
        The indices that failed.
    """
    stage = scene.get_stage()
    index = wedge.get_param(INDEX_PARAM)
    failed = []
    for i in indices:
        try:
            index.value = i
            if view is not None:
                recook_focus(stage, scene, None, view)
            path = variant_path(out, i)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            flatten_without_chunks(stage).Export(path)
        except Exception as exc:  # noqa: BLE001 - one bad variant must not stop the rest
            print(f"  variant_{i:05d} FAILED: {exc}")
            failed.append(i)
        else:
            print(f"  variant_{i:05d}", flush=True)
    return failed


def _specs(layer) -> dict:
    found = {}

    def visit(path):
        # Target and connection paths come up too, but are not specs of their own: the
        # owner's targetPaths/connectionPaths field carries them and is compared there.
        if not path.IsTargetPath():
            found[path] = layer.GetObjectAtPath(path)

    layer.Traverse(layer.pseudoRoot.path, visit)
    return found


def _differs(values) -> bool:
    return any(v != values[0] for v in values[1:])


def stitch(paths: dict, out_path: str) -> list[str]:
    """Write one layer where variant i is frame i.

    Every attribute whose value differs between the variants gets a time sample per
    variant at its index; everything else is written once, as it is in every variant.

    Args:
        paths: Variant index -> exported variant file.
        out_path: Where to write the stitched layer.

    Returns:
        What could not be stitched, one line each. Empty on success, and only then is
        out_path written.
    """
    indices = sorted(paths)
    layers = [Sdf.Layer.FindOrOpen(paths[i]) for i in indices]
    all_specs = [_specs(layer) for layer in layers]
    problems = []
    varying = []

    for path in sorted(set().union(*all_specs), key=str):
        present = [s.get(path) for s in all_specs]
        missing = [i for i, p in zip(indices, present) if p is None]
        if missing:
            problems.append(f"{path} exists in some variants only (not in {missing[:5]})")
            continue
        keys = set().union(*(p.ListInfoKeys() for p in present))
        is_attr = isinstance(present[0], Sdf.AttributeSpec)
        if is_attr:
            keys.discard("default")
            if any(p.layer.ListTimeSamplesForPath(path) for p in present):
                problems.append(f"{path} is already animated")
                continue
        for key in sorted(keys):
            if _differs([p.GetInfo(key) if p.HasInfo(key) else None for p in present]):
                problems.append(f"{path} {key} differs between variants")
        if is_attr:
            values = [p.default if p.HasInfo("default") else None for p in present]
            if _differs(values):
                varying.append((path, values))

    if problems:
        return problems

    stitched = Sdf.Layer.CreateNew(out_path)
    stitched.TransferContent(layers[0])
    for path, values in varying:
        stitched.GetAttributeAtPath(path).ClearInfo("default")
        for i, value in zip(indices, values):
            stitched.SetTimeSample(path, i, value)
    stitched.startTimeCode, stitched.endTimeCode = indices[0], indices[-1]
    stitched.Save()
    return []


def commit_frames(scene, consumers, stitched_path: str, indices) -> list:
    """Run every per-frame commit once, over every variant as a frame.

    The stitched layer goes on top of the session layer's sublayers, above the layers the
    chunks cooked into, so the live stage shows variant i at time i. Held interpolation,
    so a delegate sampling between two frames sees one variant, never a blend of two.

    Args:
        scene: The loaded network.
        consumers: The chunks from frame_consumers.
        stitched_path: The stitched layer.
        indices: The frames to commit.

    Returns:
        The chunks whose commit ran.
    """
    stage = scene.get_stage()
    stage.GetSessionLayer().subLayerPaths.insert(0, os.path.abspath(stitched_path))
    stage.SetInterpolationType(Usd.InterpolationTypeHeld)
    frames = ",".join(str(i) for i in indices)
    for c in consumers:
        c.get_param("frames").value = frames
    return chunk_scheduler.commit_all(
        consumers,
        order=lambda cs: commit_order_from_stage(cs, stage),
    )


def find_wedge(scene, name: str):
    return next((c for c in all_chunks(scene.get_chunks()) if c.name == name), None)
