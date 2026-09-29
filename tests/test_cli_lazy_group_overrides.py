"""`usdchunks run` can set a param a chunk has not built yet.

Render Settings builds a group's params only when the group is first opened: V-Ray alone
declares hundreds, and building them all up front cost seconds before any was visible.
The command line never opens anything, so `vray_render_device=7` -- the one setting that
moves a render onto the GPU -- answered "Param not found" and the render ran on the CPU.
The override now opens the groups it has to, the way clicking a header does, and closes
them again so the saved network is unchanged.
"""

from __future__ import annotations

import pytest

from usd_chunks import Scene, chunk, cli

COLLAPSED = chunk.ChunkFlags.State.COLLAPSED


def _build_on_open(owner, notice, sender):
    """Add the group's param when it is first opened, as Render Settings does."""
    if owner.has_state(COLLAPSED) or owner.get_param("device") is not None:
        return
    owner.add_param(chunk.LineEdit("device", "Device", default_value="0"))


def _group(name):
    group = chunk.Group(
        name, name,
        flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED,
    )
    group.set_state(COLLAPSED)
    return group


def _renderer():
    """A chunk holding VRay > render > device, with render built on first open."""
    node = chunk.Chunk("RenderSettings")
    outer = _group("VRay")
    inner = _group("render")
    inner.add_callback(_build_on_open, notice_alias=chunk.ChunkNoticeAlias.STATE_CHANGED, send_on_create=False)
    outer.add_param(inner)
    node.add_param(outer)
    return node, outer, inner


@pytest.fixture(autouse=True)
def scene():
    with Scene() as live:
        yield live


def test_the_param_is_not_there_until_its_group_opens():
    node, _, _ = _renderer()
    assert node.get_param("VRay.render.device") is None


def test_a_full_path_opens_the_groups_on_it():
    node, outer, inner = _renderer()

    param = cli._find_param(node, "VRay.render.device")

    assert param is not None and param.value == "0"
    assert outer.has_state(COLLAPSED) and inner.has_state(COLLAPSED), "closed again afterwards"


def test_a_bare_name_searches_the_collapsed_groups():
    node, _, _ = _renderer()
    assert cli._find_param(node, "device") is not None


def test_a_name_nothing_builds_is_still_not_found():
    node, _, inner = _renderer()
    assert cli._find_param(node, "no_such_setting") is None
    assert inner.has_state(COLLAPSED)


@pytest.mark.parametrize("spelling", ["device", "RenderSettings.device", "RenderSettings.VRay.render.device"])
def test_overrides_reach_an_unbuilt_param_by_every_spelling(scene, spelling):
    node, _, _ = _renderer()
    scene.add_chunks([node])
    cli._parse_param_overrides(scene, [f"{spelling}=7"])
    assert node.get_param("VRay.render.device").value == "7"
