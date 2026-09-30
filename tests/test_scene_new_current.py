"""Scene.new() for a script's own scene, Scene.current() for the one an app is running.

A new Scene typed into usdview's interpreter used to build on a stage of its own that
usdview never shows, and took the registry away from usdview on the way. Now it refuses,
and Scene.current() is the way into the app's scene.
"""

from __future__ import annotations

import pytest

from usd_chunks import Scene, chunk_registry, current_scene
from usd_chunks.chunk_script import ScriptingMixin


class _App(Scene):
    """Stands in for usdview: a live scene that belongs to an app."""

    is_app = True
    # An app's scene is borrowed: leaving a `with` block keeps it, as for usdview's.
    __exit__ = ScriptingMixin.__exit__


@pytest.fixture
def app():
    host = _App(host_name="app")
    try:
        yield host
    finally:
        # Let the next test start a Scene of its own again.
        host.is_app = False
        host.close()


def _build_box(scene):
    box = scene.create.cube(prim_path="/geo/box", width=2.0)
    scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)


def test_new_is_a_scene_of_its_own() -> None:
    with Scene.new() as scene:
        assert Scene.current() is scene
        _build_box(scene)
        assert scene.get_stage().GetPrimAtPath("/geo/box").IsDefined()


def test_current_builds_in_the_app_scene(app) -> None:
    assert Scene.current() is app

    with Scene.current() as scene:
        _build_box(scene)

    assert app.get_stage().GetPrimAtPath("/geo/box").IsDefined()
    assert chunk_registry.ChunkRegistry().scene is app, "leaving the block must not end the app's scene"


def test_new_refuses_while_an_app_is_running(app) -> None:
    with pytest.raises(RuntimeError, match=r"Scene\.current\(\)"):
        Scene.new()
    with pytest.raises(RuntimeError):
        Scene()

    assert chunk_registry.ChunkRegistry().scene is app, "the refused Scene must not take the registry"


def test_current_scene_still_works(app) -> None:
    assert current_scene() is Scene.current()
