"""A menu value written in another case or with underscores still picks its item.

Agents and scripts wrote `light(light_type="distant_light")`, as the build skill showed. The
Light's menu holds "DistantLight", so the value was dropped back to the first item and the
scene got a DiskLight, without a word.
"""

from __future__ import annotations

import logging

from usd_chunks import Scene, chunk


def test_snake_case_names_the_item() -> None:
    menu = chunk.Menu("light_type", menu_items=["DiskLight", "RectLight", "DistantLight"])

    menu.value = "distant_light"
    assert menu.value == "DistantLight"

    menu.value = "Rect Light"
    assert menu.value == "RectLight"


def test_an_unknown_value_warns_and_falls_back(caplog) -> None:
    menu = chunk.Menu("light_type", menu_items=["DiskLight", "RectLight"])

    with caplog.at_level(logging.WARNING, logger="usd_chunks.chunk"):
        menu.value = "tube_light"

    assert menu.value == "DiskLight"
    assert "'tube_light' is not one of" in caplog.text


def test_two_items_that_match_alike_pick_neither() -> None:
    menu = chunk.Menu("mode", menu_items=["C", "A_B", "AB"])

    menu.value = "a b"
    assert menu.value == "C"  # ambiguous: the first item, not a guess


def test_light_type_in_snake_case_makes_that_light() -> None:
    with Scene() as scene:
        light = scene.create.light(primpath_location="/World/sun", light_type="distant_light")
        scene.set_output(light)
        prim = scene.get_stage().GetPrimAtPath("/World/sun")
        assert prim.GetTypeName() == "DistantLight"
