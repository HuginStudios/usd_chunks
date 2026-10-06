"""Write a chunkgroup spec as a usd_chunks network file. Run as a separate process::

    <python> -m usd_chunks.hosts.houdini.export_worker spec.json network.usda

chunkgroup.export collects a Houdini chunkgroup into a plain spec (JSON) and runs this
under chunk_process.python_executable -- Houdini's own Python, never houdini.exe. It is a
process of its own because it builds the network with the scripting API (Scene.new), and
a scene of its own inside Houdini would take the chunk registry from the chunk nodes that
are live there. Built that way, the file is written by the same serializer Qt saves with,
so it opens in usdview, Blender and the CLI as any saved network does.

Imports no hou: Houdini's pxr is imported through the plain interpreter.

The spec, one dict per chunkgroup::

    {"name": "ChunkGroup", "output": "PreviewSurface", "params": {"SwedishHouse_floors": 1},
     "up_axis": "Z", "meters_per_unit": 0.01,
     "nodes": [{"kind": "chunk", "name": "SwedishHouse", "type": "house", "pos": [x, y],
                "inputs": ["AddMesh"], "values": {"footprint_grp.width": 5.6},
                "lists": {"imports": {"size": 2, "rows": {"sublayer_path": ["a", "b"]}}},
                "wires": {"primpath_location": ["AddMesh", "prim_path"]},
                "lofts": {"footprint_grp.floors": "SwedishHouse_floors"}},
               {"kind": "group", ...a nested chunkgroup spec..., "inputs": [...]}]}
"""
from __future__ import annotations

import json
import sys

from pxr import Sdf, UsdGeom

from usd_chunks import Scene, chunk_core, chunk_stage
from usd_chunks.chunk_naming import CHUNKS_ROOT

_POS = "ui:nodegraph:node:pos"


def _value(v):
    return tuple(v) if isinstance(v, list) else v


def _set_values(made, node: dict) -> None:
    for path, value in node.get("values", {}).items():
        if (param := made.get_param(path)) is not None:
            param.value = _value(value)
    for path, data in node.get("lists", {}).items():
        list_group = made.get_param(path)
        if not isinstance(list_group, chunk_core.ChunkListGroupBase):
            continue
        list_group.size = int(data["size"])
        for template, values in data.get("rows", {}).items():
            for i, value in enumerate(values[: list_group.size]):
                if (proxy := list_group.get_indexed_param(template, i)) is not None:
                    proxy.value = _value(value)


def _build(scene, spec: dict, group, made: dict) -> None:
    """Make spec's nodes inside group, then wire them, loft its public params, view it."""
    own: dict[str, object] = {}
    with group:
        for node in spec["nodes"]:
            inputs = [own[i] for i in node.get("inputs", []) if i in own]
            if node["kind"] == "group":
                inner = scene.add("chunkgroup", *inputs, name=node["name"])
                _build(scene, node, inner, made)
                own[node["name"]] = inner
            else:
                chunk = scene.add(node["type"], *inputs, name=node["name"])
                _set_values(chunk, node)
                own[node["name"]] = chunk
            made[id(node)] = own[node["name"]]

        for node in spec["nodes"]:
            chunk = own[node["name"]]
            for path, (source, source_path) in node.get("wires", {}).items():
                param, src = chunk.get_param(path), own.get(source)
                if param is not None and src is not None and src.get_param(source_path) is not None:
                    param.connect_value(src.get_param(source_path))
            for path, public in node.get("lofts", {}).items():
                if (param := chunk.get_param(path)) is None:
                    continue
                if public in spec.get("params", {}):
                    param.value = _value(spec["params"][public])
                chunk_stage.loft_param_to_context(param, group)

        if (out := own.get(spec.get("output") or "")) is not None:
            scene.view(out)


def _place(layer: Sdf.Layer, prim_path: Sdf.Path, spec: dict) -> None:
    """Record each node's Houdini position on its prim, as the node graph does."""
    for node in spec["nodes"]:
        path = prim_path.AppendChild(node["name"])
        prim = layer.GetPrimAtPath(path)
        if prim is None:
            continue
        if "pos" in node:
            attr = prim.attributes.get(_POS) or Sdf.AttributeSpec(
                prim, _POS, Sdf.ValueTypeNames.Float2, Sdf.VariabilityUniform)
            attr.default = tuple(node["pos"])
        if node["kind"] == "group":
            _place(layer, path, node)


def export(spec: dict, path: str) -> str:
    """Build spec as a network and save it to path. Returns the group's prim path."""
    with Scene.new() as scene:
        # The network's own space, before anything cooks: the chunks build in it, and the
        # file records it for whatever opens it next (see hosts/houdini/space.py).
        stage = scene.get_stage()
        if spec.get("up_axis") in ("Y", "Z"):
            UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if spec["up_axis"] == "Y" else UsdGeom.Tokens.z)
        if spec.get("meters_per_unit"):
            UsdGeom.SetStageMetersPerUnit(stage, float(spec["meters_per_unit"]))
        group = scene.peek_root_context() or scene.get_root_context()
        group.name = spec["name"]
        made: dict = {}
        _build(scene, spec, group, made)
        prim_path = chunk_stage.get_chunk_prim_path(group) or f"{CHUNKS_ROOT}/{spec['name']}"
        scene.save(path)
    layer = Sdf.Layer.FindOrOpen(path)
    layer.Reload()
    _place(layer, Sdf.Path(prim_path), spec)
    layer.Save()
    return prim_path


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as f:
        spec = json.load(f)
    print("USD_CHUNKS_EXPORTED", export(spec, sys.argv[2]))


if __name__ == "__main__":
    main()
