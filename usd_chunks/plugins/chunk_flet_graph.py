"""A Flet view of a chunk network. Draws only.

    python -m usd_chunks.plugins.chunk_flet_graph <network.usda>

Every fact on screen comes from `chunk_graph`: which nodes exist, what ports they carry,
what type each port is, which wires run between them, and where the nodes sit. This module
contributes no graph logic at all -- it turns those into rectangles, dots and curves. That
is the test of the split: if a second frontend needs to know a rule the first one knows,
the rule is in the wrong place.

Node positions come from USD, so this and the Qt view show the same arrangement of the
same file rather than two independent guesses at it.
"""
from __future__ import annotations

import sys

import flet as ft
import flet.canvas as cv
from pxr import Usd

from usd_chunks.plugins import chunk_graph

#: One colour per port type, matching the Qt view's vocabulary.
PORT_COLOR = {
    chunk_graph.PORT_EXEC: "#e0e0e0",
    chunk_graph.PORT_FLOAT: "#7fbf7f",
    chunk_graph.PORT_INT: "#5fa8d3",
    chunk_graph.PORT_BOOL: "#c98bdb",
    chunk_graph.PORT_STRING: "#d9a05b",
    chunk_graph.PORT_COLOR: "#d76b6b",
    chunk_graph.PORT_VECTOR: "#d4c95e",
}

NODE_W, HEADER_H, ROW_H = 210.0, 30.0, 16.0
PORT_R = 5.0
MARGIN = 60.0


def _rows(ports):
    """Input rows down the left, output rows down the right, exec first."""
    ins = [p for p in ports if p.is_input]
    outs = [p for p in ports if not p.is_input]
    key = lambda p: (0 if p.port_type == chunk_graph.PORT_EXEC else 1, p.name)  # noqa: E731
    return sorted(ins, key=key), sorted(outs, key=key)


def _port_xy(port, origin, ports):
    """Where a port's dot lands, given its node's top-left corner.

    Placement is the view's business -- the engine says a port exists and what it carries,
    not where to draw it. Qt derives Y from live widget geometry; here it is a simple row,
    and both are legitimate answers to a question the engine does not ask.
    """
    ins, outs = _rows(ports)
    x0, y0 = origin
    if port.is_input:
        index = ins.index(port)
        return x0, y0 + HEADER_H + ROW_H * (index + 0.5)
    index = outs.index(port)
    return x0 + NODE_W, y0 + HEADER_H + ROW_H * (index + 0.5)


def build_shapes(prims, ports, edges):
    """The whole picture as Flet canvas shapes."""
    positions = {path: chunk_graph.node_position(prim) for path, prim in prims.items()}
    if positions:
        min_x = min(x for x, _ in positions.values())
        min_y = min(y for _, y in positions.values())
        positions = {p: (x - min_x + MARGIN, y - min_y + MARGIN)
                     for p, (x, y) in positions.items()}

    shapes = []

    # Wires first, so nodes sit on top of them.
    for edge in edges:
        src_node, dst_node = edge.src_node_path, edge.dst_node_path
        if src_node not in positions or dst_node not in positions:
            continue
        src = next((p for p in ports[src_node] if p.attr_path == edge.src_attr_path), None)
        dst = next((p for p in ports[dst_node] if p.attr_path == edge.dst_attr_path), None)
        if src is None or dst is None:
            continue
        x1, y1 = _port_xy(src, positions[src_node], ports[src_node])
        x2, y2 = _port_xy(dst, positions[dst_node], ports[dst_node])
        colour = "#f2c14e" if edge.expression else PORT_COLOR.get(src.port_type, "#888888")
        width = 2.0 if edge.is_exec else 1.2
        # A horizontal-tangent cubic, which is what reads as a node-graph wire.
        reach = max(40.0, abs(x2 - x1) * 0.5)
        path = cv.Path(
            [cv.Path.MoveTo(x1, y1),
             cv.Path.CubicTo(x1 + reach, y1, x2 - reach, y2, x2, y2)],
            paint=ft.Paint(stroke_width=width, color=colour,
                           style=ft.PaintingStyle.STROKE),
        )
        shapes.append(path)

    for path_str, prim in prims.items():
        x, y = positions[path_str]
        node_ports = ports[path_str]
        ins, outs = _rows(node_ports)
        height = HEADER_H + ROW_H * max(len(ins), len(outs), 1) + 8
        disabled = chunk_graph.is_disabled(prim)

        shapes.append(cv.Rect(x, y, NODE_W, height, border_radius=6,
                              paint=ft.Paint(color="#3a3a3a" if not disabled
                                             else "#2a2a2a")))
        shapes.append(cv.Rect(x, y, NODE_W, HEADER_H, border_radius=6,
                              paint=ft.Paint(color="#4a5a6a" if not disabled else "#33383d")))
        shapes.append(cv.Rect(x, y, NODE_W, height, border_radius=6,
                              paint=ft.Paint(stroke_width=1, color="#101010",
                                             style=ft.PaintingStyle.STROKE)))
        shapes.append(cv.Text(x + 10, y + 7, chunk_graph.node_label(prim),
                              ft.TextStyle(size=13, color="#ffffff",
                                           weight=ft.FontWeight.W_500)))
        kind = chunk_graph.node_kind(prim)
        if kind:
            shapes.append(cv.Text(x + NODE_W - 10, y + 10, kind,
                                  ft.TextStyle(size=9, color="#9fb0c0"),
                                  alignment=ft.Alignment.TOP_RIGHT))

        for port in node_ports:
            px, py = _port_xy(port, (x, y), node_ports)
            shapes.append(cv.Circle(px, py, PORT_R,
                                    paint=ft.Paint(color=PORT_COLOR.get(port.port_type,
                                                                        "#888888"))))
            label_x = px + PORT_R + 4 if port.is_input else px - PORT_R - 4
            shapes.append(cv.Text(
                label_x, py - 7, port.name,
                ft.TextStyle(size=9, color="#b9b9b9"),
                alignment=ft.Alignment.TOP_LEFT if port.is_input else ft.Alignment.TOP_RIGHT))

    width = max((positions[p][0] for p in positions), default=0) + NODE_W + MARGIN * 2
    height = max((positions[p][1] for p in positions), default=0) + 300
    return shapes, width, height


def contexts(stage) -> list[str]:
    """Every prim path that holds Chunk prims, outermost first.

    A chunkgroup is both a node in its parent's graph and a graph of its own, so a file
    has several of these and they nest.
    """
    found, pending = [], [chunk_graph.CHUNKS_ROOT]
    while pending:
        root = pending.pop(0)
        nodes = chunk_graph.resolve_graph_nodes(stage, root)
        if not nodes:
            continue
        found.append(root)
        pending += [str(p.GetPath()) for p in nodes]
    return found


def read(network: str, context: str | None = None):
    """Stage, nodes, ports and edges for ONE context.

    One, not all of them. A chunkgroup appears as a single node in its parent's graph and
    as a whole graph when you dive into it -- drawing every context onto one canvas shows
    the group node sitting beside its own children, which is two views of the same thing
    at once and reads as duplication. Qt shows one context at a time and so does this.

    The stage comes back too, deliberately. A Usd.Prim does not keep its stage alive, so
    returning only prims lets the stage be collected the moment this returns and every prim
    expires -- surfacing later as "accessed invalid expired prim", nowhere near the cause.
    """
    stage = Usd.Stage.Open(network)
    available = contexts(stage)
    if context is None:
        # Default to the innermost context on the outermost chain: a file whose top level
        # holds nothing but one chunkgroup is asking to be opened inside it.
        context = available[0] if available else chunk_graph.CHUNKS_ROOT
        while True:
            nodes = chunk_graph.resolve_graph_nodes(stage, context)
            if len(nodes) == 1 and str(nodes[0].GetPath()) in available:
                context = str(nodes[0].GetPath())
                continue
            break
    prims, ports, edges = chunk_graph.read_graph(stage, context)
    return stage, prims, ports, edges, context, available


def main(page: ft.Page, network: str, chosen_context: str | None = None):
    page.title = f"Chunk graph — {network.rsplit('/', 1)[-1]}"
    page.bgcolor = "#232323"
    page.padding = 0

    page.stage, prims, ports, edges, context, available = read(network, chosen_context)
    shapes, width, height = build_shapes(prims, ports, edges)

    page.add(
        ft.Column([
            ft.Container(
                ft.Text(f"{context}   —   {len(prims)} nodes · {len(edges)} wires · "
                        f"{sum(len(v) for v in ports.values())} ports · "
                        f"{len(available)} context(s) in file   —   drawn from "
                        f"chunk_graph, no Qt",
                        size=12, color="#9fb0c0"),
                padding=10, bgcolor="#1b1b1b",
            ),
            ft.Container(
                cv.Canvas(shapes, width=width, height=height),
                expand=True,
            ),
        ], expand=True, scroll=ft.ScrollMode.AUTO)
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m usd_chunks.plugins.chunk_flet_graph <network.usda>")
    target = sys.argv[1]
    wanted = sys.argv[2] if len(sys.argv) > 2 else None
    ft.run(lambda page: main(page, target, wanted))
