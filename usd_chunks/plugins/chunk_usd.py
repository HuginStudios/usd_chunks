"""Moved to `usd_chunks.chunk_stage`.

This name stays importable for code written before the move. It is the same module object,
not a copy, so patching an attribute through either name patches both.
"""
from __future__ import annotations
import sys

from usd_chunks import chunk_stage as _chunk_stage

sys.modules[__name__] = _chunk_stage
