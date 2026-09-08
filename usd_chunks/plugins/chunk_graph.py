"""The node graph as data: nodes, ports and edges, with no toolkit attached.

`chunk_qt_nodegraph` grew the graph *model* and the Qt *view* together, but only
superficially -- the connection logic reads three fields off a port item, and the port
typing is pure functions over `Sdf.ValueTypeNames` and the param classes. Everything this
module needs (`chunk_usd`, `chunk_core`, `chunk`, `chunk_notice`, `chunk_registry`,
`chunk_flags`) imports zero Qt, so the model can be lifted out whole.

What that buys, in the order it pays off:

- **A second frontend.** Flet, ImGui, QML, a Houdini panel or a web view can render the
  same graph without reimplementing what a port is or which wires are legal.
- **An offline port schema.** "What ports does this chunk have, and of what type" becomes
  answerable from `main()` alone -- no stage, no window -- which is the catalog an agent
  needs to compose a network, and the metadata a marketplace listing can advertise.
- **Headless connection validity.** Whether a wire is legal can be answered before
  anything cooks, so an agent-authored graph is validated rather than merely plausible.

The split is deliberately *identity, not geometry*: this module says a port exists, what it
is called and what type it carries. Where the dot lands on screen stays with the view,
because in Qt that derives from live widget geometry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pxr import Sdf, Usd

from usd_chunks import chunk_core as core
from usd_chunks.chunk import Checkbox, ColorPicker, FileBrowser, LineEdit, Menu, Slider, Spinbox, TextEdit
from usd_chunks.plugins import chunk_usd

#: Port types. Tokens rather than an enum because they are already persisted as strings in
#: the Qt view's colour table and read by name in ~40 places.
PORT_EXEC = "exec"
PORT_FLOAT = "float"
PORT_INT = "int"
PORT_BOOL = "bool"
PORT_STRING = "string"
PORT_COLOR = "color"
PORT_VECTOR = "vector"

EXEC_INPUT = "inputs:chunk"
EXEC_OUTPUT = "outputs:chunk"
CHUNKS_ROOT = "/chunks"

#: Chunk prims are found with this rather than the default predicate: a disabled chunk is
#: an INACTIVE prim and still belongs on the graph, drawn greyed.
INCLUDE_INACTIVE = Usd.PrimIsDefined & ~Usd.PrimIsAbstract

_EXPRESSION = re.compile(r"@\w+\.\w+@")


def sdf_to_port_type(sdf_type_name) -> str:
    """A USD attribute type as the port token that carries it."""
    if sdf_type_name in (Sdf.ValueTypeNames.Color3f, Sdf.ValueTypeNames.Color4f):
        return PORT_COLOR
    if sdf_type_name in (Sdf.ValueTypeNames.Bool,):
        return PORT_BOOL
    if sdf_type_name in (Sdf.ValueTypeNames.String, Sdf.ValueTypeNames.Token,
                         Sdf.ValueTypeNames.Asset):
        return PORT_STRING
    if sdf_type_name in (Sdf.ValueTypeNames.Int, Sdf.ValueTypeNames.Int2,
                         Sdf.ValueTypeNames.Int3, Sdf.ValueTypeNames.Int4):
        return PORT_INT
    if sdf_type_name in (Sdf.ValueTypeNames.Float2, Sdf.ValueTypeNames.Float3,
                         Sdf.ValueTypeNames.Float4):
        return PORT_VECTOR
    return PORT_FLOAT


def iter_connectable_params(obj: "core.ChunkCommon"):
    """Every param that can carry a wire, recursing into groups.

    A vector yields as ONE param: its components are not individually connectable.
    """
    for param in obj.get_my_params():
        if isinstance(param, (Slider, Spinbox, Checkbox, LineEdit, TextEdit, FileBrowser,
                              ColorPicker, Menu)):
            yield param
        elif isinstance(param, core.ChunkVectorBase):
            yield param
        elif not isinstance(param, core.ChunkParamBase):
            yield from iter_connectable_params(param)


def iter_all_vectors(obj: "core.ChunkCommon"):
    """Every ChunkVectorBase under a chunk or group."""
    for param in obj.get_my_params():
        if isinstance(param, core.ChunkVectorBase):
            yield param
        elif not isinstance(param, core.ChunkParamBase):
            yield from iter_all_vectors(param)


def param_port_type(param) -> str:
    """A param as the port token it presents."""
    if isinstance(param, ColorPicker):
        return PORT_COLOR
    if isinstance(param, core.ChunkVectorBase):
        return PORT_VECTOR
    if isinstance(param, Checkbox):
        return PORT_BOOL
    if isinstance(param, (LineEdit, TextEdit, FileBrowser, Menu)):
        return PORT_STRING
    if param.value is not None and isinstance(param.value, int) and not isinstance(param.value, bool):
        return PORT_INT
    return PORT_FLOAT


@dataclass(frozen=True)
class PortRef:
    """One connection point: which node, which attribute, what it carries.

    `attr_path` is the full `/path/to/prim.inputs:name`, which is what the Qt view already
    keys its port items by and what a connection in USD names -- so a frontend can round
    trip through this without inventing an identifier scheme of its own.
    """

    node_path: str
    attr_path: str
    port_type: str
    is_input: bool

    @property
    def name(self) -> str:
        """The bare param name, which is what a label shows."""
        return self.attr_path.rsplit(":", 1)[-1]

    @property
    def is_exec(self) -> bool:
        suffix = self.attr_path.split(".", 1)[-1]
        return chunk_usd.is_exec_input_name(suffix) or suffix == EXEC_OUTPUT


@dataclass(frozen=True)
class EdgeRef:
    """One wire, named by the attributes at each end.

    A connection in USD is owned by the CONSUMER -- the downstream `inputs:` attribute
    holds the path of what feeds it -- but it is drawn from the producer's output dot. The
    source path here is already rewritten to `outputs:`, so a view can render it directly.
    """

    src_attr_path: str
    dst_attr_path: str
    is_exec: bool
    expression: str | None = None

    @property
    def src_node_path(self) -> str:
        return self.src_attr_path.split(".", 1)[0]

    @property
    def dst_node_path(self) -> str:
        return self.dst_attr_path.split(".", 1)[0]


def resolve_graph_nodes(stage: Usd.Stage, root_path: str = CHUNKS_ROOT) -> list[Usd.Prim]:
    """The chunk prims that are nodes of the graph rooted at root_path.

    Inactive prims are included: a disabled chunk stays on the graph, greyed, rather than
    vanishing and taking its wires with it.
    """
    root = stage.GetPrimAtPath(Sdf.Path(root_path))
    if not root or not root.IsValid():
        return []
    return [prim for prim in root.GetFilteredChildren(INCLUDE_INACTIVE)
            if prim.GetTypeName() == "Chunk"]


def iter_ports(prim: Usd.Prim, chunk=None) -> list[PortRef]:
    """Every port a node presents: the exec pair, then one in/out pair per typed param.

    Works from the prim alone. A chunk object, when given, is the better source for param
    typing because it knows the param CLASS -- a Menu and a LineEdit are both strings in
    USD but only the chunk knows which is which.
    """
    if not prim or not prim.IsValid():
        return []
    path = str(prim.GetPath())
    ports: list[PortRef] = [
        PortRef(path, f"{path}.{EXEC_INPUT}", PORT_EXEC, True),
        PortRef(path, f"{path}.{EXEC_OUTPUT}", PORT_EXEC, False),
    ]

    # Extra exec slots: a merge grows inputs:chunk1, chunk2 ... as branches are wired.
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if name != EXEC_INPUT and chunk_usd.is_exec_input_name(name):
            ports.append(PortRef(path, f"{path}.{name}", PORT_EXEC, True))

    if chunk is not None:
        for param in iter_connectable_params(chunk):
            port_type = param_port_type(param)
            ports.append(PortRef(path, f"{path}.inputs:{param.name}", port_type, True))
            ports.append(PortRef(path, f"{path}.outputs:{param.name}", port_type, False))
        return ports

    # No chunk: fall back to whatever USD declares, typed from the attribute.
    #
    # Both directions per param, not just the one that happens to be authored. A param is
    # connectable either way, and USD only carries the side that has a value or a
    # connection -- so a node feeding its primpath_location downstream has inputs: on disk
    # and no outputs: at all. Deriving ports from the attributes alone then loses the port
    # the wire starts at, and the wire silently does not draw.
    seen = {p.attr_path for p in ports}
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if name in (EXEC_INPUT, EXEC_OUTPUT) or chunk_usd.is_exec_input_name(name):
            continue
        if not (name.startswith("inputs:") or name.startswith("outputs:")):
            continue
        bare = name.split(":", 1)[1]
        port_type = sdf_to_port_type(attr.GetTypeName())
        for prefix, is_input in (("inputs:", True), ("outputs:", False)):
            attr_path = f"{path}.{prefix}{bare}"
            if attr_path not in seen:
                seen.add(attr_path)
                ports.append(PortRef(path, attr_path, port_type, is_input))
    return ports


def build_edges(prims: dict[str, Usd.Prim], chunks: dict | None = None,
                show_param_edges: bool = True) -> list[EdgeRef]:
    """Every wire between the given nodes, read off USD.

    `prims` maps prim path to prim, so a connection pointing outside the set (a node in
    another context, or one already deleted) is dropped rather than drawn into nothing.
    """
    chunks = chunks or {}
    edges: list[EdgeRef] = []
    for path, prim in prims.items():
        if not prim or not prim.IsValid():
            continue
        for attr in prim.GetAttributes():
            name = attr.GetName()
            if not name.startswith("inputs:"):
                continue
            is_exec = chunk_usd.is_exec_input_name(name)
            if not is_exec and not show_param_edges:
                continue

            expression = None
            chunk = chunks.get(path)
            if chunk is not None:
                try:
                    param = chunk.get_param(name[len("inputs:"):])
                    raw = getattr(param, "raw_value", None) if param else None
                    if isinstance(raw, str) and _EXPRESSION.search(raw):
                        expression = raw
                except Exception:
                    expression = None

            for conn in attr.GetConnections():
                if str(conn.GetPrimPath()) not in prims:
                    continue
                # Drawn from the producer's OUTPUT dot even though the connection names
                # whatever attribute the consumer was wired to.
                src = str(conn).replace(".inputs:", ".outputs:", 1)
                edges.append(EdgeRef(src, f"{path}.{name}", is_exec, expression))
    return edges


def build_adjacency(edges: list[EdgeRef], exec_only: bool = True) -> dict[str, list[str]]:
    """{node path: [downstream node paths]} -- the shape a layout or a walk wants."""
    out: dict[str, list[str]] = {}
    for edge in edges:
        if exec_only and not edge.is_exec:
            continue
        out.setdefault(edge.src_node_path, []).append(edge.dst_node_path)
    return out


#: Where a node sits and how big it is, as UsdUINodeGraphNodeAPI records it. Position lives
#: in USD rather than in a per-frontend save file precisely so that two frontends showing
#: the same stage show the same graph -- move a node in Qt and the Flet view agrees.
POS_ATTR = "ui:nodegraph:node:pos"
SIZE_ATTR = "ui:nodegraph:node:size"

DEFAULT_NODE_SIZE = (200.0, 90.0)


def node_position(prim: Usd.Prim, default: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float]:
    attr = prim.GetAttribute(POS_ATTR)
    value = attr.Get() if attr and attr.HasAuthoredValue() else None
    return (float(value[0]), float(value[1])) if value is not None else default


def node_size(prim: Usd.Prim, default: tuple[float, float] = DEFAULT_NODE_SIZE) -> tuple[float, float]:
    attr = prim.GetAttribute(SIZE_ATTR)
    value = attr.Get() if attr and attr.HasAuthoredValue() else None
    return (float(value[0]), float(value[1])) if value is not None else default


def node_label(prim: Usd.Prim) -> str:
    return prim.GetName()


def node_kind(prim: Usd.Prim) -> str:
    attr = prim.GetAttribute("info:chunkType")
    return str(attr.Get()) if attr and attr.Get() else ""


def is_disabled(prim: Usd.Prim) -> bool:
    """A disabled chunk is an inactive prim; it stays on the graph, drawn greyed."""
    return not prim.IsActive()


def read_graph(stage: Usd.Stage, root_path: str = CHUNKS_ROOT, chunks: dict | None = None):
    """Everything a view needs to draw one context: nodes, their ports, and the wires.

    Returns (prims_by_path, {path: [PortRef]}, [EdgeRef]). A frontend calls this and then
    only draws -- no USD walking, no port typing, no connection rules of its own.
    """
    prims = {str(p.GetPath()): p for p in resolve_graph_nodes(stage, root_path)}
    chunks = chunks or {}
    ports = {path: iter_ports(prim, chunks.get(path)) for path, prim in prims.items()}
    return prims, ports, build_edges(prims, chunks)


def can_connect(a: PortRef, b: PortRef) -> bool:
    """Is a wire between these two legal?

    One end has to be an input and the other an output, they cannot belong to the same
    node, and exec only meets exec. Everything else is allowed to convert: an int into a
    float port is a coercion the param system already performs, and refusing it here would
    make the graph stricter than the thing it draws.
    """
    if a.is_input == b.is_input:
        return False
    if a.node_path == b.node_path:
        return False
    return (a.port_type == PORT_EXEC) == (b.port_type == PORT_EXEC)
