"""Connection-aware auto-layout algorithm for the node graph."""

from __future__ import annotations
from typing import Any


def layout_nodes(graph: Any, node_list: list[Any] | None = None) -> None:
    """Layout nodes left-to-right by depth, vertically aligned to connections."""
    if node_list:
        expanded = set()

        def _collect(node) -> None:
            if node.id in expanded:
                return
            expanded.add(node.id)
            for port in node.input_ports():
                for cp in port.connected_ports():
                    _collect(cp.node())
            for port in node.output_ports():
                for cp in port.connected_ports():
                    _collect(cp.node())

        for n in node_list:
            _collect(n)
        all_n = [n for n in graph.all_nodes() if n.id in expanded]
    else:
        all_n = graph.all_nodes()

    if not all_n:
        return

    node_set = set(n.id for n in all_n)
    node_by_id = {n.id: n for n in all_n}

    def _node_size(n) -> None:
        try:
            r = n.view.boundingRect()
            return r.width(), r.height()
        except Exception:
            return 160, 80

    def _upstream_ids(node) -> None:
        ids = []
        for port in node.input_ports():
            for cp in port.connected_ports():
                if cp.node().id in node_set:
                    ids.append(cp.node().id)
        return ids

    def _downstream_ids(node) -> None:
        ids = []
        for port in node.output_ports():
            for cp in port.connected_ports():
                if cp.node().id in node_set:
                    ids.append(cp.node().id)
        return ids

    depth = {}

    def _get_depth(node) -> None:
        if node.id in depth:
            return depth[node.id]
        depth[node.id] = 0
        max_d = -1
        for uid in _upstream_ids(node):
            max_d = max(max_d, _get_depth(node_by_id[uid]))
        depth[node.id] = max_d + 1
        return depth[node.id]

    for n in all_n:
        _get_depth(n)

    for n in all_n:
        if depth[n.id] == 0 and not _upstream_ids(n):
            ds = _downstream_ids(n)
            if ds:
                min_ds_depth = min(depth[did] for did in ds)
                if min_ds_depth > 1:
                    depth[n.id] = min_ds_depth - 1

    columns = {}
    for n in all_n:
        columns.setdefault(depth[n.id], []).append(n)

    x_gap = 60
    y_gap = 35
    col_x = {}
    sorted_cols = sorted(columns.keys())
    start_x = min(n.pos()[0] for n in all_n)
    cur_x = start_x
    for ci in sorted_cols:
        col_x[ci] = cur_x
        max_w = max(_node_size(n)[0] for n in columns[ci])
        cur_x += max_w + x_gap

    placed = {}

    def _target_y(n) -> None:
        ups = [placed[uid][1] for uid in _upstream_ids(n) if uid in placed]
        if ups:
            return sum(ups) / len(ups)
        ds = [placed[did][1] for did in _downstream_ids(n) if did in placed]
        if ds:
            return sum(ds) / len(ds)
        return 0

    def _place_column(col_nodes, cx) -> None:
        targets = []
        for n in col_nodes:
            ty = _target_y(n)
            _, h = _node_size(n)
            targets.append((ty, n, h))
        targets.sort(key=lambda t: t[0])

        occupied = []
        for ty, n, h in targets:
            y = ty
            for oy_top, oy_bot in occupied:
                if y < oy_bot + y_gap and y + h > oy_top - y_gap:
                    y = oy_bot + y_gap
            occupied.append((y, y + h))
            placed[n.id] = (cx, y, h)

    for ci in sorted_cols:
        _place_column(columns[ci], col_x[ci])

    if sorted_cols:
        first_col = sorted_cols[0]
        sources = [n for n in columns[first_col] if not _upstream_ids(n)]
        if sources:
            for n in sources:
                if n.id in placed:
                    del placed[n.id]
            _place_column(sources, col_x[first_col])

    for n in all_n:
        if n.id in placed:
            n.set_pos(placed[n.id][0], placed[n.id][1])
