"""usdview asks to install a chunk's missing packages after a scene load, never during it.

The question is a modal dialog with its own event loop. Shown while a scene was loading, it
let usdview's queued work run on the half-loaded stage, and opening a scene with an RBD Solver
crashed in usdview's prim tree (_resizePrimView). Now the chunk loads without the packages
and the question comes once the load is over.

usdview is not importable here, so the scene is built without its constructor, as in
test_usdview_freecam_lens, and the dialog and timer are stand-ins.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pxr.Usdviewq", reason="usdview is not part of the usd-core wheel")

from usd_chunks import chunk_notice  # noqa: E402
from usd_chunks.hosts import usdview  # noqa: E402


@pytest.fixture()
def host(monkeypatch):
    asked: list[str] = []
    queued: list = []
    monkeypatch.setattr(usdview.chunk_qt, "confirm_dial", lambda msg, **kw: asked.append(kw.get("detail", "")) or False)
    monkeypatch.setattr(usdview.QtCore.QTimer, "singleShot", lambda ms, ctx, fn: queued.append(fn))
    return object.__new__(usdview.UsdviewScene), asked, queued


def test_asks_after_the_load_not_during_it(host, monkeypatch) -> None:
    scene, asked, queued = host
    monkeypatch.setattr(chunk_notice, "_loading_stage", object())

    assert scene.should_install_packages(["mujoco>=3.1,<4"], "rbd_solver") is False
    assert asked == [], "no dialog while the scene loads"
    assert len(queued) == 1

    monkeypatch.setattr(chunk_notice, "_loading_stage", None)
    queued.pop()()
    assert len(asked) == 1 and "reopen" in asked[0], "asked once the load is over"


def test_asks_right_away_outside_a_load(host, monkeypatch) -> None:
    scene, asked, queued = host
    monkeypatch.setattr(chunk_notice, "_loading_stage", None)

    scene.should_install_packages(["mujoco>=3.1,<4"], "rbd_solver")
    assert len(asked) == 1 and queued == []


def test_auto_install_during_a_load_runs_without_the_dialog(host, monkeypatch) -> None:
    """USD_CHUNK_AUTO_INSTALL=1 skips the question, so pip runs mid-load: no dialog then."""
    scene, _asked, _queued = host
    monkeypatch.setattr(usdview.chunk_qt, "progress_dial", lambda *a: pytest.fail("progress dialog during a load"))
    monkeypatch.setattr(chunk_notice, "_loading_stage", object())

    assert scene.run_install(["mujoco>=3.1,<4"], "rbd_solver", lambda on_line=None: (0, "ok")) == (0, "ok")


def test_auto_install_outside_a_load_shows_progress(host, monkeypatch) -> None:
    scene, _asked, _queued = host
    shown = []
    monkeypatch.setattr(usdview.chunk_qt, "progress_dial", lambda title, label, run: shown.append(label) or run())
    monkeypatch.setattr(chunk_notice, "_loading_stage", None)

    scene.run_install(["mujoco>=3.1,<4"], "rbd_solver", lambda on_line=None: (0, "ok"))
    assert len(shown) == 1
