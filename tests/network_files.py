"""Saved networks of transforms, and `usdchunks run` on them, for headless tests.

Each transform moves one prim along z. Files are saved without cooking, so they hold no
bake: a transform's value reaches its prim only if the run cooked it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import (
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    export_chunks,
    get_chunk_prim_path,
    initialize as init_chunk_usd,
)

REPO = Path(__file__).resolve().parents[1]


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def wire(stage, upstream: str, downstream: str) -> None:
    """Wire upstream's exec output into downstream's exec input (chunk prim paths)."""
    prim = stage.GetPrimAtPath(Sdf.Path(downstream))
    attr = prim.GetAttribute(_EXEC_INPUT) or prim.CreateAttribute(
        _EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
    )
    attr.SetConnections([Sdf.Path(f"{upstream}.{_EXEC_OUTPUT}")])


def save_network(
    path: Path,
    chain: dict[str, tuple[str, float]],
    ends: tuple[str, ...] = (),
    loose: dict[str, tuple[str, float]] | None = None,
    view_on: str | None = None,
) -> dict[str, str]:
    """A file holding one chain of transforms, saved uncooked.

    Args:
        path: Where to save it.
        chain: {chunk name: (prim it moves, tz)}, in chain order.
        ends: Terminal modules ("view", "output") wired to the chain's end, in that order.
        loose: Transforms wired to nothing.
        view_on: Also add an active view (slot 1) wired to this chain chunk.

    Returns:
        {chunk name: its chunk prim path}, the ends under their module names.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    init_chunk_usd(stage, scene)

    def make(module_name, node_name):
        mod = registry.get_chunk_module(module_name)
        chunks = mod.create() if mod.loaded else mod.load()
        node = chunks[0]
        node.name = node_name
        for c in chunks:
            scene.add_chunk(c)
        return get_chunk_prim_path(node), node

    def transform(name, prim_path, tz):
        made = make("transform", name)
        made[1].get_param("primpath_location").value = prim_path
        made[1].get_param("tz").value = tz
        return made

    paths = {}
    order = []
    for name, (prim_path, tz) in chain.items():
        paths[name], _ = transform(name, prim_path, tz)
        order.append(paths[name])
    for name, (prim_path, tz) in (loose or {}).items():
        paths[name], _ = transform(name, prim_path, tz)
    for module_name in ends:
        paths[module_name], _ = make(module_name, f"{path.stem}_{module_name}")
    for upstream, downstream in zip(order, order[1:]):
        wire(stage, upstream, downstream)
    for module_name in ends:
        wire(stage, order[-1], paths[module_name])
    view_paths = [paths["view"]] if "view" in ends else []
    if view_on:
        paths["view_on"], _ = make("view", f"{path.stem}_view_on")
        wire(stage, paths[view_on], paths["view_on"])
        view_paths.append(paths["view_on"])
    for slot, view_path in enumerate(reversed(view_paths), start=1):
        stage.GetPrimAtPath(view_path).CreateAttribute("view:slot", Sdf.ValueTypeNames.Int).Set(slot)
    if view_paths:
        root = stage.GetPrimAtPath(Sdf.Path(view_paths[0]).GetParentPath())
        root.CreateAttribute("chunk:activeView", Sdf.ValueTypeNames.Int, custom=True).Set(1)
    export_chunks(scene, str(path))
    for c in list(scene.get_chunks()):
        c.request_delete()
    return paths


def save_cubes(path: Path, names: str) -> None:
    """A layer of cubes at the origin, one per letter in names."""
    layer = Sdf.Layer.CreateNew(str(path))
    stage = Usd.Stage.Open(layer)
    for name in names:
        UsdGeom.Cube.Define(stage, f"/{name}")
    layer.Save()


def run_and_read(usda: Path, names: str) -> dict[str, float]:
    """`usdchunks run usda --export`, and each named prim's z in the result."""
    out = usda.parent / "out.usda"
    cmd = [sys.executable, "-m", "usd_chunks.cli", "run", str(usda), "--export", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    assert result.returncode == 0, result.stdout + result.stderr
    stage = Usd.Stage.Open(str(out))
    z = {}
    for name in names:
        xform = UsdGeom.Xformable(stage.GetPrimAtPath(f"/{name}"))
        z[name] = xform.ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation()[2]
    return z
