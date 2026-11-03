"""usdview's Hydra Settings and the Render Settings chunk show the same renderer settings.

Both set the same delegate, through StageView.SetRendererSetting, and each used to know
only what it had set itself: the chunk kept showing what it last sent while the delegate ran
on what the menu set, and the menu kept the checkbox state it was built with. The host now
reports what usdview's own UI sets (VIEWPORT_RENDERER_SETTINGS_CHANGED, which the chunk
follows) and rebuilds the menu after a chunk sets anything.

Also pins the key type: the push built Tf.Token(key), which this USD's Python does not bind,
so every setting raised -- caught and logged at debug -- and none ever reached the delegate.

usdview is not importable here, so the proxy is built without its constructor and driven
with stand-ins for the stage view and the app controller.
"""
from __future__ import annotations

import pytest

# chunk_usdview imports pxr.Usdviewq, which ships with a full USD build but not with the
# usd-core wheel CI installs.
pytest.importorskip("pxr.Usdviewq", reason="usdview is not part of the usd-core wheel")

from usd_chunks.hosts.usdview import UsdviewScene  # noqa: E402


class _StageView:
    """StageView, as far as renderer settings go."""

    def __init__(self):
        self.settings = {}

    def SetRendererSetting(self, key, value):
        if not isinstance(key, str):
            raise TypeError(f"expected a str key, got {type(key).__name__}")
        self.settings[key] = value

    def GetCurrentRendererId(self):
        return "HdAuroraRendererPlugin"


class _AppController:
    def __init__(self, view):
        self._stageView = view
        self.menu_builds = 0

    def _configureRendererSettings(self):
        self.menu_builds += 1


@pytest.fixture
def rig():
    view = _StageView()
    ctrl = _AppController(view)
    scene = UsdviewScene.__new__(UsdviewScene)
    scene._setting_viewport_renderer = False
    scene._get_ctrl = lambda: ctrl
    reported = []
    scene.viewportRendererSettingsChangedCallback = (
        lambda renderer_id, settings: reported.append((renderer_id, dict(settings)))
    )
    scene._watch_renderer_settings(view)
    return scene, view, ctrl, reported


def test_a_hydra_settings_change_is_reported(rig) -> None:
    """usdview's menu calls SetRendererSetting on the stage view; that is what is watched."""
    _scene, view, _ctrl, reported = rig
    view.SetRendererSetting("aurora:is_denoising_enabled", True)

    assert view.settings == {"aurora:is_denoising_enabled": True}
    assert reported == [("HdAuroraRendererPlugin", {"aurora:is_denoising_enabled": True})]


def test_a_chunk_push_reaches_the_delegate_with_str_keys(rig) -> None:
    scene, view, _ctrl, _reported = rig
    scene.set_viewport_renderer_settings({"aurora:exposure": 2.5})

    assert view.settings == {"aurora:exposure": 2.5}


def test_a_chunk_push_is_not_reported_back(rig) -> None:
    """The chunk already has what it sent; reporting it would echo it back."""
    scene, _view, _ctrl, reported = rig
    scene.set_viewport_renderer_settings({"aurora:exposure": 2.5})

    assert reported == []


def test_the_menu_is_rebuilt_after_a_chunk_push(rig) -> None:
    """usdview reads its checkboxes from the delegate only when it builds the menu."""
    scene, _view, ctrl, _reported = rig
    scene.set_viewport_renderer_settings({"aurora:exposure": 2.5})

    assert ctrl.menu_builds == 1


def test_the_view_is_wrapped_once(rig) -> None:
    """set_viewport_renderer_settings re-checks the wrap; it must not stack."""
    scene, view, _ctrl, reported = rig
    scene._watch_renderer_settings(view)
    view.SetRendererSetting("aurora:trace_depth", 8)

    assert len(reported) == 1
