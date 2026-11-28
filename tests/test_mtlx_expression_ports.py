"""A script wires one MaterialX node's output into another's input with an expression, `@Node.out@`.

A shader node's outputs are ports, not params, so nothing could name them: `@cc.out@` warned
"Expression ref not found", and the connection it did author landed on the grouped
inputs:Base:base_color, which the shader wiring didn't read. The surface kept its plain value, so
from a script there was no way to tint a texture with Color Correct short of writing USD by hand.
"""
from __future__ import annotations

import logging

import pytest
from pxr import Usd, UsdShade

from usd_chunks import Scene, shader_defs

pytestmark = pytest.mark.skipif(
    "mtlx" not in shader_defs.available_source_types(),
    reason="this USD has no MaterialX shader definitions (no usdMtlx plugin)",
)


def _source(stage, shader_path, input_name):
    """(source prim path, output name) an input of a real shader is connected to, or None."""
    shader = UsdShade.Shader(stage.GetPrimAtPath(shader_path))
    sources = shader.GetInput(input_name).GetConnectedSources()[0]
    return (str(sources[0].source.GetPath()), sources[0].sourceName) if sources else None


def test_an_expression_wires_a_node_output_into_a_surface_input(caplog):
    with Scene(Usd.Stage.CreateInMemory(), "pytest") as scene:
        cc = scene.create.mtlx_shader(nodedef="ND_colorcorrect_color3", prim_path="/M/ng/cc", gain=1.5)
        surface = scene.create.standard_surface(cc, prim_path="/M/mat", bind_to="")
        with caplog.at_level(logging.WARNING):
            scene.set(surface, base_color=f"@{cc.name}.out@")
            scene.cook(surface)
        assert _source(scene.get_stage(), "/M/mat/shader", "base_color") == ("/M/ng/cc", "out")
        assert "not found" not in caplog.text            # a port is not a dangling ref


def test_the_wire_can_be_given_when_the_surface_is_made():
    with Scene(Usd.Stage.CreateInMemory(), "pytest") as scene:
        cc = scene.create.mtlx_shader(nodedef="ND_colorcorrect_color3", prim_path="/M/ng/cc")
        scene.create.standard_surface(cc, prim_path="/M/mat", bind_to="", base_color=f"@{cc.name}.out@")
        assert _source(scene.get_stage(), "/M/mat/shader", "base_color") == ("/M/ng/cc", "out")


def test_a_misspelt_port_still_says_it_is_not_found(caplog):
    with Scene(Usd.Stage.CreateInMemory(), "pytest") as scene:
        cc = scene.create.mtlx_shader(nodedef="ND_colorcorrect_color3", prim_path="/M/ng/cc")
        surface = scene.create.standard_surface(cc, prim_path="/M/mat", bind_to="")
        with caplog.at_level(logging.WARNING):
            scene.set(surface, base_color=f"@{cc.name}.output_typo@")
        assert "not found" in caplog.text
