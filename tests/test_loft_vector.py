"""Loft up a vector (Focus Point, Clipping Range): the group publishes all its fields as one."""
from __future__ import annotations

import pytest
from pxr import Sdf

from tests.test_clear_param import _right_click, _row
from tests.test_unloft import env  # noqa: F401 -- env is a fixture
from usd_chunks import Scene, chunk_stage
from usd_chunks import chunk_core as core
from usd_chunks.frontends.qt.widgets import ChunkWidget


def _lofted(group) -> list:
    return [p for p in group.get_my_params() if getattr(p, "_lofted", False)]


def _camera_in_group(scene):
    group = scene.create.chunkgroup(name="grp")
    with group:
        camera = scene.create.camera(name="cam")
    return group, camera


def test_a_lofted_vector_is_one_vector_on_the_group():
    with Scene() as scene:
        group, camera = _camera_in_group(scene)

        chunk_stage.loft_param_to_context(camera.get_param("focus_point"), group)

        [lofted] = _lofted(group)
        assert isinstance(lofted, core.ChunkVectorBase)
        assert lofted.length == 3


def test_the_group_and_the_chunk_follow_each_other():
    with Scene() as scene:
        group, camera = _camera_in_group(scene)
        focus = camera.get_param("focus_point")
        chunk_stage.loft_param_to_context(focus, group)
        [lofted] = _lofted(group)

        lofted.value = (1.0, 2.0, 3.0)
        assert focus.value == pytest.approx((1.0, 2.0, 3.0))

        focus.value = (4.0, 5.0, 6.0)
        assert lofted.value == pytest.approx((4.0, 5.0, 6.0))


def test_a_lofted_vector_survives_a_reopen(tmp_path):
    path = str(tmp_path / "lofted_vector.usda")
    with Scene() as scene:
        group, camera = _camera_in_group(scene)
        chunk_stage.loft_param_to_context(camera.get_param("focus_point"), group)
        _lofted(group)[0].value = (1.0, 2.0, 3.0)
        scene.save(path)

    with Scene.open(path) as scene:
        group = next(c for c in scene.get_contexts() if c.name == "grp")
        camera = next(c for c in scene.get_chunks() if c.name == "cam")
        [lofted] = _lofted(group)
        assert lofted.value == pytest.approx((1.0, 2.0, 3.0))

        lofted.value = (7.0, 8.0, 9.0)

        assert camera.get_param("focus_point").value == pytest.approx((7.0, 8.0, 9.0))


def test_unlofting_a_vector_leaves_the_chunk_as_it_was():
    with Scene() as scene:
        group, camera = _camera_in_group(scene)
        focus = camera.get_param("focus_point")
        chunk_stage.loft_param_to_context(focus, group)
        _lofted(group)[0].value = (1.0, 2.0, 3.0)
        group_attr = _lofted(group)[0]._param_attr_path

        chunk_stage.unloft_param(_lofted(group)[0])

        assert _lofted(group) == []
        assert focus.value == pytest.approx((1.0, 2.0, 3.0))
        inner = scene.get_stage().GetAttributeAtPath(Sdf.Path(focus._param_attr_path))
        assert group_attr not in [str(c) for c in inner.GetConnections()]


def test_a_vector_lofts_from_its_header(env, monkeypatch):  # noqa: F811
    outside = env["mk"]("example", name="outside")
    group = env["mk"]("chunkgroup", context=outside.get_context(), name="grp")
    inner = env["mk"]("example", context=group, name="inner")
    body = ChunkWidget(inner)
    body.resize(400, 1200)
    body.show()
    env["settle"]()
    header = _row(body, inner.get_param("SliderVec")).widget.minimize_button

    offered = _right_click(header, monkeypatch, pick="Loft up")
    env["settle"]()

    assert "Loft up" in offered
    [lofted] = _lofted(group)
    assert isinstance(lofted, core.ChunkVectorBase)
    assert "Unloft" in _right_click(header, monkeypatch)


def test_the_group_node_shows_a_lofted_vector_until_it_is_unlofted(env):  # noqa: F811
    outside = env["mk"]("example", name="outside")
    group = env["mk"]("chunkgroup", context=outside.get_context(), name="grp")
    inner = env["mk"]("example", context=group, name="inner")
    env["settle"]()
    chunk_stage.loft_param_to_context(inner.get_param("SliderVec"), group)
    env["settle"]()
    node = ChunkWidget(group)  # a group's body on the graph
    node.show()
    env["settle"]()
    [lofted] = _lofted(group)

    assert _row(node, lofted) is not None

    chunk_stage.unloft_param(lofted)
    env["settle"]()
    with pytest.raises(AssertionError):
        _row(node, lofted)
