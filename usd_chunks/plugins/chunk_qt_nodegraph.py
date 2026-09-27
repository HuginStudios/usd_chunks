"""Moved to `usd_chunks.frontends.qt.nodegraph`.

This name stays importable for code written before the move. It is the same module object,
not a copy, so patching an attribute through either name patches both. Run with ``-m``, it
runs the new module as ``__main__``.
"""
from __future__ import annotations
import importlib
import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("usd_chunks.frontends.qt.nodegraph", run_name="__main__", alter_sys=True)
else:
    sys.modules[__name__] = importlib.import_module("usd_chunks.frontends.qt.nodegraph")
