"""usdview's current frame comes back as a number, as get_current_frame promises.

usdview keeps its frame as a Usd.TimeCode, which float() refuses, so a caller converting
the frame with float() failed in usdview.
"""
from __future__ import annotations

import types

import pytest

pytest.importorskip("pxr.Usdviewq", reason="usdview is not part of the usd-core wheel")

from pxr import Usd  # noqa: E402

from usd_chunks.hosts.usdview import UsdviewScene  # noqa: E402


def test_the_current_frame_is_a_float() -> None:
    scene = UsdviewScene.__new__(UsdviewScene)
    scene.usdviewAPI = types.SimpleNamespace(dataModel=types.SimpleNamespace(currentFrame=Usd.TimeCode(42)))
    frame = scene.get_current_frame()
    assert float(frame) == 42.0
