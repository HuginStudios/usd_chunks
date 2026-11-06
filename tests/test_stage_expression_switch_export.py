"""An export of a network with a Stage Expression Switch holds only the branch it picks.

With TIME = day the exported file still held the night light rig as well. The switch muted
the night branch when it cooked, and a later view recook in the same pass -- the
chunkgroup's -- revealed everything upstream of its view again; the switch, already cooked,
did not run to mute it once more.
"""
from __future__ import annotations

from pxr import Usd

from usd_chunks import Scene


def _defined(stage, path):
    prim = stage.GetPrimAtPath(path)
    return bool(prim) and prim.IsDefined()


def test_an_export_holds_only_the_picked_branch(tmp_path):
    out = tmp_path / "day.usda"
    with Scene(host_name="pytest", lazy=True) as scene:
        ground = scene.create.cube(prim_path="/world/ground", name="Ground")
        time_of_day = scene.create.stage_expression(ground, name="TimeOfDay")
        time_of_day.get_param("variables").size = 1
        scene.set(time_of_day, **{"variables.0.var_name": "TIME", "variables.0.var_type": "string",
                                  "variables.0.var_value": "day"})
        sun = scene.create.cube(time_of_day, prim_path="/lights/day/sun", name="Sun")
        moon = scene.create.cube(time_of_day, prim_path="/lights/night/moon", name="Moon")
        switch = scene.create.stage_expression_switch(sun, moon, name="DayNight", variable="TIME", pattern="night")
        scene.set_output(scene.create.transform(switch, name="After"))
        scene.export(str(out))
        assert not _defined(scene.get_stage(), "/lights/night/moon")

    exported = Usd.Stage.Open(str(out))
    assert _defined(exported, "/lights/day/sun")
    assert not _defined(exported, "/lights/night/moon")
