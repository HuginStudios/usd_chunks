"""A script with a QApplication but no GL context: the Render chunk makes one.

HgiGL needs a current OpenGL context with Use GPU on. The check only asked whether a Qt
application existed, so a script that made one (and no context) crashed inside the
delegate with an access violation instead of rendering.

Run in a subprocess on the desktop Qt platform: the suite's offscreen platform cannot make
a context at all.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

RENDER_DIR = Path(__file__).resolve().parents[1] / "chunks" / "usd"

SCRIPT = f"""
import sys
from PySide6 import QtGui, QtWidgets
app = QtWidgets.QApplication(sys.argv)
sys.path.insert(0, {str(RENDER_DIR)!r})
import render
assert QtGui.QOpenGLContext.currentContext() is None
possible = render._gl_context_possible()
print("possible" if possible else "impossible", QtGui.QOpenGLContext.currentContext() is not None)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="needs a desktop session")
def test_a_qt_app_with_no_context_gets_one():
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    last = out.stdout.strip().splitlines()[-1]
    if last.startswith("impossible"):
        pytest.skip("no OpenGL on this machine")
    assert last == "possible True", "said a context was possible without one being current"
