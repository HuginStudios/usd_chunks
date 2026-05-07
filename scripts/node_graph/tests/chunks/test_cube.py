"""Minimal test chunk — creates a single Xform prim at a configurable path."""
from usd_chunks import chunk
from pxr import Usd, UsdGeom


def _execute(owner, notice, sender):
    stage = owner.get_stage()
    if not stage:
        return
    chunk_obj = owner.get_chunk()
    if not chunk_obj:
        return
    prim_path = (chunk_obj.get_param("prim_path").value or "").strip() or "/TestCube"
    layer = chunk_obj.get_edit_context()
    if layer:
        with Usd.EditContext(stage, layer):
            UsdGeom.Xform.Define(stage, prim_path)
    else:
        UsdGeom.Xform.Define(stage, prim_path)


def main():
    c = chunk.Chunk("TestCube")
    c.add_param(chunk.LineEdit("prim_path", default_value="/TestCube"))
    btn = chunk.Button("execute")
    btn.add_callback(_execute)
    c.add_param(btn)
    return c


def config():
    return chunk.DefaulConfig()
