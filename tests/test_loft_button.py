"""Loft a button: the group gets a button that clicks the inner one."""
from __future__ import annotations

import pytest

from tests.test_unloft import _lofted, _loft, env  # noqa: F401
from pxr import Usd

from usd_chunks import chunk, chunk_stage
from usd_chunks.chunk_stage import export_chunks
from tests.test_lofted_menu_follows_inner import _init, _mk, _settle


def _button_on(inner, clicks):
    btn = chunk.Button("go", "Go")
    btn.add_callback(lambda *a: clicks.append(1))
    inner.add_param(btn)
    return btn


def test_a_lofted_button_clicks_the_inner_one(env):
    group = env["mk"]("chunkgroup", name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    clicks = []
    btn = _button_on(inner, clicks)
    env["settle"]()

    lofted = _loft(env, btn, group)

    assert isinstance(lofted, chunk.Button)
    lofted.click()
    env["settle"]()
    assert clicks == [1]


def test_unloft_removes_the_group_button_and_leaves_the_inner_one_clickable(env):
    group = env["mk"]("chunkgroup", name="Group")
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    clicks = []
    btn = _button_on(inner, clicks)
    env["settle"]()
    lofted = _loft(env, btn, group)

    chunk_stage.unloft_param(lofted)
    env["settle"]()

    assert _lofted(group) == []
    btn.click()
    assert clicks == [1]


def test_a_reopened_lofted_button_still_clicks_the_inner_one(qapp, tmp_path):
    stage = Usd.Stage.CreateInMemory()
    scene = _init(stage)
    group = _mk(scene, "chunkgroup")
    inner = _mk(scene, "export_chunkgroup", context=group)
    _settle(qapp)
    chunk_stage.loft_param_to_context(inner.get_param("export"), group)
    _settle(qapp)
    out = tmp_path / "lofted_button.usda"
    export_chunks(scene, out)

    scene2 = _init(Usd.Stage.Open(str(out)))
    _settle(qapp)
    group2 = next(c for c in scene2.get_chunks() if c.name == group.name)
    inner2 = next(c for c in group2.get_my_params() if c.name == inner.name)
    lofted2 = next(p for p in group2.get_my_params() if getattr(p, "_lofted", False))
    assert isinstance(lofted2, chunk.Button)

    clicks = []
    inner2.get_param("export").add_callback(lambda *a: clicks.append(1))
    lofted2.click()
    _settle(qapp)
    assert clicks == [1]
