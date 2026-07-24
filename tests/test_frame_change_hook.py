from __future__ import annotations

from pxr import Usd
from pxr.Usdviewq.rootDataModel import RootDataModel

from plugins.chunk_usdview import _install_frame_change_hook
from usd_chunks import chunk_animation


def test_frame_change_hook_fires_on_current_frame_assignment(monkeypatch) -> None:
    """RootDataModel.currentFrame is the one property every frame-change path (scrubbing,
    playback, appController.setFrame(), usdview's own startup frame-range init) funnels
    through -- see CONTEXT.md's "Attribute Animation" section for why appController.setFrame()
    alone was tried and rejected. This asserts the patched setter still calls through to the
    original (so usdview's own bbox/xform caches keep updating) and also notifies
    ChunkAnimationManager with a plain float."""
    seen = []
    monkeypatch.setattr(
        chunk_animation.ChunkAnimationManager,
        "on_frame_changed",
        lambda self, frame: seen.append(frame),
    )

    _install_frame_change_hook()

    rdm = RootDataModel()
    rdm.currentFrame = Usd.TimeCode(5.0)
    rdm.currentFrame = Usd.TimeCode(12.0)

    assert seen == [5.0, 12.0]
    # Original setter still ran -- usdview's own caches (bbox/xform) stay in sync.
    assert rdm.currentFrame == Usd.TimeCode(12.0)


def test_frame_change_hook_install_is_idempotent() -> None:
    """Installing twice (e.g. two UsdviewScene instances in one process) must not
    double-wrap the property setter."""
    _install_frame_change_hook()
    once_wrapped = RootDataModel.currentFrame.fset

    _install_frame_change_hook()
    assert RootDataModel.currentFrame.fset is once_wrapped
