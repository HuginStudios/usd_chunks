"""The node graph as data: nodes, ports and edges, with no toolkit attached.

It depends on `chunk_names` for the chunk vocabulary and reaches for `chunk_usd` only
inside the one function that needs a live session, so the reading and typing half runs with
no stage machinery loaded at all.

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
from usd_chunks import chunk_utils
from usd_chunks.chunk import Checkbox, ColorPicker, FileBrowser, LineEdit, Menu, Slider, Spinbox, TextEdit
from usd_chunks import chunk_names

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
CHUNKS_ROOT = chunk_names.CHUNKS_ROOT

#: The prim type a chunk is, and the root scope, both taken from chunk_usd rather than
#: spelled out again -- renaming either there should not leave the graph quietly finding
#: nothing. Bound directly, not via getattr with a fallback: a fallback would paper over
#: exactly the rename it is supposed to catch.
CHUNK_TYPENAME = chunk_names.CHUNK_PRIM_TYPENAME

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
    def param_name(self) -> str:
        """The name get_param answers to, namespaces included.

        A param inside a page group is `inputs:Base:base_color` in USD and `Base.base_color`
        to the chunk. `name` is the last segment and is right for a label; it is wrong for
        a lookup, which is why both exist.

        Qt is inconsistent about this today -- its connect path uses the last segment while
        its disconnect path uses the dotted form -- so one of the two is wrong for a
        grouped param. Preserved rather than silently changed here: unifying the two call
        sites is the job, and altering what either one resolves to is a separate decision.
        """
        suffix = self.attr_path.split(".", 1)[-1]
        for prefix in ("inputs:", "outputs:"):
            if suffix.startswith(prefix):
                return suffix[len(prefix):].replace(":", ".")
        return suffix.replace(":", ".")

    @property
    def is_exec(self) -> bool:
        suffix = self.attr_path.split(".", 1)[-1]
        return chunk_names.is_exec_input_name(suffix) or suffix == EXEC_OUTPUT


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
            if prim.GetTypeName() == CHUNK_TYPENAME]


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
        if name != EXEC_INPUT and chunk_names.is_exec_input_name(name):
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
        if name in (EXEC_INPUT, EXEC_OUTPUT) or chunk_names.is_exec_input_name(name):
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
            is_exec = chunk_names.is_exec_input_name(name)
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

    The types must match exactly, one end must be an input and the other an output, and
    they cannot belong to the same node. Exact rather than convertible: an int into a float
    port looks harmless and the param system would coerce it, but the Qt view has always
    refused it, and an engine that is more permissive than the frontend it was extracted
    from would let a second frontend author graphs the first one considers invalid.
    """
    if a.port_type != b.port_type:
        return False
    if a.is_input == b.is_input:
        return False
    return a.node_path != b.node_path


# ---------------------------------------------------------------------------
# Mutating operations
#
# These author USD and return a GraphChange saying what moved. They deliberately do NOT
# redraw, recook or touch a view: that is the caller's business, and keeping it out is what
# lets the same op serve a Qt scene and a Flet canvas.


@dataclass
class GraphChange:
    """What a mutation touched, so a view can react without re-deriving it.

    `touched_nodes` holds prim paths whose chunk may need recooking -- the caller maps them
    back to chunk objects, because which of them matter depends on the active view.
    """

    edges_dirty: bool = True
    touched_nodes: set[str] = None
    exec_changed: bool = False
    #: Set when the wire was authored but its expression could not be written -- the
    #: connection stands, the value will not resolve through it until the text is fixed.
    expression_error: str = ""

    def __post_init__(self):
        if self.touched_nodes is None:
            self.touched_nodes = set()


def _clear_connections(stage: Usd.Stage, attr: Usd.Attribute) -> None:
    """Clear a connection wherever an opinion for it exists, not just in the edit layer.

    A connection may have been authored under a different Edit Target, or before any Edit
    Target chunk ran; clearing only the current layer silently no-ops and leaves the
    original opinion composed in as a stale, seemingly undeletable wire.

    Delegates to ChunkStageSync when a session is live, because that path also keeps the
    session's own bookkeeping in step. Without one it does the work itself -- ChunkStageSync
    returns silently when uninitialised, so a frontend that reads a stage rather than
    joining a session got a disconnect that reported success and changed nothing.
    """
    # Imported here, not at module scope: ChunkStageSync is the one thing in this file
    # that belongs to a live session, and importing chunk_usd for it would put five
    # thousand lines and two hundred and fifty modules behind every use of the engine --
    # including the offline ones that have no session and want none.
    from usd_chunks.plugins import chunk_usd

    sync = chunk_usd.ChunkStageSync()
    if getattr(sync, "initialized", False) and getattr(sync, "_stage", None) is stage:
        sync.clear_attr_connections(attr)
        return
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        spec = layer.GetAttributeAtPath(attr.GetPath())
        if spec and spec.connectionPathList.explicitItems:
            with Usd.EditContext(stage, layer):
                attr.ClearConnections()


def connect(stage: Usd.Stage, a: PortRef, b: PortRef, chunks: dict | None = None) -> GraphChange | None:
    """Wire two ports. Returns None if the pair is not connectable.

    Order does not matter: whichever end is the input becomes the destination, since a USD
    connection is owned by the consumer.
    """
    if not can_connect(a, b):
        return None
    chunks = chunks or {}
    src, dst = (b, a) if a.is_input else (a, b)

    dst_attr = stage.GetAttributeAtPath(Sdf.Path(dst.attr_path))
    if not (dst_attr and dst_attr.IsValid()):
        # An unconnected node has no inputs:chunk attribute yet -- it is created on first
        # wiring rather than up front, so it has to be created here.
        prim = stage.GetPrimAtPath(Sdf.Path(dst.attr_path).GetPrimPath())
        if not prim.IsValid():
            return None
        sdf_type = Sdf.ValueTypeNames.Token if dst.port_type == PORT_EXEC else Sdf.ValueTypeNames.String
        dst_attr = prim.CreateAttribute(dst.attr_path.rsplit(".", 1)[-1], sdf_type, custom=False)
        if not dst_attr.IsValid():
            return None

    # An exec connection references outputs:chunk; a typed param connection references the
    # source's INPUTS:param, not its outputs -- the output dot is a drawing convention, the
    # connection names where the value actually lives.
    if src.port_type == PORT_EXEC:
        src_conn_path = Sdf.Path(src.attr_path)
    else:
        src_conn_path = Sdf.Path(src.attr_path.replace(".outputs:", ".inputs:", 1))

    # Bookkeeping follows the Edit Target: update in place where a spec already exists,
    # otherwise resolve where a new one belongs, rather than trusting the ambient target.
    home = chunk_utils.get_attr_home_layer(dst_attr)
    target = home or chunk_utils.get_bookkeeping_layer(stage, chunks.get(dst.node_path))
    with Usd.EditContext(stage, target):
        dst_attr.SetConnections([src_conn_path])

    # A typed wire also writes the expression that makes the value resolve at runtime. The
    # wiring and the expression are separate concerns -- clearing the text keeps the wire --
    # and they can fail separately too: a param that validates its value rejects the
    # expression text outright (a prim-path LineEdit checks the path exists, and
    # "@Node.param@" is not a path). The connection is already authored by this point, so
    # raising would leave the wire in place and the caller holding an exception. Report it
    # instead and let the view say so.
    expression_error = ""
    if src.port_type != PORT_EXEC:
        dst_chunk = chunks.get(dst.node_path)
        if dst_chunk is not None:
            param = dst_chunk.get_param(dst.name)
            if param is not None:
                src_node_name = Sdf.Path(src.attr_path).GetPrimPath().name
                try:
                    param.value = f"@{src_node_name}.{src.name}@"
                except Exception as exc:  # noqa: BLE001 -- the wire stands either way
                    expression_error = f"{type(exc).__name__}: {exc}"

    return GraphChange(touched_nodes={dst.node_path},
                       exec_changed=src.port_type == PORT_EXEC,
                       expression_error=expression_error)


def disconnect(stage: Usd.Stage, port: PortRef, prims: dict, chunks: dict | None = None) -> GraphChange:
    """Clear a port's connections, healing the exec chain across the gap.

    Detaching a node from the middle of an exec chain rewires its successor to its
    predecessor, so removing one node does not sever everything downstream of it.
    """
    chunks = chunks or {}
    touched = {port.node_path}
    attr = stage.GetAttributeAtPath(Sdf.Path(port.attr_path))
    if not (attr and attr.IsValid()):
        return GraphChange(edges_dirty=False, touched_nodes=set())

    if port.port_type == PORT_EXEC:
        out_attr_path = f"{port.node_path}.{EXEC_OUTPUT}"
        for path, prim in prims.items():
            # Every exec slot, not only inputs:chunk -- a merge fed through slot 2 is just
            # as much a successor, and skipping it would leave that slot pointing at a node
            # the user just detached.
            for succ_attr in chunk_names.iter_exec_inputs(prim):
                if not any(str(c) == out_attr_path for c in succ_attr.GetConnections()):
                    continue
                predecessors = attr.GetConnections()
                if predecessors:
                    succ_attr.SetConnections(list(predecessors))
                else:
                    succ_attr.ClearConnections()
                touched.add(path)
                break

    _clear_connections(stage, attr)

    if port.port_type != PORT_EXEC and port.is_input:
        chunk = chunks.get(port.node_path)
        if chunk is not None:
            param = chunk.get_param(port.param_name)
            if param is not None:
                param.value = param.default_value

    return GraphChange(touched_nodes=touched, exec_changed=port.port_type == PORT_EXEC)
