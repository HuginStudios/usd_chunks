"""A MaterialX Shader is named after its shader, numbered cleanly, unless someone named it.

Two bugs, found building a 1000-node shader network from a script: name="Bark" was replaced
by the shader's name as soon as Node Definition was set, so scene.get_chunk("Bark") found
nothing; and the second constant_color3 was called constant_color3_ (a bare underscore).
"""

from __future__ import annotations

import pytest

from usd_chunks import Scene

pytest.importorskip("pxr.UsdMtlx")


def test_copies_are_numbered_from_one():
    with Scene() as scene:
        names = [scene.add("mtlx_shader", nodedef="ND_constant_color3").name for _ in range(3)]
        surfaces = [scene.add("mtlx_shader", nodedef="ND_standard_surface_surfaceshader").name for _ in range(2)]
    assert names == ["constant_color3", "constant_color3_1", "constant_color3_2"]
    assert surfaces == ["standard_surface_surfaceshader", "standard_surface_surfaceshader1"]


def test_a_given_name_is_kept():
    with Scene() as scene:
        bark = scene.add("mtlx_shader", name="Bark", nodedef="ND_constant_color3")
        assert bark.name == "Bark"
        assert scene.get_chunk("Bark") is bark
        bark.get_param("nodedef").value = "ND_add_color3"
        assert bark.name == "Bark"


def test_an_automatic_name_follows_the_shader():
    with Scene() as scene:
        node = scene.add("mtlx_shader", nodedef="ND_constant_color3")
        node.get_param("nodedef").value = "ND_add_color3"
        assert node.name == "add_color3"
