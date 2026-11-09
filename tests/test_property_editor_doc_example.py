"""The edit lines Property Editor's description shows (and `usdchunks show` prints) work.

An agent wrote `xformOp:translate = (3, 0.5, 0)` and got "skipped an edit it could not
read": the value is JSON, which nothing said, and it read the chunk's source to find out.
The description now gives the rule and an example; this runs that example.
"""
from __future__ import annotations

import logging

import pytest

from usd_chunks import Scene


def test_the_described_edits_apply(caplog) -> None:
    caplog.set_level(logging.WARNING)
    with Scene() as scene:
        last = scene.create.distant_light(primpath_location="/World/key")
        last = scene.create.property_editor(last, prim="/World/key", edits=(
            "inputs:intensity = 3\n"
            "inputs:color = [1, 0.9, 0.8]\n"
            "bool vray:env:bg:tex:use = true"))
        scene.set_output(last)
        key = scene.get_stage().GetPrimAtPath("/World/key")

        assert key.GetAttribute("inputs:intensity").Get() == pytest.approx(3.0)
        assert tuple(key.GetAttribute("inputs:color").Get()) == pytest.approx((1.0, 0.9, 0.8))
        assert key.GetAttribute("vray:env:bg:tex:use").Get() is True
    assert "could not read" not in caplog.text


def test_a_tuple_is_not_json_and_says_so(caplog) -> None:
    caplog.set_level(logging.WARNING)
    with Scene() as scene:
        last = scene.create.distant_light(primpath_location="/World/key")
        scene.set_output(scene.create.property_editor(last, prim="/World/key", edits="inputs:color = (1, 0, 0)"))
    assert "skipped an edit it could not read" in caplog.text
