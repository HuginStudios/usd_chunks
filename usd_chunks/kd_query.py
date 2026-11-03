"""KD-tree queries split over a pool of threads that lives as long as the process.

scipy's cKDTree.query(workers=-1) starts a thread per core on every call, and a scatter asks
tens of thousands of small queries: YoungValley's started 406k threads, a third of its cook.
One query on one thread wastes the other cores instead. Here a query is cut into blocks and
the blocks go to threads made once -- scipy lets go of the interpreter while it walks the
tree, so they run side by side. The answer is the same as one call's: each point's query is
its own, and the blocks are joined back in order.

A caller already running on several threads of its own (single_thread) queries on its own
thread: more would only wait on each other.

Requires: numpy, scipy (the tree is the caller's).
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

# Below this many points a query runs on the calling thread: handing out blocks costs more.
_MIN_SPLIT = 4096
# Points per block: big enough that handing it out is a small part of walking it.
_BLOCK = 2048

_local = threading.local()
_pool = None
_pool_lock = threading.Lock()


def single_thread():
    """A thread initializer: this thread's queries stay on it."""
    _local.single = True


def _threads():
    return max(1, min(os.cpu_count() or 1, 16))


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(_threads(), thread_name_prefix="kd_query", initializer=single_thread)
        return _pool


def query(tree, points, **kwargs):
    """tree.query(points, **kwargs) -- the same (distances, indices) -- over the pool."""
    points = np.asarray(points)
    if len(points) < _MIN_SPLIT or getattr(_local, "single", False) or _threads() == 1:
        return tree.query(points, workers=1, **kwargs)
    blocks = max(2, min(_threads() * 2, -(-len(points) // _BLOCK)))
    edges = np.linspace(0, len(points), blocks + 1).astype(np.int64)
    parts = list(_get_pool().map(lambda ab: tree.query(points[ab[0]:ab[1]], workers=1, **kwargs),
                                 zip(edges[:-1], edges[1:])))
    return np.concatenate([p[0] for p in parts]), np.concatenate([p[1] for p in parts])
