"""A Flet view of a chunk network: draws it, and dispatches input into the engine.

    python -m usd_chunks.plugins.chunk_flet_graph <network.usda> [context_prim_path]

    click a port          select it
    click a second port   connect, if chunk_graph says the pair is legal
    right-click a port    disconnect it
    drag a node           move it; the position is written back to USD
    click empty space     deselect

Every rule lives in `chunk_graph`: which nodes exist, what ports they have, what type each
carries, whether a wire is legal, and what authoring one does to the stage. This module
knows only where things sit on screen -- hit-testing and pixels. That division is the point:
if this file ever needs to know a rule the Qt view knows, the rule is in the wrong place.

Node positions come from USD, so this and the Qt view show the same arrangement of the same
file, and a node dragged here has moved there too.

One honest gap: authoring a *param* wire also wants the destination param set to an
`@Node.param@` expression, and that needs the live chunk objects, which this view does not
load -- it reads a stage, not a session. Exec wiring is complete; a param wire authored here
connects but does not yet write the expression.
"""
from __future__ import annotations

import sys

import flet as ft
import flet.canvas as cv
from pxr import Gf, Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks.plugins import chunk_graph, chunk_usd
from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, load_chunks

#: One colour per port type, matching the Qt view's vocabulary.
PORT_COLOUR = {
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
GRAB_R = 11.0  # click tolerance, wider than the dot so a port is not fiddly to hit
MARGIN = 60.0


class _Scene(SceneSender):
    """The minimum a chunk session needs to exist: a stage and a selection."""

    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def contexts(stage) -> list[str]:
    """Every prim path holding Chunk prims, outermost first.

    A chunkgroup is both a node in its parent's graph and a graph of its own, so a file has
    several of these and they nest.
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


def default_context(stage) -> str:
    """The innermost context on the outermost chain.

    A file whose top level holds nothing but one chunkgroup is asking to be opened inside
    it, the same way double-clicking that group in Qt dives into it.
    """
    available = contexts(stage)
    context = available[0] if available else chunk_graph.CHUNKS_ROOT
    while True:
        nodes = chunk_graph.resolve_graph_nodes(stage, context)
        if len(nodes) == 1 and str(nodes[0].GetPath()) in available:
            context = str(nodes[0].GetPath())
            continue
        return context


class GraphView:
    """Pixels and pointer events. Every decision belongs to chunk_graph."""

    def __init__(self, network: str, context: str | None = None):
        self.network = network
        # Held for the life of the view: a Usd.Prim does not keep its stage alive, so
        # dropping this collects the stage and expires every prim -- which surfaces later
        # as "accessed invalid expired prim", nowhere near the cause.
        self.stage = Usd.Stage.Open(network)
        # A live session, the same as Qt has. Without one the view can still draw -- ports
        # and wires come off the prims -- but two things quietly differ: a Menu and a
        # LineEdit are both strings in USD, so only the chunk knows which port type to
        # show, and a param wire needs its @Node.param@ expression written onto the
        # destination param, which is a chunk operation. Falling back rather than failing,
        # because a stage is still worth looking at when the modules cannot be found.
        self.chunks: dict = {}
        self.session_error = ""
        try:
            self.scene = _Scene(self.stage)
            chunk_registry.ChunkRegistry().initialize(self.scene, "flet")
            ChunkLayerManager().initialize(self.stage)
            chunk_usd.ChunkStageSync().initialize(self.stage, self.scene)
            load_chunks(self.stage, self.scene)
            self.chunks = {get_chunk_prim_path(c): c for c in self.scene.get_chunks()}
        except Exception as exc:  # noqa: BLE001 -- a broken session must not stop drawing
            self.session_error = f"no chunk session ({type(exc).__name__}); drawing only"

        self.available = contexts(self.stage)
        self.context = context or default_context(self.stage)
        self.selected: chunk_graph.PortRef | None = None
        self.status = ""
        self.dragging: str | None = None
        self.wiring: chunk_graph.PortRef | None = None
        self.wire_to = (0.0, 0.0)
        self.canvas: cv.Canvas | None = None
        self.status_text: ft.Text | None = None
        self.reload()

    # -- state -------------------------------------------------------------

    def reload(self):
        self.prims, self.ports, self.edges = chunk_graph.read_graph(
            self.stage, self.context, self.chunks)
        raw = {path: chunk_graph.node_position(prim) for path, prim in self.prims.items()}
        # Screen space is USD space shifted so the top-left node sits at MARGIN. The offset
        # is kept because a drag has to be written back in the coordinates USD stores.
        self.origin = (min((x for x, _ in raw.values()), default=0.0) - MARGIN,
                       min((y for _, y in raw.values()), default=0.0) - MARGIN)
        self.positions = {p: (x - self.origin[0], y - self.origin[1])
                          for p, (x, y) in raw.items()}

    def rows(self, path: str):
        node_ports = self.ports[path]
        ins = [p for p in node_ports if p.is_input]
        outs = [p for p in node_ports if not p.is_input]
        key = lambda p: (0 if p.port_type == chunk_graph.PORT_EXEC else 1, p.name)  # noqa: E731
        return sorted(ins, key=key), sorted(outs, key=key)

    def node_height(self, path: str) -> float:
        ins, outs = self.rows(path)
        return HEADER_H + ROW_H * max(len(ins), len(outs), 1) + 8

    def port_xy(self, port: chunk_graph.PortRef):
        """Where a port's dot lands.

        Placement is the view's business: the engine says a port exists and what it
        carries, not where to draw it. Qt derives Y from live widget geometry; here it is a
        row, and both are legitimate answers to a question the engine does not ask.
        """
        ins, outs = self.rows(port.node_path)
        x0, y0 = self.positions[port.node_path]
        if port.is_input:
            return x0, y0 + HEADER_H + ROW_H * (ins.index(port) + 0.5)
        return x0 + NODE_W, y0 + HEADER_H + ROW_H * (outs.index(port) + 0.5)

    # -- hit testing -------------------------------------------------------

    def port_at(self, x: float, y: float):
        best, best_distance = None, GRAB_R
        for path in self.prims:
            for port in self.ports[path]:
                px, py = self.port_xy(port)
                distance = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
                if distance <= best_distance:
                    best, best_distance = port, distance
        return best

    def node_at(self, x: float, y: float):
        for path in self.prims:
            nx, ny = self.positions[path]
            if nx <= x <= nx + NODE_W and ny <= y <= ny + self.node_height(path):
                return path
        return None

    # -- input -------------------------------------------------------------

    def on_tap(self, event):
        port = self.port_at(event.local_x, event.local_y)
        if port is None:
            self.selected, self.status = None, ""
            return self.refresh()

        if self.selected is None:
            self.selected = port
            self.status = f"selected {port.name} ({port.port_type})"
            return self.refresh()

        if port.attr_path == self.selected.attr_path:
            self.selected, self.status = None, ""
            return self.refresh()

        change = chunk_graph.connect(self.stage, self.selected, port, self.chunks)
        if change is None:
            # The engine's rule, reported rather than re-derived here.
            self.status = (f"cannot connect {self.selected.name} "
                           f"({self.selected.port_type}) to {port.name} ({port.port_type})")
        else:
            self.stage.GetRootLayer().Save()
            self.status = (f"connected {self.selected.name} -> {port.name}"
                           + (f" (expression refused: {change.expression_error})"
                              if change.expression_error else ""))
            self.reload()
        self.selected = None
        self.refresh()

    def on_secondary_tap(self, event):
        port = self.port_at(event.local_x, event.local_y)
        if port is None:
            return
        change = chunk_graph.disconnect(self.stage, port, self.prims, self.chunks)
        if change.edges_dirty:
            self.stage.GetRootLayer().Save()
            self.status = f"disconnected {port.name}"
            self.reload()
        else:
            self.status = f"{port.name} had nothing to disconnect"
        self.selected = None
        self.refresh()

    def on_pan_start(self, event):
        # A grab on a port starts a wire; anywhere else on a node moves it.
        port = self.port_at(event.local_x, event.local_y)
        if port is not None:
            self.wiring = port
            self.wire_to = (event.local_x, event.local_y)
            self.dragging = None
            return self.refresh()
        self.dragging = self.node_at(event.local_x, event.local_y)

    def on_pan_update(self, event):
        if self.wiring is not None:
            x, y = self.wire_to
            self.wire_to = (x + event.delta_x, y + event.delta_y)
            return self.refresh()
        if not self.dragging:
            return
        x, y = self.positions[self.dragging]
        self.positions[self.dragging] = (x + event.delta_x, y + event.delta_y)
        self.refresh()

    def on_pan_end(self, event):
        if self.wiring is not None:
            source, self.wiring = self.wiring, None
            target = self.port_at(*self.wire_to)
            if target is None:
                self.status = ""
            elif target.attr_path == source.attr_path:
                self.status = ""
            else:
                change = chunk_graph.connect(self.stage, source, target, self.chunks)
                if change is None:
                    self.status = (f"cannot connect {source.name} ({source.port_type}) "
                                   f"to {target.name} ({target.port_type})")
                else:
                    self.stage.GetRootLayer().Save()
                    self.status = (f"connected {source.name} -> {target.name}"
                                   + (f" (expression refused: {change.expression_error})"
                                      if change.expression_error else ""))
                    self.reload()
            self.selected = None
            return self.refresh()
        if not self.dragging:
            return
        # Back into USD, in USD's own coordinates. The position is the shared state that
        # makes this view and the Qt one show the same arrangement of the same file.
        x, y = self.positions[self.dragging]
        prim = self.prims[self.dragging]
        attr = prim.GetAttribute(chunk_graph.POS_ATTR)
        if not attr:
            attr = prim.CreateAttribute(chunk_graph.POS_ATTR, Sdf.ValueTypeNames.Float2)
        attr.Set(Gf.Vec2f(float(x + self.origin[0]), float(y + self.origin[1])))
        self.stage.GetRootLayer().Save()
        self.status = f"moved {self.dragging.rsplit('/', 1)[-1]}"
        self.dragging = None
        self.refresh()

    # -- drawing -----------------------------------------------------------

    def shapes(self):
        out = []
        if self.wiring is not None:
            x1, y1 = self.port_xy(self.wiring)
            x2, y2 = self.wire_to
            reach = max(40.0, abs(x2 - x1) * 0.5)
            out.append(cv.Path(
                [cv.Path.MoveTo(x1, y1),
                 cv.Path.CubicTo(x1 + reach, y1, x2 - reach, y2, x2, y2)],
                paint=ft.Paint(stroke_width=1.6, color="#ffffff",
                               style=ft.PaintingStyle.STROKE)))
        for edge in self.edges:
            src_node, dst_node = edge.src_node_path, edge.dst_node_path
            if src_node not in self.positions or dst_node not in self.positions:
                continue
            src = next((p for p in self.ports[src_node]
                        if p.attr_path == edge.src_attr_path), None)
            dst = next((p for p in self.ports[dst_node]
                        if p.attr_path == edge.dst_attr_path), None)
            if src is None or dst is None:
                continue
            x1, y1 = self.port_xy(src)
            x2, y2 = self.port_xy(dst)
            colour = "#f2c14e" if edge.expression else PORT_COLOUR.get(src.port_type, "#888888")
            reach = max(40.0, abs(x2 - x1) * 0.5)
            out.append(cv.Path(
                [cv.Path.MoveTo(x1, y1),
                 cv.Path.CubicTo(x1 + reach, y1, x2 - reach, y2, x2, y2)],
                paint=ft.Paint(stroke_width=2.0 if edge.is_exec else 1.2, color=colour,
                               style=ft.PaintingStyle.STROKE)))

        for path, prim in self.prims.items():
            x, y = self.positions[path]
            height = self.node_height(path)
            off = chunk_graph.is_disabled(prim)
            out.append(cv.Rect(x, y, NODE_W, height, border_radius=6,
                               paint=ft.Paint(color="#2a2a2a" if off else "#3a3a3a")))
            out.append(cv.Rect(x, y, NODE_W, HEADER_H, border_radius=6,
                               paint=ft.Paint(color="#33383d" if off else "#4a5a6a")))
            out.append(cv.Rect(x, y, NODE_W, height, border_radius=6,
                               paint=ft.Paint(stroke_width=1, color="#101010",
                                              style=ft.PaintingStyle.STROKE)))
            out.append(cv.Text(x + 10, y + 7, chunk_graph.node_label(prim),
                               ft.TextStyle(size=13, color="#ffffff",
                                            weight=ft.FontWeight.W_500)))
            kind = chunk_graph.node_kind(prim)
            if kind:
                out.append(cv.Text(x + NODE_W - 10, y + 10, kind,
                                   ft.TextStyle(size=9, color="#9fb0c0"),
                                   alignment=ft.Alignment.TOP_RIGHT))

            for port in self.ports[path]:
                px, py = self.port_xy(port)
                if self.selected is not None and port.attr_path == self.selected.attr_path:
                    out.append(cv.Circle(px, py, PORT_R + 3, paint=ft.Paint(color="#ffffff")))
                out.append(cv.Circle(px, py, PORT_R,
                                     paint=ft.Paint(color=PORT_COLOUR.get(port.port_type,
                                                                          "#888888"))))
                out.append(cv.Text(
                    px + PORT_R + 4 if port.is_input else px - PORT_R - 4, py - 7,
                    port.name, ft.TextStyle(size=9, color="#b9b9b9"),
                    alignment=ft.Alignment.TOP_LEFT if port.is_input
                    else ft.Alignment.TOP_RIGHT))
        return out

    def extent(self):
        width = max((x for x, _ in self.positions.values()), default=0) + NODE_W + MARGIN * 2
        height = max((y for _, y in self.positions.values()), default=0) + 300
        return width, height

    def summary(self):
        return (f"{self.context}   —   {len(self.prims)} nodes · {len(self.edges)} wires · "
                f"{sum(len(v) for v in self.ports.values())} ports · "
                f"{len(self.available)} context(s)"
                + (f"   —   {self.session_error}" if self.session_error else "")
                + (f"   —   {self.status}" if self.status else ""))

    def refresh(self):
        if self.canvas is not None:
            self.canvas.shapes = self.shapes()
            self.canvas.width, self.canvas.height = self.extent()
            self.canvas.update()
        if self.status_text is not None:
            self.status_text.value = self.summary()
            self.status_text.update()


def main(page: ft.Page, network: str, context: str | None = None):
    view = GraphView(network, context)
    page.title = f"Chunk graph — {network.rsplit('/', 1)[-1]}"
    page.bgcolor = "#232323"
    page.padding = 0

    width, height = view.extent()
    view.canvas = cv.Canvas(view.shapes(), width=width, height=height)
    view.status_text = ft.Text(view.summary(), size=12, color="#9fb0c0")

    page.add(
        ft.Column([
            ft.Container(view.status_text, padding=10, bgcolor="#1b1b1b"),
            ft.Container(
                ft.GestureDetector(
                    content=view.canvas,
                    on_tap_down=view.on_tap,
                    on_secondary_tap_down=view.on_secondary_tap,
                    on_pan_start=view.on_pan_start,
                    on_pan_update=view.on_pan_update,
                    on_pan_end=view.on_pan_end,
                ),
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
