"""Only the camera chunk whose lock claimed the viewport may release it.

Every camera chunk listens to every camera move, lock or no lock. Each unlocked one used to
call `release_viewport_camera()` on each move, and in usdview that cancels a snap still
pending from someone else -- Set Camera switching to another camera, or another chunk's "Set
as Viewport Camera" -- so the viewport stayed where it was.
"""
from __future__ import annotations

import importlib.util
import pathlib


def _module():
    """chunks/ is not importable as a package; the chunk is loaded from its file."""
    path = pathlib.Path(__file__).resolve().parent.parent / "chunks" / "modelling" / "camera.py"
    spec = importlib.util.spec_from_file_location("camera_chunk_claim_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Value:
    def __init__(self, value):
        self.value = value


class _Scene:
    def __init__(self):
        self.releases = 0

    def release_viewport_camera(self):
        self.releases += 1


class _Chunk:
    def __init__(self, scene, locked):
        self._scene = scene
        self._params = {"lock_to_freecam": _Value(locked)}

    def get_scene(self):
        return self._scene

    def get_param(self, name):
        return self._params.get(name)


class _Owner:
    def __init__(self, chunk):
        self._chunk = chunk

    def get_chunk(self):
        return self._chunk


def test_an_unlocked_camera_does_not_release_on_a_camera_move() -> None:
    scene = _Scene()
    _module()._apply_freecam_transform(_Owner(_Chunk(scene, locked=False)), None, None)

    assert scene.releases == 0


def test_the_camera_that_held_the_lock_releases_once() -> None:
    module = _module()
    scene = _Scene()
    chunk = _Chunk(scene, locked=False)
    setattr(chunk, module._HOLDS_VIEW, True)  # its lock had claimed the view

    module._apply_freecam_transform(_Owner(chunk), None, None)
    module._apply_freecam_transform(_Owner(chunk), None, None)

    assert scene.releases == 1
