"""Hierarchical group-first auto-layout for the node graph.

Design decisions (see grill-me session):
  - Backdrop membership via backdrop.nodes() — NOT positional overlap.
  - Nodes inside a backdrop are stacked in topological order (upstream first).
  - Backdrop is manually sized: 45px sides, 55px top (title bar), 100px bottom (text).
  - Canvas anchor: top-left of pre-layout positions is preserved.
  - Empty backdrops (no contained nodes) are left in place.
  - Groups within a depth column use target-then-push Y alignment.
"""

from __future__ import annotations
from typing import Any

_X_GAP = 65  # horizontal gap between group columns
_Y_GAP = 25  # vertical gap between nodes inside a backdrop
_GROUP_GAP = 55  # vertical gap between groups in the same column
_CHAIN_GAP = 60  # vertical gap between disconnected chains
_BD_PAD_X = 45  # horizontal margin inside backdrop (each side)
_BD_PAD_TOP = 55  # above first node: title bar + breathing room
_BD_PAD_BOT = 100  # below last node: room for backdrop_text


# ── Helpers ───────────────────────────────────────────────────────────────────


def _node_size(n: Any) -> tuple[float, float]:
    try:
        r = n.view.boundingRect()
        return r.width(), r.height()
    except Exception:
        return 160.0, 80.0


class _Group:
    __slots__ = ("gid", "backdrop", "nodes")

    def __init__(self, gid: int, backdrop: Any | None, nodes: list) -> None:
        self.gid = gid
        self.backdrop = backdrop  # BackdropNode or None
        self.nodes = nodes  # regular nodes belonging to this group


def _group_w(g: _Group) -> float:
    if not g.nodes:
        return 160.0
    max_nw = max(_node_size(n)[0] for n in g.nodes)
    return max_nw + 2 * _BD_PAD_X if g.backdrop is not None else max_nw


def _group_h(g: _Group) -> float:
    if not g.nodes:
        return 80.0
    nh = sum(_node_size(n)[1] for n in g.nodes)
    gaps = _Y_GAP * max(len(g.nodes) - 1, 0)
    if g.backdrop is not None:
        return _BD_PAD_TOP + nh + gaps + _BD_PAD_BOT
    return nh + gaps


# ── Within-group topo sort ────────────────────────────────────────────────────


def _topo_sort_nodes(nodes: list) -> list:
    """Return nodes in topological order (upstream first) using internal wires."""
    if len(nodes) <= 1:
        return list(nodes)

    ids = {n.id for n in nodes}
    by_id = {n.id: n for n in nodes}
    down: dict[str, list[str]] = {n.id: [] for n in nodes}

    for n in nodes:
        if not hasattr(n, "output_ports"):
            continue
        for port in n.output_ports():
            for cp in port.connected_ports():
                if cp.node().id in ids:
                    down[n.id].append(cp.node().id)

    visited: set[str] = set()
    order: list[str] = []

    def dfs(nid: str) -> None:
        if nid in visited:
            return
        visited.add(nid)
        for d in down[nid]:
            dfs(d)
        order.append(nid)

    for n in nodes:
        dfs(n.id)

    order.reverse()
    return [by_id[nid] for nid in order]


# ── Apply group position ──────────────────────────────────────────────────────


def _apply_group(g: _Group, gx: float, gy: float) -> None:
    """Position backdrop (if any) and stack nodes inside it, topo-sorted."""
    sorted_nodes = _topo_sort_nodes(g.nodes)

    if g.backdrop is not None:
        g.backdrop.set_pos(gx, gy)
        g.backdrop.set_property("width", _group_w(g))
        g.backdrop.set_property("height", _group_h(g))
        nx, ny = gx + _BD_PAD_X, gy + _BD_PAD_TOP
    else:
        nx, ny = gx, gy

    for n in sorted_nodes:
        n.set_pos(nx, ny)
        ny += _node_size(n)[1] + _Y_GAP


# ── Build groups ──────────────────────────────────────────────────────────────


def _build_groups(all_n: list, backdrops: list) -> list[_Group]:
    """Assign each regular node to a backdrop group or singleton group.

    Uses backdrop.nodes() (NodeGraphQt's Qt-based containment check) rather
    than a manual AABB test to avoid mis-assigning drifted nodes.
    """
    regular_ids = {n.id for n in all_n}
    assigned: set[str] = set()
    groups: list[_Group] = []
    gid = 0

    for bd in backdrops:
        try:
            contained = [n for n in bd.nodes() if n.id in regular_ids]
        except Exception:
            contained = []
        if contained:
            groups.append(_Group(gid, bd, contained))
            assigned.update(n.id for n in contained)
            gid += 1
        # Empty backdrops: skipped here, left untouched in layout_nodes.

    for n in all_n:
        if n.id not in assigned:
            groups.append(_Group(gid, None, [n]))
            gid += 1

    return groups


# ── Group connectivity ────────────────────────────────────────────────────────


def _build_group_graph(
    groups: list[_Group],
    node_to_gid: dict[str, int],
) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    up: dict[int, set[int]] = {g.gid: set() for g in groups}
    down: dict[int, set[int]] = {g.gid: set() for g in groups}

    for g in groups:
        for n in g.nodes:
            for port in n.input_ports():
                for cp in port.connected_ports():
                    src_gid = node_to_gid.get(cp.node().id)
                    if src_gid is not None and src_gid != g.gid:
                        up[g.gid].add(src_gid)
                        down[src_gid].add(g.gid)

    return up, down


def _find_group_components(
    groups: list[_Group],
    up: dict[int, set[int]],
    down: dict[int, set[int]],
) -> list[list[int]]:
    parent = {g.gid: g.gid for g in groups}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for gid, ups in up.items():
        for u in ups:
            union(gid, u)

    buckets: dict[int, list[int]] = {}
    for g in groups:
        buckets.setdefault(find(g.gid), []).append(g.gid)
    return list(buckets.values())


def _topo_depth(gids: list[int], up: dict[int, set[int]]) -> dict[int, int]:
    gid_set = set(gids)
    depth: dict[int, int] = {}

    def _d(gid: int) -> int:
        if gid in depth:
            return depth[gid]
        depth[gid] = 0
        md = -1
        for u in up[gid]:
            if u in gid_set:
                md = max(md, _d(u))
        depth[gid] = md + 1
        return depth[gid]

    for gid in gids:
        _d(gid)
    return depth


# ── Column placement ──────────────────────────────────────────────────────────


def _place_column(
    col_gids: list[int],
    cx: float,
    up: dict[int, set[int]],
    down: dict[int, set[int]],
    placed_cy: dict[int, float],  # gid → center-Y of placed group
    by_gid: dict[int, _Group],
    chain_top: float,  # minimum Y for this chain
) -> None:
    """Place groups in one column using target-then-push Y strategy."""
    targets: list[tuple[float, int]] = []

    for gid in col_gids:
        ups = [placed_cy[u] for u in up[gid] if u in placed_cy]
        dws = [placed_cy[d] for d in down[gid] if d in placed_cy]
        if ups:
            ty = sum(ups) / len(ups)
        elif dws:
            ty = sum(dws) / len(dws)
        else:
            ty = chain_top + _group_h(by_gid[gid]) / 2
        targets.append((ty, gid))

    targets.sort(key=lambda t: t[0])

    occupied: list[tuple[float, float]] = []  # (top, bottom) of placed groups

    for ty, gid in targets:
        g = by_gid[gid]
        gh = _group_h(g)
        y = ty - gh / 2

        # Never go above the chain start
        y = max(y, chain_top)

        # Push down if overlapping an already-placed group
        for top, bot in occupied:
            if y < bot + _GROUP_GAP and y + gh > top - _GROUP_GAP:
                y = bot + _GROUP_GAP

        occupied.append((y, y + gh))
        placed_cy[gid] = y + gh / 2
        _apply_group(g, cx, y)


# ── Public entry point ────────────────────────────────────────────────────────


def layout_nodes(graph: Any, node_list: list[Any] | None = None) -> None:
    """Hierarchical group-first layout.

    Each BackdropNode + its contained nodes form an atomic group.
    Groups are placed by depth column derived from group-to-group wires.
    Disconnected chains stack vertically. Empty backdrops are left in place.
    """
    if node_list:
        expanded: set = set()

        def _collect(node) -> None:
            if node.id in expanded:
                return
            expanded.add(node.id)
            if not hasattr(node, "input_ports"):
                return
            for port in node.input_ports():
                for cp in port.connected_ports():
                    _collect(cp.node())
            if hasattr(node, "output_ports"):
                for port in node.output_ports():
                    for cp in port.connected_ports():
                        _collect(cp.node())

        for n in node_list:
            _collect(n)
        all_nodes = [n for n in graph.all_nodes() if n.id in expanded]
    else:
        all_nodes = list(graph.all_nodes())

    if not all_nodes:
        return

    backdrops = [n for n in all_nodes if not hasattr(n, "input_ports")]
    all_n = [n for n in all_nodes if hasattr(n, "input_ports")]

    if not all_n:
        return

    # Canvas anchor: preserve top-left of pre-layout graph
    start_x = min(n.pos()[0] for n in all_n)
    start_y = min(n.pos()[1] for n in all_n)

    # Build groups — backdrop.nodes() for correct containment
    groups = _build_groups(all_n, backdrops)
    node_to_gid = {n.id: g.gid for g in groups for n in g.nodes}
    by_gid = {g.gid: g for g in groups}

    up, down = _build_group_graph(groups, node_to_gid)

    # Sort components: largest first (anchors the top of the canvas)
    components = _find_group_components(groups, up, down)
    components.sort(key=lambda c: -len(c))

    cursor_y = start_y

    for comp_gids in components:
        if not comp_gids:
            continue

        depth = _topo_depth(comp_gids, up)
        columns: dict[int, list[int]] = {}
        for gid in comp_gids:
            columns.setdefault(depth[gid], []).append(gid)

        sorted_cols = sorted(columns.keys())

        # Assign X positions per column
        cur_x = start_x
        col_x: dict[int, float] = {}
        for ci in sorted_cols:
            col_x[ci] = cur_x
            max_gw = max(_group_w(by_gid[gid]) for gid in columns[ci])
            cur_x += max_gw + _X_GAP

        placed_cy: dict[int, float] = {}

        # Place all columns left → right so upstream targets are known
        for ci in sorted_cols:
            _place_column(columns[ci], col_x[ci], up, down, placed_cy, by_gid, cursor_y)

        # Re-place source groups (no upstream) using now-known downstream positions
        source_col = sorted_cols[0]
        src_gids = [gid for gid in columns[source_col] if not up[gid]]
        if src_gids and len(sorted_cols) > 1:
            for gid in src_gids:
                placed_cy.pop(gid, None)
            _place_column(
                src_gids, col_x[source_col], up, down, placed_cy, by_gid, cursor_y
            )

        # Advance cursor below this chain
        if placed_cy:
            max_bot = max(
                placed_cy[gid] + _group_h(by_gid[gid]) / 2
                for gid in comp_gids
                if gid in placed_cy
            )
            cursor_y = max_bot + _CHAIN_GAP
