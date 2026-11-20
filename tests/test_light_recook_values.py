"""A Light keeps the values it was given, quietly, when a chain of chunks after it cooks.

A Light's params come from show_attribute, which builds them while the Light cooks. A cook
after the first is a diff cook into a scratch layer, and pushing a driven param's value used
to open an edit context for that scratch layer on the scene's stage, which doesn't hold it:
USD refused, and every run with a light logged "Could not send notice ... is not in the
local LayerStack" as an ERROR, though the value did land in the end.
"""
from __future__ import annotations

import logging

from pxr import UsdLux

from usd_chunks import Scene


def _light_chain(scene):
    ground = scene.create.ground(prim_path="/World/ground")
    sun = scene.create.distant_light(ground, primpath_location="/lights/sun", intensity=2.0)
    aim = scene.create.transform(sun, primpath_location="/lights/sun", rx=40.0)
    sky = scene.create.dome_light(aim, primpath_location="/lights/sky", intensity=0.5)
    cam = scene.create.camera(sky, primpath_location="/cam", tx=3.0)
    scene.set_output(cam)


def test_a_light_in_a_chain_cooks_without_a_notice_error(caplog) -> None:
    with caplog.at_level(logging.ERROR), Scene.new() as scene:
        _light_chain(scene)
        scene.commit()
        scene.commit()
        stage = scene.get_stage()
        assert UsdLux.DistantLight(stage.GetPrimAtPath("/lights/sun")).GetIntensityAttr().Get() == 2.0
        assert UsdLux.DomeLight(stage.GetPrimAtPath("/lights/sky")).GetIntensityAttr().Get() == 0.5
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, errors


def test_a_light_colour_set_at_create_stays_in_the_light_s_layer(tmp_path) -> None:
    """A colour is a vector param. Set at create, before the Light's params were promoted to
    drive the light, its first write went to the bookkeeping layer, the network's root: the
    saved network then held an opinion on /lights/sun stronger than the Light chunk, and
    changing the colour in the chunk did nothing."""
    path = tmp_path / "light.usda"
    with Scene.new(path=str(path)) as scene:
        ground = scene.create.ground(prim_path="/World/ground")
        sun = scene.create.distant_light(ground, primpath_location="/lights/sun", color=(1.0, 0.5, 0.25))
        scene.set_output(sun)
        scene.commit()
        stage = scene.get_stage()
        assert not stage.GetRootLayer().GetPrimAtPath("/lights"), "the root layer holds no light"
        colour = UsdLux.DistantLight(stage.GetPrimAtPath("/lights/sun")).GetColorAttr()
        assert tuple(colour.Get()) == (1.0, 0.5, 0.25)
        scene.set(sun, color=(0.25, 0.5, 1.0))
        scene.commit()
        assert tuple(colour.Get()) == (0.25, 0.5, 1.0), "an edit in the chunk still changes the light"
