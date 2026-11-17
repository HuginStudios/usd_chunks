"""Render chunk: Run in Background renders in its own process, and Stop Render ends it.

The render command is a small Python script standing in for usdrecord, so the test needs
no renderer: it copies the scene file it was given to the output image, fails, or starts
a child process and sleeps, by the output image's name.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from pxr import Usd, UsdGeom

from usd_chunks.chunk_registry import ChunkRegistry
from usd_chunks.chunk_scene import run_pending
from usd_chunks.chunk_script import Scene

FAKE_RENDER = """
import subprocess, sys, time
scene_path, out = sys.argv[-2], sys.argv[-1]
if "fail" in out:
    sys.stderr.write("no licence")
    sys.exit(3)
if "slow" in out:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(out + ".pid", "w") as f:
        f.write(str(child.pid))
    time.sleep(60)
with open(scene_path) as src, open(out, "w") as dst:
    dst.write(scene_path + "\\n" + src.read())
"""


@pytest.fixture
def render(tmp_path, monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/World/box")
    scene = Scene(stage, "pytest")
    node = scene.add("render", background=True)
    fake = tmp_path / "fake_render.py"
    fake.write_text(FAKE_RENDER)
    module = ChunkRegistry().get_chunk_module("render").module
    monkeypatch.setattr(module, "_resolve_command", lambda command: [sys.executable, str(fake)])
    yield node
    module._stop_all_background()
    scene.close()


def _wait_for_status(node, prefix, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        run_pending()
        status = node.get_param("render_status").value
        if status.startswith(prefix):
            return status
        time.sleep(0.05)
    raise AssertionError(f"status stayed {node.get_param('render_status').value!r}, wanted {prefix!r}")


def _is_running(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_render_now_returns_at_once_and_reports_done(render, tmp_path):
    out = tmp_path / "frame.png"
    render.get_param("output_image").value = str(out)

    render.get_param("render_now").click()
    assert render.get_param("render_status").value == "Rendering frame.png"

    _wait_for_status(render, "Done")
    assert render.get_param("output").value == str(out)
    scene_path, scene_text = out.read_text().split("\n", 1)
    assert scene_path.endswith(".usda") and 'def Cube "box"' in scene_text  # the flattened scene
    assert not os.path.exists(scene_path)  # and removed it afterwards


def test_stop_render_ends_the_render_and_what_it_started(render, tmp_path, caplog):
    out = tmp_path / "slow.png"
    render.get_param("output_image").value = str(out)
    render.get_param("render_now").click()
    pid_file = tmp_path / "slow.png.pid"
    end = time.monotonic() + 30
    while not pid_file.exists() and time.monotonic() < end:
        time.sleep(0.05)
    time.sleep(0.2)  # let the script finish writing the pid
    child = int(pid_file.read_text())

    render.get_param("render_now").click()  # a second press while rendering starts nothing
    assert "already rendering" in caplog.text

    render.get_param("stop_render").click()
    _wait_for_status(render, "Stopped")
    end = time.monotonic() + 10
    while _is_running(child) and time.monotonic() < end:
        time.sleep(0.1)
    assert not _is_running(child)
    assert render.get_param("output").value == ""


def test_a_failed_render_says_so(render, tmp_path, caplog):
    render.get_param("output_image").value = str(tmp_path / "fail.png")

    render.get_param("render_now").click()

    assert _wait_for_status(render, "Failed") == "Failed (exit 3), see the log"
    assert "no licence" in caplog.text
    assert render.get_param("output").value == ""
