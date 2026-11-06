"""A plain script's Scene prints usd_chunks warnings and errors, not its info.

The package logger only had a NullHandler until an entry point configured it, and a script
never did. So a failed package install (the usdview bundle's Python has no pip) said nothing,
and the chunk quietly ran without the package.
"""
from __future__ import annotations

import os
import subprocess
import sys

SCRIPT = """
import logging
from usd_chunks import Scene
with Scene():
    logging.getLogger("usd_chunks.test").info("an info line")
    logging.getLogger("usd_chunks.test").warning("a warning line")
"""


def test_a_script_scene_prints_warnings_but_not_info():
    env = dict(os.environ)
    out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    assert "a warning line" in out.stderr
    assert "an info line" not in out.stderr
