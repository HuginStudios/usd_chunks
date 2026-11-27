"""A reopened network of MaterialX Shaders cooks the same shaders it did when it was built.

The shader inputs are lofted from the nodedef in a CHUNK_LOADED callback, after the load
has already cooked the chunk. That first cook found no params and authored bare shaders,
and nothing cooked them again: a wood material opened as grey standard_surface defaults.
"""

from __future__ import annotations

import pytest
from pxr import UsdShade

from usd_chunks import chunk_stage
from usd_chunks.chunk_script import Scene
from usd_chunks.graph import engine as chunk_graph

pytest.importorskip("pxr.UsdMtlx")


def _inputs(stage, path):
    shader = UsdShade.Shader(stage.GetPrimAtPath(path))
    assert shader, f"no shader at {path}"
    # What a renderer reads: the shader output an input is wired to, or the value it
    # resolves to -- through the connection to the chunk prim's storage that drives it.
    out = {}
    for inp in shader.GetInputs():
        producers = inp.GetValueProducingAttributes()
        attr = producers[0] if producers else inp.GetAttr()
        out[inp.GetBaseName()] = str(attr.GetPath()) if UsdShade.Output(attr) else attr.Get()
    return out


def _wire(scene, src, dst, input_name):
    """src.outputs:out -> dst.inputs:<input_name>, as the node graph wires it."""
    stage = scene.get_stage()
    chunks = {chunk_stage.get_chunk_prim_path(c): c for c in scene.get_chunks()}
    sp, dp = chunk_stage.get_chunk_prim_path(src), chunk_stage.get_chunk_prim_path(dst)
    param = dst.get_param(input_name)
    out_attr = stage.GetPrimAtPath(sp).GetAttribute("outputs:out")
    a = chunk_graph.PortRef(sp, f"{sp}.outputs:out", chunk_graph.sdf_to_port_type(out_attr.GetTypeName()), False)
    b = chunk_graph.PortRef(dp, f"{dp}.inputs:{input_name}", chunk_graph.param_port_type(param), True, param=param)
    assert chunk_graph.connect(stage, a, b, chunks) is not None


def test_reopened_shaders_keep_their_inputs_and_wires(tmp_path):
    path = str(tmp_path / "net.usda")
    with Scene() as scene:
        position = scene.add("mtlx_shader", nodedef="ND_position_vector3")
        offset = scene.add("mtlx_shader", position, nodedef="ND_add_vector3")
        offset.get_param("in2").value = (0.0, 1.5, 9.0)
        _wire(scene, position, offset, "in1")
        add_path = offset.get_param("prim_path").value
        position_out = f"{position.get_param('prim_path').value}.outputs:out"
        built = _inputs(scene.get_stage(), add_path)
        scene.save(path)

    assert built["in2"] == pytest.approx((0.0, 1.5, 9.0))
    assert built["in1"] == position_out

    with Scene.open(path) as scene:
        reopened = _inputs(scene.get_stage(), add_path)

    assert reopened == built


def test_a_vector_input_set_after_reopening_sticks(tmp_path):
    """A MaterialX Shader's vector inputs are made at load; the first edit to one bound it to
    its storage then and read the old saved value back over the edit -- in2 = (400, 3, 3) on
    a reopened network left (160, 3, 3). Now the load binds it, and the edit stays."""
    first, second = str(tmp_path / "a.usda"), str(tmp_path / "b.usda")
    with Scene() as scene:
        mul = scene.add("mtlx_shader", nodedef="ND_multiply_vector3")
        mul.get_param("in2").value = (1.0, 2.0, 3.0)
        mul_path = mul.get_param("prim_path").value
        scene.save(first)

    with Scene.open(first) as scene:
        mul = next(c for c in scene.get_chunks() if c.get_param("nodedef") is not None)
        mul.get_param("in2").value = (7.0, 8.0, 9.0)
        assert tuple(mul.get_param("in2").value) == pytest.approx((7.0, 8.0, 9.0))
        assert tuple(_inputs(scene.get_stage(), mul_path)["in2"]) == pytest.approx((7.0, 8.0, 9.0)), "the shader too"
        scene.save(second)

    with Scene.open(second) as scene:
        mul = next(c for c in scene.get_chunks() if c.get_param("nodedef") is not None)
        assert tuple(mul.get_param("in2").value) == pytest.approx((7.0, 8.0, 9.0)), "and the file"
