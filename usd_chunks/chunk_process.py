"""The Python interpreter usd_chunks starts its subprocesses with.

pip, a sim bake's worker, a sweep's or wedge's variant, a pinned scene's venv: every one
runs ``<python> -m ...``, and that python must be a Python. sys.executable is only one when
the host is: inside Houdini it is houdini.exe (hython in hython), and
``houdini.exe -m pip list`` opened a whole new Houdini, whose startup ran the same check
again -- one more Houdini per check. So nothing here starts sys.executable directly; it
starts python_executable(), which a host whose executable is not a Python points at one
that is (hosts/houdini does, at Houdini's own python311/python.exe).

Imports nothing from usd_chunks, so any module can use it.
"""
from __future__ import annotations

import os
import sys

#: Environment variable naming the interpreter; unset means sys.executable.
USD_CHUNK_PYTHON = "USD_CHUNK_PYTHON"


def python_executable() -> str:
    """The Python to start subprocesses with: $USD_CHUNK_PYTHON, else sys.executable."""
    return os.environ.get(USD_CHUNK_PYTHON) or sys.executable
