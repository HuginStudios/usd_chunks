"""Pure auto-layout math for the node graphs, with no Qt or USD dependency.

That keeps it unit-testable without a real scene or stage. The frontends supply the rest:
node sizes, position persistence, and the connection data (exec and typed alike), which
`chunk_graph.layout_graph` assembles before calling in here.

Topological depth assigns X-columns, and within a column each node's target Y is the
average Y of its already-placed upstream and downstream neighbours, pushed down only on
collision. Chunkgroups are subnets, not spatial containers, so the layout works on
individual nodes with no grouping concept at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

X_GAP = 65  # horizontal gap between depth columns
Y_GAP = 55  # vertical gap between nodes in the same column (or around a locked obstacle)
CHAIN_GAP = 60  # vertical gap between disconnected components

SizeOf = Callable[[str], tuple[float, float]]
PosOf = Callable[[str], tuple[float, float]]
IsLocked = Callable[[str], bool]
AdjacencyMap = dict[str, set[str]]


def find_connected_components(node_ids: list[str], up_of: AdjacencyMap, down_of: AdjacencyMap) -> list[list[str]]:
    """Union-find over node_ids using only edges where both ends are in node_ids."""
    ids = list(node_ids)
    id_set = set(ids)
    parent = {nid: nid for nid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for nid in ids:
        for u in up_of.get(nid, ()):
            if u in id_set:
                union(nid, u)
        for d in down_of.get(nid, ()):
            if d in id_set:
                union(nid, d)

    buckets: dict[str, list[str]] = {}
    for nid in ids:
        buckets.setdefault(find(nid), []).append(nid)
    return list(buckets.values())


def topo_depth(ids: list[str], up_of: AdjacencyMap) -> dict[str, int]:
    """Longest-path-from-a-source depth, restricted to upstream neighbors within ids."""
    id_set = set(ids)
    depth: dict[str, int] = {}

    def _d(nid: str) -> int:
        if nid in depth:
            return depth[nid]
        depth[nid] = 0  # cycle guard: a node re-entered mid-recursion reads depth 0, not infinite
        md = -1
        for u in up_of.get(nid, ()):
            if u in id_set:
                md = max(md, _d(u))
        depth[nid] = md + 1
        return depth[nid]

    for nid in ids:
        _d(nid)
    return depth


def _rects_overlap(
    ax: float, ay: float, aw: float, ah: float, bx: float, by: float, bw: float, bh: float, gap: float
) -> bool:
    return ax < bx + bw + gap and ax + aw + gap > bx and ay < by + bh + gap and ay + ah + gap > by


def layout_positions(
    node_ids: list[str],
    size_of: SizeOf,
    is_locked: IsLocked,
    current_pos: PosOf,
    up_of: AdjacencyMap,
    down_of: AdjacencyMap,
    origin: tuple[float, float] | None = None,
    obstacles: Iterable[tuple[float, float, float, float]] = (),
) -> dict[str, tuple[float, float]]:
    """Compute new positions for every node in `node_ids` that is not locked.

    Locked nodes are never included in the result -- callers must leave them exactly where
    they are -- but they still participate as fixed reference points for target-Y averaging
    (so a movable neighbor visually gravitates toward a locked node it's wired to) and as
    obstacles: a movable node is nudged down if its computed rect would overlap any locked
    node's real (unchanged) rect, checked as an actual 2D bounding-box test rather than a
    same-column-only approximation, since a locked node's real X may not line up with
    whichever column is currently being placed.

    Args:
        node_ids: The nodes to lay out.
        size_of: Takes a node id and returns its (width, height).
        is_locked: Takes a node id and returns whether it must stay put.
        current_pos: Takes a node id and returns its current (x, y).
        up_of: Upstream neighbours of each node. Must only contain edges where BOTH ends are
            in node_ids -- callers building them from a "grow selection to its full connected
            subgraph" BFS closure already guarantee this, so this function doesn't filter
            external references itself.
        down_of: Downstream neighbours of each node, under the same rule as `up_of`.
        origin: The (x, y) the layout starts at, top left. None starts where the nodes
            already are: the leftmost x and topmost y among them.
        obstacles: More (x, y, w, h) rects no node may land on, kept clear of the same way
            as a locked node's, without being anyone's neighbour.

    Returns:
        {node_id: (x, y)} for every movable node.
    """
    if not node_ids:
        return {}

    movable = [nid for nid in node_ids if not is_locked(nid)]
    locked = [nid for nid in node_ids if is_locked(nid)]
    if not movable:
        return {}

    if origin is not None:
        start_x, start_y = origin
    else:
        start_x = min(current_pos(nid)[0] for nid in node_ids)
        start_y = min(current_pos(nid)[1] for nid in node_ids)

    placed_cy: dict[str, float] = {}
    locked_rects: list[tuple[float, float, float, float]] = []  # (x, y, w, h)
    for nid in locked:
        x, y = current_pos(nid)
        w, h = size_of(nid)
        placed_cy[nid] = y + h / 2
        locked_rects.append((x, y, w, h))
    locked_rects.extend(obstacles)

    components = find_connected_components(movable, up_of, down_of)
    components.sort(key=len, reverse=True)  # largest first, anchors the top of the canvas

    result: dict[str, tuple[float, float]] = {}
    cursor_y = start_y

    for comp_ids in components:
        depth = topo_depth(comp_ids, up_of)
        columns: dict[int, list[str]] = {}
        for nid in comp_ids:
            columns.setdefault(depth[nid], []).append(nid)
        sorted_cols = sorted(columns.keys())

        cur_x = start_x
        col_x: dict[int, float] = {}
        for ci in sorted_cols:
            col_x[ci] = cur_x
            max_w = max(size_of(nid)[0] for nid in columns[ci])
            cur_x += max_w + X_GAP

        def _place_column(col_ids: list[str], cx: float, chain_top: float) -> None:
            targets: list[tuple[float, str]] = []
            for nid in col_ids:
                ups = [placed_cy[u] for u in up_of.get(nid, ()) if u in placed_cy]
                dws = [placed_cy[d] for d in down_of.get(nid, ()) if d in placed_cy]
                if ups:
                    ty = sum(ups) / len(ups)
                elif dws:
                    ty = sum(dws) / len(dws)
                else:
                    _, h = size_of(nid)
                    ty = chain_top + h / 2
                targets.append((ty, nid))
            targets.sort(key=lambda t: t[0])

            occupied: list[tuple[float, float]] = []
            for ty, nid in targets:
                w, h = size_of(nid)
                y = ty - h / 2
                y = max(y, chain_top)
                for top, bot in occupied:
                    if y < bot + Y_GAP and y + h > top - Y_GAP:
                        y = bot + Y_GAP
                moved = True
                while moved:
                    moved = False
                    for lx, ly, lw, lh in locked_rects:
                        if _rects_overlap(cx, y, w, h, lx, ly, lw, lh, Y_GAP):
                            y = ly + lh + Y_GAP
                            moved = True
                occupied.append((y, y + h))
                placed_cy[nid] = y + h / 2
                result[nid] = (cx, y)

        for ci in sorted_cols:
            _place_column(columns[ci], col_x[ci], cursor_y)

        # Re-place source nodes (no upstream at all, in the first column) toward their now-
        # known downstream targets -- the concrete mechanism behind "source nodes pulled close
        # to their targets".
        source_col = sorted_cols[0]
        src_ids = [nid for nid in columns[source_col] if not up_of.get(nid)]
        if src_ids and len(sorted_cols) > 1:
            for nid in src_ids:
                placed_cy.pop(nid, None)
                result.pop(nid, None)
            _place_column(src_ids, col_x[source_col], cursor_y)

        if comp_ids:
            bottoms = [result[nid][1] + size_of(nid)[1] for nid in comp_ids if nid in result]
            cursor_y = (max(bottoms) if bottoms else cursor_y) + CHAIN_GAP

    return result


Rect = tuple[float, float, float, float]


@dataclass
class Box:
    """A backdrop, for layout_nested.

    Attributes:
        nodes: The nodes directly in it, not in a smaller box inside it.
        children: The boxes directly in it.
        rect: Where it is now: (x, y, w, h).
        locked: It must not move.
    """

    nodes: list[str]
    children: list[str] = field(default_factory=list)
    rect: Rect = (0.0, 0.0, 0.0, 0.0)
    locked: bool = False


def layout_nested(
    node_ids: list[str],
    size_of: SizeOf,
    is_locked: IsLocked,
    current_pos: PosOf,
    up_of: AdjacencyMap,
    down_of: AdjacencyMap,
    boxes: dict[str, Box],
    inner_origin: tuple[float, float],
    pad: float,
    inside: str | None = None,
    obstacles: Iterable[Rect] = (),
) -> tuple[dict[str, tuple[float, float]], dict[str, Rect]]:
    """Lay out nodes that sit in boxes (backdrops), boxes inside boxes included, without
    pulling any box apart.

    A box's contents are laid out inside it first: its nodes, and its own boxes, each laid
    out the same way and placed as one block. Then the box is one block where it sits, the
    size it now needs, wired to whatever is inside it is wired to. A box holding a locked
    node or a locked box, or locked itself, stays where it is, contents and all, and is an
    obstacle like a locked node.

    Args:
        node_ids: The nodes to lay out.
        size_of: Takes a node id and returns its (width, height).
        is_locked: Takes a node id and returns whether it must stay put.
        current_pos: Takes a node id and returns its current (x, y).
        up_of: Upstream neighbours of each node, both ends in node_ids.
        down_of: Downstream neighbours of each node, both ends in node_ids.
        boxes: Box id -> Box. A node is directly in one box at most. Box ids must not be
            node ids.
        inner_origin: Where a box's contents start, from its top-left corner.
        pad: Space left between a box's contents and its right and bottom edges.
        inside: Lay out only what is in this box, from its top-left corner, and fit the box
            around it. None: node_ids and the boxes they are in, as a whole graph.
        obstacles: (x, y, w, h) rects nothing may land on.

    Returns:
        ({node id: (x, y)} for every node that moves, {box id: (x, y, w, h)} for every box
        that moves or is resized).
    """
    id_set = set(node_ids)
    under: dict[str, list[str]] = {}
    frozen: dict[str, bool] = {}

    def nodes_under(b: str) -> list[str]:
        if b not in under:
            found = [n for n in boxes[b].nodes if n in id_set]
            for c in boxes[b].children:
                found += nodes_under(c)
            under[b] = found
        return under[b]

    def is_frozen(b: str) -> bool:
        if b not in frozen:
            box = boxes[b]
            frozen[b] = (box.locked or any(is_locked(n) for n in box.nodes if n in id_set)
                         or any(is_frozen(c) for c in box.children))
        return frozen[b]

    def lay(nodes, children, origin, keep_clear):
        """Lay out nodes and boxes side by side. Returns positions, rects, and the bottom
        right corner of everything placed, or None when there was nothing."""
        nodes = [n for n in nodes if n in id_set]
        children = [c for c in children if nodes_under(c)]
        items = nodes + children
        if not items:
            return {}, {}, None
        inner = {}
        for c in children:
            if not is_frozen(c):
                rel_pos, rel_rects, corner = lay(boxes[c].nodes, boxes[c].children, inner_origin, ())
                inner[c] = (rel_pos, rel_rects, (corner[0] + pad, corner[1] + pad))
        rep = {n: n for n in nodes}
        for c in children:
            rep.update((n, c) for n in nodes_under(c))

        def size(i):
            if i in inner:
                return inner[i][2]
            return (boxes[i].rect[2], boxes[i].rect[3]) if i in boxes else size_of(i)

        def pos(i):
            return (boxes[i].rect[0], boxes[i].rect[1]) if i in boxes else current_pos(i)

        def locked(i):
            return is_frozen(i) if i in boxes else is_locked(i)

        item_up: AdjacencyMap = {}
        item_down: AdjacencyMap = {}
        for n, r in rep.items():
            for u in up_of.get(n, ()):
                if u in rep and rep[u] != r:
                    item_up.setdefault(r, set()).add(rep[u])
                    item_down.setdefault(rep[u], set()).add(r)
        placed = layout_positions(items, size, locked, pos, item_up, item_down, origin=origin,
                                  obstacles=keep_clear)
        positions: dict[str, tuple[float, float]] = {}
        rects: dict[str, Rect] = {}
        for i, (x, y) in placed.items():
            if i in inner:
                rel_pos, rel_rects, (w, h) = inner[i]
                rects[i] = (x, y, w, h)
                positions.update((n, (x + rx, y + ry)) for n, (rx, ry) in rel_pos.items())
                rects.update((k, (x + r[0], y + r[1], r[2], r[3])) for k, r in rel_rects.items())
            else:
                positions[i] = (x, y)
        corner = (max(placed.get(i, pos(i))[0] + size(i)[0] for i in items),
                  max(placed.get(i, pos(i))[1] + size(i)[1] for i in items))
        return positions, rects, corner

    if inside is None:
        in_a_box = {n for box in boxes.values() for n in box.nodes}
        nested = {c for box in boxes.values() for c in box.children}
        loose = [n for n in node_ids if n not in in_a_box]
        positions, rects, _ = lay(loose, [b for b in boxes if b not in nested], None, obstacles)
        return positions, rects

    box = boxes[inside]
    x, y = box.rect[0], box.rect[1]
    positions, rects, corner = lay(box.nodes, box.children, (x + inner_origin[0], y + inner_origin[1]), obstacles)
    if corner is not None:
        rects[inside] = (x, y, corner[0] + pad - x, corner[1] + pad - y)
    return positions, rects


def grow_to_connected_subgraph(seed_ids: list[str], up_of: AdjacencyMap, down_of: AdjacencyMap) -> set[str]:
    """Every node reachable from the seeds through any connection, up or down.

    The maps are the FULL adjacency and may reference ids outside `seed_ids` -- that is the
    point: this is how "layout selected" finds everything the selection is wired to.

    Args:
        seed_ids: The nodes to start from.
        up_of: Upstream neighbours of each node.
        down_of: Downstream neighbours of each node.

    Returns:
        Every node reached, the seeds included.
    """
    visited: set[str] = set(seed_ids)
    frontier = list(seed_ids)
    while frontier:
        nid = frontier.pop()
        neighbors = up_of.get(nid, set()) | down_of.get(nid, set())
        for neighbor in neighbors:
            if neighbor not in visited:
                visited.add(neighbor)
                frontier.append(neighbor)
    return visited
