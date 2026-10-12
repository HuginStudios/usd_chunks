"""Points bucketed by integer cell id, for neighbourhood queries as array work.

The members are sorted by cell id, so each occupied cell is one run of that order, found by
binary search, and a cell's neighbourhood is a handful of runs. Queries are answered as flat
(query, member) index pairs, or a cell at a time where that is cheaper -- never a Python
loop per cell of queries, which was most of a scatter's cook in Grow Near and the Density
Field before this.

The caller owns the cell ids: any int64 encoding of a cell in which a neighbour's id is the
cell's id plus a fixed offset id (see proximity._cell_ids).

Requires: numpy.
"""

from __future__ import annotations

import numpy as np

# Query points x members in one cell past which the cell is taken whole -- see CellRuns.split.
DENSE = 20_000


class CellRuns:
    """Member indices grouped by cell: *ids* (M,) is each member's cell id."""

    __slots__ = ("order", "cells", "starts", "counts")

    def __init__(self, ids):
        ids = np.asarray(ids, dtype=np.int64).reshape(-1)
        self.order = np.argsort(ids, kind="stable")
        # Per occupied cell: its id, where its run of `order` starts, and how long it is.
        self.cells, self.starts, self.counts = np.unique(ids[self.order], return_index=True, return_counts=True)

    def __len__(self):
        return len(self.order)

    def lookup(self, ids, offsets):
        """Per cell the queries *ids* (N,) fall in, its neighbourhood -- the cells *offsets*
        away -- as runs: (query -> cell, run starts, run lengths), the runs (cells, offsets)
        arrays, cells ascending. Looked up once per cell, not once per query: a batch of
        candidates shares a few thousand cells, and the search was the cost."""
        cells, inverse = np.unique(np.asarray(ids, dtype=np.int64).reshape(-1), return_inverse=True)
        near = cells[:, None] + np.asarray(offsets, dtype=np.int64)[None, :]
        at = np.minimum(np.searchsorted(self.cells, near), len(self.cells) - 1)
        found = self.cells[at] == near
        return inverse.reshape(-1), np.where(found, self.starts[at], 0), np.where(found, self.counts[at], 0)

    def members(self, lo, runs):
        """The member indices in runs starting at *lo*, *runs* long (1-D), run by run: one
        arange, shifted per run."""
        total = int(runs.sum())
        return self.order[np.repeat(lo - (np.cumsum(runs) - runs), runs) + np.arange(total)]

    def pairs(self, rows, inverse, lo, runs, budget=2_000_000):
        """Every (query, member) pair of the queries *rows* (ascending) and their cells'
        neighbourhoods, as (rows, members) index arrays, query by query, in blocks of about
        *budget* pairs -- so a dense neighbourhood (road vertices pack ~1500 into one) is
        never padded out to every query."""
        lo, runs = lo[inverse[rows]], runs[inverse[rows]]
        per_query = runs.sum(axis=1)
        ends = np.cumsum(per_query)
        a, done = 0, 0
        while a < len(rows):
            b = min(max(int(np.searchsorted(ends, done + budget, side="right")), a + 1), len(rows))
            if ends[b - 1] > done:
                yield np.repeat(rows[a:b], per_query[a:b]), self.members(lo[a:b].ravel(), runs[a:b].ravel())
            done, a = int(ends[b - 1]), b

    @staticmethod
    def split(inverse, runs):
        """The queries to pair up, and the dense cells to take whole: (rows, [(rows, cell)]).

        A cell whose queries times its members passes DENSE costs less as one broadcast (its
        queries x its members) than as pairs, which gather every query once per member: a
        200 m radius over a forest puts every source in every neighbourhood."""
        sizes = runs.sum(axis=1)
        order = np.argsort(inverse, kind="stable")
        bounds = np.searchsorted(inverse[order], np.arange(len(sizes) + 1))
        dense = np.flatnonzero(np.diff(bounds) * sizes >= DENSE)
        if not len(dense):
            return np.arange(len(inverse)), []
        whole = [(order[bounds[u]:bounds[u + 1]], u) for u in dense.tolist()]
        is_dense = np.zeros(len(sizes), dtype=bool)
        is_dense[dense] = True
        return np.flatnonzero(~is_dense[inverse]), whole


def segment_starts(rows):
    """Where each query's run starts in pair arrays that come query by query."""
    return np.flatnonzero(np.r_[True, rows[1:] != rows[:-1]])
