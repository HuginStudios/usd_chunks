"""A str reaches a token or asset attribute it drives, not only a String one.

The guards that keep unresolved expression text (an @ref@ that did not resolve, a ${VAR}
with no value) out of a numeric attribute used to test `type != String`. A token and an
asset take a str just as well, so those guards silently dropped every real value bound for
one: a dome light's texture file never reached the prim, and neither did any schema enum
picked from a menu. Live-reported on the Light chunk's DomeLight image.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdLux

from usd_chunks import chunk, chunk_registry, chunk_utils
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


@pytest.fixture
def light(qapp):
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    module = registry.get_chunk_module("light")
    assert module is not None
    node = (module.create() if module.loaded else module.load())[0]
    scene.add_chunk(node)
    node.get_param("light_type").value = "DomeLight"
    qapp.processEvents()
    prim = stage.GetPrimAtPath(node.get_param("primpath_location").value)
    assert prim and prim.IsA(UsdLux.DomeLight)
    return node, prim


def test_a_dome_lights_texture_file_reaches_the_prim(qapp, light):
    node, prim = light
    param = node.get_param("attr_grp.inputstexture.file")
    assert isinstance(param, chunk.FileBrowser)

    param.value = "C:/tex/sky.exr"
    qapp.processEvents()

    attr = prim.GetAttribute("inputs:texture:file")
    assert attr.GetTypeName() == Sdf.ValueTypeNames.Asset
    assert attr.Get() == Sdf.AssetPath("C:/tex/sky.exr"), (
        "an asset attr takes a str; the value must not be dropped as 'text on a typed attr'"
    )


def test_a_token_menu_reaches_the_prim(qapp, light):
    node, prim = light
    param = node.get_param("attr_grp.visibility")
    assert isinstance(param, chunk.Menu)

    param.value = "invisible"
    qapp.processEvents()

    attr = prim.GetAttribute("visibility")
    assert attr.GetTypeName() == Sdf.ValueTypeNames.Token
    assert attr.Get() == "invisible"


def test_unresolved_text_still_stays_off_a_numeric_attr():
    """The reason the guard exists: expression text must never be Set() on a float."""
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/p")
    f = prim.CreateAttribute("f", Sdf.ValueTypeNames.Float)
    t = prim.CreateAttribute("t", Sdf.ValueTypeNames.Token)
    a = prim.CreateAttribute("a", Sdf.ValueTypeNames.Asset)
    s = prim.CreateAttribute("s", Sdf.ValueTypeNames.String)
    assert not chunk_utils.attr_holds_text(f)
    assert chunk_utils.attr_holds_text(t)
    assert chunk_utils.attr_holds_text(a)
    assert chunk_utils.attr_holds_text(s)

