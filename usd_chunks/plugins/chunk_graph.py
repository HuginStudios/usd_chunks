"""The node graph as data: nodes, ports and edges, with no toolkit attached.

It depends on `chunk_naming` for the chunk vocabulary and reaches for `chunk_stage` only
inside the one function that needs a live session, so the reading and typing half runs with
no stage machinery loaded at all.

`chunk_qt_nodegraph` grew the graph *model* and the Qt *view* together, but only
superficially -- the connection logic reads three fields off a port item, and the port
typing is pure functions over `Sdf.ValueTypeNames` and the param classes. Everything this
module needs (`chunk_stage`, `chunk_core`, `chunk`, `chunk_notice`, `chunk_registry`,
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

import logging
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pxr import Gf, Sdf, Usd

from usd_chunks import chunk_core as core
from usd_chunks import chunk_utils
from usd_chunks.chunk import Checkbox, ColorPicker, FileBrowser, LineEdit, Menu, Slider, Spinbox, TextEdit
from usd_chunks import chunk_naming, chunk_registry, chunk_scene
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_graph_layout
from usd_chunks import chunk_stage

logger = logging.getLogger(__name__)

#: Port types. Tokens rather than an enum because they are already persisted as strings in
#: the Qt view's colour table and read by name in ~40 places.
PORT_EXEC = "exec"
PORT_FLOAT = "float"
PORT_INT = "int"
PORT_BOOL = "bool"
PORT_STRING = "string"
PORT_COLOR = "color"
PORT_VECTOR = "vector"

INPUTS_PREFIX = chunk_naming.INPUTS_PREFIX
EXEC_INPUT = chunk_naming.EXEC_INPUT
EXEC_OUTPUT = chunk_naming.EXEC_OUTPUT
CHUNKS_ROOT = chunk_naming.CHUNKS_ROOT

#: The prim type a chunk is, and the root scope, both taken from chunk_stage rather than
#: spelled out again -- renaming either there should not leave the graph quietly finding
#: nothing. Bound directly, not via getattr with a fallback: a fallback would paper over
#: exactly the rename it is supposed to catch.
CHUNK_TYPENAME = chunk_naming.CHUNK_PRIM_TYPENAME
CHUNK_TYPE_ATTR = chunk_naming.CHUNK_TYPE_ATTR

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
        return chunk_naming.is_exec_input_name(suffix) or suffix == EXEC_OUTPUT


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
        if name != EXEC_INPUT and chunk_naming.is_exec_input_name(name):
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
        if name in (EXEC_INPUT, EXEC_OUTPUT) or chunk_naming.is_exec_input_name(name):
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
            is_exec = chunk_naming.is_exec_input_name(name)
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


def graph_position(prim: "Usd.Prim | None") -> tuple[float, float]:
    """A prim's node position in CANVAS space: USD is Y-up, a canvas is Y-down.

    node_position returns what USD stores; this returns what a graph draws. Both frontends
    flip, so the flip is defined once here rather than twice, differently -- which is
    exactly what had happened (Qt flipped, Flet did not, and the same file rendered
    mirrored between them).
    """
    if prim is None or not prim.IsValid():
        return (0.0, 0.0)
    x, y = node_position(prim)
    return (x, -y)


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


def contexts(stage: Usd.Stage) -> "list[str]":
    """Every prim path holding Chunk prims, outermost first.

    A chunkgroup is both a node in its parent's graph and a graph of its own, so a file has
    several of these and they nest.

    Seeded with CHUNKS_ROOT plus any Chunk prim sitting directly at the stage's own root: a
    tab context's prim_path can be authored anywhere, and one set to a root-level path (say
    "/Tab1") puts every chunk under it with NO /chunks intermediate at all -- CHUNKS_ROOT
    alone as the only seed then finds nothing to BFS from, so contexts() (and everything
    built on it, default_context included) never discovers that tab or anything in it.
    """
    root_level = [str(p.GetPath())
                  for p in stage.GetPseudoRoot().GetFilteredChildren(INCLUDE_INACTIVE)
                  if p.GetTypeName() == CHUNK_TYPENAME]
    found, pending = [], [CHUNKS_ROOT] + root_level
    while pending:
        root = pending.pop(0)
        nodes = resolve_graph_nodes(stage, root)
        if not nodes:
            continue
        found.append(root)
        pending += [str(p.GetPath()) for p in nodes]
    return found


def default_context(stage: Usd.Stage) -> str:
    """The innermost context on the outermost chain -- which graph to open.

    A file whose top level holds nothing but one chunkgroup is asking to be opened inside
    it, the same way double-clicking that group in Qt dives into it. Every frontend needs
    this the moment it opens a file it did not author: `add_chunk` routes a context-less
    chunk into the scene's default chunkgroup, so a network read back at CHUNKS_ROOT shows
    one group and none of the work.
    """
    available = contexts(stage)
    context = available[0] if available else CHUNKS_ROOT
    while True:
        nodes = resolve_graph_nodes(stage, context)
        if len(nodes) == 1 and str(nodes[0].GetPath()) in available:
            context = str(nodes[0].GetPath())
            continue
        return context


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
    # that belongs to a live session, and importing chunk_stage for it would put five
    # thousand lines and two hundred and fifty modules behind every use of the engine --
    # including the offline ones that have no session and want none.
    from usd_chunks import chunk_stage

    sync = chunk_stage.ChunkStageSync()
    if getattr(sync, "initialized", False) and getattr(sync, "_stage", None) is stage:
        sync.clear_attr_connections(attr)
        return
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        spec = layer.GetAttributeAtPath(attr.GetPath())
        if spec and spec.connectionPathList.explicitItems:
            with Usd.EditContext(stage, layer):
                attr.ClearConnections()


def set_param_visibly(chunk_obj, param, value) -> None:
    """Set a param's value so its widget hears about it, even on a blocked chunk.

    A chunk created by dropping a wire into empty space is blocked the moment it is made
    (view-driven execution: nothing is wired to it yet), and a blocked chunk suppresses its
    own param notices -- so a deferred connect could author the expression, have it resolve
    correctly, and still leave the field on the node showing nothing at all.

    Unblocking for the write is what makes the widget update, and `_executing` is what keeps
    that from running the module: set_blocked suppresses per-param VALUE_CHANGED notices as
    well as execution, while _executing suppresses only chunk_notice's reserved
    module.execute() dispatch.
    """
    # getattr, not a bare call: in Qt this only ever saw a real ChunkBase, but connect()
    # is handed whatever the caller's chunk dict holds. An object that cannot be blocked
    # does not need any of the dance below.
    is_blocked = getattr(chunk_obj, "is_blocked", None)
    if not callable(is_blocked) or not is_blocked():
        param.value = value
        return
    prev_executing = getattr(chunk_obj, "_executing", False)
    chunk_obj._executing = True
    chunk_obj.set_blocked(False)
    try:
        param.value = value
    finally:
        chunk_obj.set_blocked(True)
        chunk_obj._executing = prev_executing


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
                    set_param_visibly(dst_chunk, param, f"@{src_node_name}.{src.name}@")
                except Exception as exc:  # noqa: BLE001 -- the wire stands either way
                    expression_error = f"{type(exc).__name__}: {exc}"

    return GraphChange(touched_nodes={dst.node_path},
                       exec_changed=src.port_type == PORT_EXEC,
                       expression_error=expression_error)


def disconnect(stage: Usd.Stage, port: PortRef, prims: dict, chunks: dict | None = None,
               heal: bool = True) -> GraphChange:
    """Clear a port's connections, healing the exec chain across the gap.

    Detaching a node from the middle of an exec chain rewires its successor to its
    predecessor, so removing one node does not sever everything downstream of it.

    heal=False cuts just the one wire. That is what dragging a wire off an input means:
    healing there took A -> B -> View, rewired View to A and left B dangling, so the wire
    the user pulled off looked like it had jumped onto the next node down the chain.
    """
    chunks = chunks or {}
    touched = {port.node_path}
    attr = stage.GetAttributeAtPath(Sdf.Path(port.attr_path))
    if not (attr and attr.IsValid()):
        return GraphChange(edges_dirty=False, touched_nodes=set())

    if port.port_type == PORT_EXEC and heal:
        out_attr_path = f"{port.node_path}.{EXEC_OUTPUT}"
        for path, prim in prims.items():
            # Every exec slot, not only inputs:chunk -- a merge fed through slot 2 is just
            # as much a successor, and skipping it would leave that slot pointing at a node
            # the user just detached.
            for succ_attr in chunk_naming.iter_exec_inputs(prim):
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


def iter_copyable_params(chunk: core.ChunkCommon):
    """`iter_connectable_params`, minus anything flagged State.HIDDEN.

    A HIDDEN param is a chunk module's own internal bookkeeping, never meant for a user to
    see or set directly -- e.g. scripts/usd/create_layer.py's _new_layer_id/_layer_name_used/
    _parent_layer_id, which cache identifiers for the anon Sdf.Layer *that specific chunk
    instance* created. Copying those verbatim onto a pasted chunk hands it the ORIGINAL
    instance's cached identifier; since that layer is still alive (Sdf.Layer.Find succeeds
    process-wide, not scoped to this chunk), the pasted chunk's own next execute() silently
    adopts and reuses the original's real layer object instead of creating an independent one
    of its own -- the two nodes end up secretly sharing state. Not scoped to
    scripts/usd/create_layer.py specifically -- any chunk module using a HIDDEN param the same way
    has the identical hazard, so the exclusion lives at this shared, module-agnostic level
    rather than as a create_layer-specific special case.

    Deliberately a separate generator from `iter_connectable_params` itself (used elsewhere
    for port placement) rather than changing what that one yields -- copy/paste is the only
    caller that needs this narrower set."""
    for param in iter_connectable_params(chunk):
        if param.has_state(ChunkFlags.State.HIDDEN):
            continue
        yield param


def iter_chunk_param_paths(chunk: core.ChunkCommon, prefix: str = ""):
    """(dotted path, param) for everything under `chunk`, groups included."""
    for param in chunk.get_my_params():
        path = f"{prefix}{param.name}"
        yield path, param
        if isinstance(param, core.ChunkGroupBase):
            yield from iter_chunk_param_paths(param, f"{path}.")


# ---------------------------------------------------------------------------
# Clipboard
#
# The format a copied selection travels in, and the snapshot/restore logic around it.
# Moved here from chunk_qt_nodegraph, which is where it grew: none of it was ever Qt --
# the only toolkit reference was a QPointF holding a relative position, now a plain
# tuple. Both frontends share the model AND the module-level clipboard below, so a
# selection copied in one can be pasted in the other.
#
# What deliberately did NOT move is materialization. Qt defers wiring pasted edges
# through a QTimer because its proxy widgets are built in batches and the new nodes'
# ports do not exist yet; Flet builds its bodies synchronously and needs no such dance.
# The shape of a paste is toolkit business, the shape of a CLIPBOARD is not.
# ---------------------------------------------------------------------------

class ClipboardNode:
    __slots__ = ("module_name", "values", "rel_pos", "children", "child_edges", "list_sizes",
                 "lofts", "name")

    def __init__(
        self, module_name: str, values: list, rel_pos: tuple[float, float],
        children: "list[ClipboardNode] | None" = None,
        child_edges: "list[ClipboardEdge] | None" = None,
        list_sizes: "list[tuple[str, int]] | None" = None,
        lofts: "list[tuple[int, str]] | None" = None,
        name: str = "",
    ):
        self.module_name = module_name
        # The name the chunk had. A paste keeps it (made unique by add_chunk if it is still
        # taken), so moving nodes into a group does not turn "Materials" back into "Python".
        self.name = name
        self.values = values
        # How many rows each of the chunk's list groups held. Values alone are not enough:
        # a freshly created chunk starts with zero rows, so there is nothing for the
        # positional zip to pair them against and every row is silently dropped.
        self.list_sizes = list_sizes or []
        self.rel_pos = rel_pos  # position relative to the copied selection's top-left corner
        # Only non-empty for a ChunkGroup: its nested sub-chunks and the wiring between them,
        # so copying/duplicating/cutting a group recursively takes its whole contents along
        # instead of pasting an empty group. rel_pos for a child is relative to the *group's
        # own children's* top-left, not the top-level selection's.
        self.children: list[ClipboardNode] = children or []
        self.child_edges: list[ClipboardEdge] = child_edges or []
        # Params this group publishes on its own interface, as (child index, the inner
        # attribute lofted). A loft is not a wire between two children, so it is not a
        # ClipboardEdge: it points UP from a child to the group itself, and the copy has to
        # re-loft rather than re-connect -- the pasted group has no such param until
        # something lofts one.
        self.lofts: list[tuple[int, str]] = lofts or []


class ClipboardEdge:
    """A wire between two copied nodes, referenced by index into the node clipboard rather
    than by prim path (paths don't exist yet at copy time for a not-yet-pasted node, and
    change on every paste anyway). src_attr/dst_attr are ChunkPortItem-suffix form
    ("outputs:chunk"/"inputs:tx", ...), not raw Sdf connection-target form -- see
    `iter_prim_connections`."""

    __slots__ = ("src_index", "src_attr", "dst_index", "dst_attr")

    def __init__(self, src_index: int, src_attr: str, dst_index: int, dst_attr: str):
        self.src_index = src_index
        self.src_attr = src_attr
        self.dst_index = dst_index
        self.dst_attr = dst_attr


def iter_prim_connections(prim: Usd.Prim):
    """Yield (dst_attr_suffix, src_prim_path, src_attr_suffix) for every real inputs:*
    connection on this prim, in ChunkPortItem.attr_path suffix form for BOTH ends (e.g. dst
    "inputs:tx", src "outputs:tx") -- independent of the "Show param connections" view toggle
    (unlike ChunkGraphScene._build_edges, which only builds what's currently drawn), since
    copy/paste needs every real connection, not just the visible ones. Takes a bare Usd.Prim
    rather than a ChunkNodeItem so it also works for a ChunkGroup's children, which have no
    live Qt node at all unless someone has dived into that group."""
    if not prim.IsValid():
        return
    for attr in prim.GetAttributes():
        attr_name = attr.GetName()
        if not attr_name.startswith("inputs:"):
            continue
        for conn_path in attr.GetConnections():
            src_prim_path = str(conn_path.GetPrimPath())
            if chunk_stage.is_exec_input_name(attr_name):
                # Exec connections point straight at the source's outputs:chunk already.
                src_attr_suffix = conn_path.name
            else:
                # Typed connections normally point at the source's OWN inputs:param (see
                # ChunkGraphView._author_connection), but an expression referencing an
                # OUTPUT-flagged param authors the target as outputs:param directly (see
                # ChunkBase.data_changed's use_out branch in chunk_core). Normalize BOTH to
                # the ChunkPortItem-suffix "outputs:param" form used on both ends elsewhere
                # -- strip whichever namespace prefix is actually present rather than
                # assuming inputs: (blindly slicing len("inputs:") off an already-outputs:
                # target left a stray leading colon -> "outputs::param" -> a malformed
                # Sdf.Path that resolved to <>, crashing SetConnections on paste).
                bare = conn_path.name.split(":", 1)[-1]
                src_attr_suffix = "outputs:" + bare
            yield attr_name, src_prim_path, src_attr_suffix


def iter_group_children(chunk: "core.ChunkBase", scene_sender: core.SceneSender, stage: Usd.Stage):
    """Yield the direct children of a ChunkGroup -- both plain leaf chunks (found via
    get_context(), which walks the real Python ownership chain) and nested sub-groups (which
    get_context() can NEVER find this way: it returns self for any CONTEXT-flagged chunk --
    see ChunkGraphScene._rebuild_nodes's own "c is not context" guard for the same quirk --
    and a sub-group is always added as a top-level scene entry, never actually owned by its
    structural parent). Mirrors _rebuild_nodes's own combined lookup (get_context() equality
    plus a get_context_for_prim_path() scan of the group's own USD children) exactly, so a
    ChunkGroup nested inside another ChunkGroup is still found. View chunks are excluded --
    not content worth duplicating, matching the top-level selection's own exclusion."""
    by_path: dict[str, "core.ChunkBase"] = {
        chunk_stage.get_chunk_prim_path(c): c
        for c in scene_sender.get_chunks()
        if c.get_context() == chunk and c is not chunk and not chunk_stage.is_view_chunk(c)
    }
    root_prim = stage.GetPrimAtPath(Sdf.Path(chunk.prim_path)) if chunk.prim_path else None
    if root_prim is not None and root_prim.IsValid():
        for prim in root_prim.GetFilteredChildren(INCLUDE_INACTIVE):
            path = str(prim.GetPath())
            if path in by_path:
                continue
            ctx_chunk = scene_sender.get_context_for_prim_path(path)
            if ctx_chunk is not None and not chunk_stage.is_view_chunk(ctx_chunk):
                by_path[path] = ctx_chunk
    return list(by_path.values())


def snapshot_chunk_node(
    chunk: "core.ChunkBase", scene_sender: core.SceneSender, stage: Usd.Stage, rel_pos: tuple[float, float]
) -> "ClipboardNode | None":
    """Build a ClipboardNode for `chunk` at rel_pos, recursing into its own children (and
    their internal wiring) if it's a ChunkGroup -- shared by the top-level selection snapshot
    and by each recursive level of group contents. Returns None for a chunk with no module
    (nothing to recreate it from -- e.g. a bare exec 'dot')."""
    cfg = chunk.get_config()
    module_name = cfg.module if cfg else None
    if not module_name:
        return None
    clip_node = ClipboardNode(
        module_name, snapshot_param_values(chunk), rel_pos,
        list_sizes=snapshot_list_sizes(chunk), name=chunk.name,
    )
    # Children only for a chunkgroup. A chunk that builds its own children (AMD MaterialX
    # Library's Reference and BindMaterial) makes them again when the copy cooks, so
    # copying them as well pasted every one twice -- and the chunk, finding its own by
    # name, drove one set while the other sat there fighting it.
    if not chunk_stage.is_chunkgroup(chunk):
        return clip_node

    children = iter_group_children(chunk, scene_sender, stage)
    child_prims: dict[str, Usd.Prim] = {}
    child_pos: dict[str, tuple[float, float]] = {}
    for child in children:
        path = chunk_stage.get_chunk_prim_path(child)
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        child_prims[path] = prim
        child_pos[path] = graph_position(prim)
    if child_pos:
        min_x = min(p[0] for p in child_pos.values())
        min_y = min(p[1] for p in child_pos.values())
    else:
        min_x = min_y = 0.0

    kept_index: dict[str, int] = {}
    for child in children:
        path = chunk_stage.get_chunk_prim_path(child)
        pos = child_pos[path]
        child_clip = snapshot_chunk_node(
            child, scene_sender, stage, (pos[0] - min_x, pos[1] - min_y)
        )
        if child_clip is None:
            continue
        kept_index[path] = len(clip_node.children)
        clip_node.children.append(child_clip)

    for child in children:
        path = chunk_stage.get_chunk_prim_path(child)
        dst_index = kept_index.get(path)
        prim = child_prims.get(path)
        if dst_index is None or prim is None:
            continue
        for dst_attr, src_prim_path, src_attr in iter_prim_connections(prim):
            if src_prim_path == chunk.prim_path:
                # A LOFT, not a wire to a sibling: the inner attr points up at the group's
                # own interface. Dropping it as "outside the copied children" is what left a
                # duplicated group with none of the params it published.
                clip_node.lofts.append((dst_index, dst_attr))
                continue
            src_index = kept_index.get(src_prim_path)
            if src_index is None:
                continue  # source outside this group's copied children -- wire dropped
            clip_node.child_edges.append(ClipboardEdge(src_index, src_attr, dst_index, dst_attr))

    return clip_node


def snapshot_list_sizes(chunk: core.ChunkCommon) -> "list[tuple[str, int]]":
    """How many rows each list group on `chunk` holds, keyed by param path.

    A list group's rows are created at runtime, so a pasted chunk has none of them and the
    positional value snapshot has nothing to land on -- copying a Sublayer with a path set
    produced one with an empty list and no path, and the same for every other chunk built
    on a list group.
    """
    sizes: "list[tuple[str, int]]" = []
    for path, param in iter_chunk_param_paths(chunk):
        if isinstance(param, core.ChunkListGroupBase):
            sizes.append((path, param.size))
    return sizes


def apply_list_sizes(chunk: core.ChunkCommon, sizes: "list[tuple[str, int]]") -> None:
    """Grow each list group back before any value is applied, so the rows exist to take them."""
    for path, size in sizes:
        param = chunk.get_param(path)
        if isinstance(param, core.ChunkListGroupBase) and param.size != size:
            try:
                param.size = size
            except Exception:
                # A list whose size cannot be set (driven by something else, or a module
                # that has changed shape since the copy) should not abort the paste.
                continue


def apply_name(chunk: core.ChunkCommon, item: "ClipboardNode") -> None:
    """Give a pasted chunk the name it was copied with, before it is added to a scene.

    Before, not after: add_chunk is what makes a taken name unique, and renaming an added
    chunk is a rename of its prim.
    """
    name = getattr(item, "name", "")
    if name and chunk.name != name:
        chunk.name = name


def snapshot_param_values(chunk: core.ChunkCommon) -> list:
    """Positional snapshot of every copyable param's value, in `iter_copyable_params`
    order. Two instances of the same chunk module build their param tree in the same code
    order, so a plain positional zip (see `apply_param_values`) is enough to pair them back
    up -- no param-path bookkeeping needed."""
    return [p.value for p in iter_copyable_params(chunk)]


def apply_param_values(chunk: core.ChunkCommon, values: list) -> None:
    for param, value in zip(iter_copyable_params(chunk), values):
        try:
            param.value = value
        except Exception:
            # A param whose value can't be re-applied as-is (e.g. a stale expression string
            # for a wire that wasn't copied along with it) shouldn't abort the rest of the
            # paste -- leave it at its default and move on.
            continue


#: The clipboard itself. Module level, and shared: a selection copied in the Qt graph can
#: be pasted in the Flet one, because both now speak the same ClipboardNode/ClipboardEdge.
node_clipboard: list = []
edge_clipboard: list = []


# ---------------------------------------------------------------------------
# The frontend contract
# ---------------------------------------------------------------------------


@runtime_checkable
class GraphFrontend(Protocol):
    """What a graph frontend has to be able to do, whatever it draws with.

    This exists because the second frontend silently lacked half of the first one, and
    nothing said so. Qt and Flet had eleven operations with the same meaning under eleven
    different names -- `_delete_selected` and `delete_selected`,
    `_copy_selected_to_clipboard` and `copy_selection` -- and exactly two names in common.
    A third frontend (QML, ImGui, a web client) had nothing to read but the Qt file, which
    is 5000 lines and mostly Qt.

    **Scope.** These are the verbs. Drawing and input are deliberately absent: they have no
    shared shape. Flet computes `shapes()` and positions bodies in a Stack; Qt builds
    QGraphicsItems that place themselves; QML would bind a model to delegates. Requiring
    any one of those shapes would smuggle a toolkit's design into the contract. So the rule
    is: if it decides *what happens to the graph* it belongs here, and if it decides *what
    appears on screen* it does not.

    **Why a Protocol and not an ABC.** ABCMeta cannot be mixed into a PySide6 class -- the
    metaclasses conflict outright. The usual workaround (a metaclass deriving from both)
    does construct, but Shiboken's __call__ skips the abstract-method check, so an
    incomplete Qt frontend instantiates happily. A contract that appears to be enforced but
    is not is worse than none, so enforcement lives in a conformance test instead
    (tests/test_graph_frontend_contract.py), which checks names AND signatures.

    **Names.** Taken from the Flet implementation, because Qt's equivalents are private and
    inconsistently phrased, not because Flet is the reference. A Qt adapter conforms by
    delegating.

    Implementations are also expected to hold `context` (the prim path of the graph being
    shown) and `selected_nodes` (a set of prim paths), but a Protocol cannot usefully
    require plain attributes on classes that may compute them, so they are documented here
    rather than declared.
    """

    # -- selection ---------------------------------------------------------

    def set_selection(self, paths, add: bool = False) -> None:
        """Select these node paths, replacing the selection unless `add`.

        Must mirror onto each chunk's ChunkFlags.State.SELECTED bit: the shared operations
        read the bit, not a frontend attribute, so a selection kept privately is invisible
        to them.
        """

    def selected_chunks(self) -> list:
        """The selected chunks, as chunk objects."""

    # -- editing -----------------------------------------------------------

    def delete_selected(self) -> None:
        """Delete the selected chunks, healing the exec chain closed behind them."""

    def toggle_disable_selected(self) -> None:
        """Flip DISABLED on the selection. A disabled chunk stays visible, greyed."""

    def collapse_selected_or_all(self) -> None:
        """Collapse the selection, or the whole graph when nothing is selected.

        One direction for the whole set: anything still expanded collapses, and only a set
        that is already entirely collapsed expands again. Chunkgroups are skipped -- a
        collapsed one hides the control that expands it -- and so are views, which have no
        body to collapse.
        """

    def copy_selection(self) -> None:
        """Snapshot the selection into chunk_graph.node_clipboard/edge_clipboard.

        The clipboard is shared between frontends, so a copy here must be pasteable
        anywhere: put ClipboardNode/ClipboardEdge in those two module-level lists rather
        than inventing a private format.
        """

    def cut_selection(self) -> None:
        """Copy, then delete."""

    def paste_at(self, x: float, y: float, nodes=None, edges=None) -> None:
        """Materialize the clipboard at a layout point, wire it, and select the result.

        Wires with both ends in the clipboard are recreated between the new nodes; a wire
        with only one end copied is dropped rather than reattached to the original.
        """

    def duplicate_selection(self) -> None:
        """Copy and paste in one step at an offset, WITHOUT touching the clipboard."""

    def group_selection(self) -> None:
        """Put the selection in a new chunkgroup, which takes its place in the exec chain.

        The boundary must be read before anything is removed: deleting the selection heals
        the chain closed, after which nothing records what used to attach where.
        """

    def create_chunk(self, entry, x: float, y: float) -> None:
        """Create one ChunkRegistry MenuEntry's chunk(s) at a layout point.

        Takes an entry rather than a module name so variants work, and takes no menu: how
        the user picks the entry is the frontend's business.
        """

    # -- navigation --------------------------------------------------------

    def can_dive(self, path: str) -> bool:
        """Whether `path` is a subnet the user may navigate into.

        Must key on chunk_stage.is_chunkgroup, not on the CONTEXT flag: a chunk-in-chunk
        carries CONTEXT too and is an implementation detail, not a subnet.
        """

    def dive_into(self, path: str) -> None:
        """Show the inside of that chunkgroup as the graph.

        Whatever this does, creating a chunk afterwards must land INSIDE the group --
        add_chunk parents to the scene's current context, which navigating does not set by
        itself.
        """

    def dive_out(self) -> None:
        """Back up to the enclosing graph. A no-op at the top."""

    # -- views -------------------------------------------------------------

    def active_slot(self) -> int:
        """Which view slot drives execution here; 0 for none."""

    def set_active_slot(self, slot: int) -> None:
        """Switch the driving view and recook.

        Must go through chunk_stage.recook_for_view -- a highlight is not a view switch --
        and must persist to chunk:activeView on the context prim, clearing rather than
        writing a 0, so the choice survives a save and is seen by other frontends.
        """

    # -- viewport ----------------------------------------------------------

    def frame(self, paths) -> None:
        """Fit those nodes in the viewport."""


#: The verbs a conformance test checks. Kept as data so a test can report exactly what a
#: new frontend is missing instead of failing on the first absent name.
GRAPH_FRONTEND_METHODS = (
    "set_selection", "selected_chunks",
    "delete_selected", "toggle_disable_selected",
    "copy_selection", "cut_selection", "paste_at", "duplicate_selection",
    "group_selection", "create_chunk",
    "can_dive", "dive_into", "dive_out",
    "active_slot", "set_active_slot",
    "frame",
)


# ---------------------------------------------------------------------------
# Operations
#
# The toolkit-free half of what a frontend's edit commands do. Both frontends had
# their own copy of each of these, identical line for line, because they grew in Qt
# and were rewritten from scratch for Flet.
#
# Materialization is NOT here, and that is deliberate rather than unfinished. Qt
# defers wiring a paste through a QTimer because its ports arrive in deferred proxy
# batches, guards a window with chunk._executing, and blocks pasted chunks when the
# context has an active view; Flet builds synchronously and needs none of it. The
# steps that differ are the whole of the difference, so a shared materialize would be
# a function with a Qt branch and a Flet branch inside it.
# ---------------------------------------------------------------------------


def exec_boundary(stage: Usd.Stage, inside: "set[str]") -> "tuple[list[str], list[tuple[str, str]]]":
    """(feeds, consumers) -- the exec wires crossing into and out of `inside`.

    A feed is a source prim path. A consumer is (prim path, exec input name): the slot
    matters, because a merge fed by the selection through inputs:chunk1 must get its
    replacement in inputs:chunk1, not in inputs:chunk where another branch already is.

    Read this BEFORE removing anything. Deleting a selection heals the exec chain closed
    as it goes, so afterwards nothing records what used to attach where, and a group put
    in its place has nothing to reconnect to.

    Order is preserved and duplicates dropped: the first wire found wins, so re-wiring is
    deterministic rather than set-ordered.
    """
    feeds: list[str] = []
    consumers: list[tuple[str, str]] = []
    for prim in stage.TraverseAll():
        if prim.GetTypeName() != CHUNK_TYPENAME:
            continue
        path = str(prim.GetPath())
        for attr in chunk_stage.iter_exec_inputs(prim):
            for conn in attr.GetConnections():
                source = str(conn.GetPrimPath())
                if path in inside and source not in inside and source not in feeds:
                    feeds.append(source)
                elif source in inside and path not in inside:
                    slot = (path, attr.GetName())
                    if slot not in consumers:
                        consumers.append(slot)
    return feeds, consumers


def attach_group(stage: Usd.Stage, scene, group_path: str, feeds: "list[str]",
                 consumers: "list[tuple[str, str]]") -> None:
    """Wire a new group in where the selection it replaced used to be.

    Every feed goes onto the group's one exec input, stacked rather than each replacing the
    last. Every consumer gets the group in the same slot the selection fed.
    """
    for source in feeds:
        chunk_stage.connect_exec(stage, scene, source, group_path, append=True)
    for consumer, input_name in consumers:
        chunk_stage.connect_exec(stage, scene, group_path, consumer, input_name=input_name)


def snapshot_selection(chunks: dict, prims: dict, positions: dict, scene, stage,
                       selected: "set[str]"):
    """(clipboard nodes, clipboard edges) for `selected`.

    Positions come back relative to the selection's top-left, so a paste lands as a shape
    rather than at whatever absolute coordinates the originals had.

    A wire with only one end selected is DROPPED, not recreated against the original
    outside node. Copying half a chain and having the copy silently wire itself into the
    original's upstream is worse than copying half a chain.

    View chunks are excluded: they are per-context slot machinery, not content, and a
    second view in a slot that already has one is meaningless.
    """
    chosen = [p for p in selected if p in prims and p in positions]
    chosen = [p for p in chosen
              if p not in chunks or not chunk_stage.is_view_chunk(chunks[p])]
    if not chosen:
        return [], []

    min_x = min(positions[p][0] for p in chosen)
    min_y = min(positions[p][1] for p in chosen)
    nodes, index = [], {}
    for path in chosen:
        chunk = chunks.get(path)
        if chunk is None:
            continue
        rel = (positions[path][0] - min_x, positions[path][1] - min_y)
        clip = snapshot_chunk_node(chunk, scene, stage, rel)
        if clip is None:
            continue  # no module behind it -- nothing to recreate it from
        index[path] = len(nodes)
        nodes.append(clip)

    edges = []
    for path in chosen:
        dst_index = index.get(path)
        if dst_index is None:
            continue
        for dst_attr, src_path, src_attr in iter_prim_connections(prims[path]):
            src_index = index.get(src_path)
            if src_index is None:
                continue
            edges.append(ClipboardEdge(src_index, src_attr, dst_index, dst_attr))
    return nodes, edges


def delete_chunks(chunks) -> int:
    """Delete these chunks, returning how many were asked for.

    request_delete heals the exec chain closed as it removes each one, so callers that
    need to know what was attached must read exec_boundary first.
    """
    chunks = list(chunks)
    for chunk in chunks:
        chunk.request_delete()
    return len(chunks)


def layout_adjacency(prims: dict) -> "tuple[dict, dict]":
    """(up_of, down_of) keyed by prim path, from EVERY connection on these prims.

    Exec and typed data alike, and independent of whether a frontend is currently drawing
    param wires: a layout should account for every real connection, not only the ones
    visible. Both maps are scoped to `prims`, which is what chunk_graph_layout requires of
    its callers.
    """
    up_of: dict = {}
    down_of: dict = {}
    for path, prim in prims.items():
        if not prim.IsValid():
            continue
        for attr in prim.GetAttributes():
            if not attr.GetName().startswith(INPUTS_PREFIX):
                continue
            for conn in attr.GetConnections():
                source = str(conn.GetPrimPath())
                if source == path or source not in prims:
                    continue
                up_of.setdefault(path, set()).add(source)
                down_of.setdefault(source, set()).add(path)
    return up_of, down_of


def layout_graph(prims: dict, size_of, current_pos, seeds=None, is_locked=None) -> dict:
    """Auto-layout. Returns {prim path: (x, y)} for the nodes that should move.

    The computation only: growing a selection to its connected subgraph, scoping the
    adjacency to what is being laid out, and running chunk_graph_layout. APPLYING the
    result is the frontend's, because that is where they genuinely differ -- Qt calls
    setPos on an item, Flet writes its positions dict, and ImGui hands the coordinates to
    the node editor.

    All three had their own 44-line copy of everything above that line.

    size_of / current_pos / is_locked are callables taking a prim path, because only a
    frontend knows how big a node ended up. is_locked defaults to "nothing is locked": Qt
    pins nodes whose prim comes from a referenced layer, and the other two have no such
    concept yet.

    seeds narrows the pass to those nodes and everything connected to them -- "layout
    selected". None lays out everything.
    """
    up_of, down_of = layout_adjacency(prims)
    if seeds:
        grown = chunk_graph_layout.grow_to_connected_subgraph(list(seeds), up_of, down_of)
        node_ids = sorted(grown & set(prims))
    else:
        node_ids = sorted(prims)
    if not node_ids:
        return {}

    wanted = set(node_ids)
    scoped_up = {node: {u for u in up_of.get(node, ()) if u in wanted} for node in node_ids}
    scoped_down = {node: {d for d in down_of.get(node, ()) if d in wanted} for node in node_ids}
    return chunk_graph_layout.layout_positions(
        node_ids, size_of, is_locked or (lambda path: False), current_pos,
        scoped_up, scoped_down,
    )


def nest_target(chunk):
    """The context new chunks should be parented to, or None to leave them at the root.

    add_chunk parents to the scene's current context, which navigating into a group does
    not set -- so without this a node created inside a group silently lands outside it.
    Pushing and restoring is SceneSender.creating_in's job; this only answers WHICH, which
    is the half every frontend had written for itself.
    """
    if chunk is not None and chunk_stage.is_group_chunk(chunk):
        return chunk
    return None


def menu_entries() -> list:
    """Every chunk the registry offers, as frontend-agnostic MenuEntry objects.

    Discovery walks the filesystem and imports arbitrary chunk modules, so it can fail for
    environmental reasons; a frontend should show an empty menu rather than die. Narrowed
    from bare Exception so a bug in menu building still surfaces.
    """
    try:
        return chunk_registry.ChunkRegistry().get_menu_entries()
    except (AttributeError, RuntimeError, OSError):
        logger.exception("could not build the chunk menu")
        return []


def materialize(scene, nodes, to_usd) -> dict:
    """Create clipboard nodes through `scene`. Returns {clipboard index: chunk}.

    to_usd(rel_x, rel_y) converts a clipboard entry's relative position into the
    coordinates USD stores -- the one part that genuinely differs, because each frontend
    has its own canvas origin and Y convention.

    Qt does NOT use this. Its paste defers wiring through a QTimer because its ports
    arrive in deferred proxy batches, guards a window with chunk._executing, and blocks
    pasted chunks when the context has an active view. Those steps are the difference, so
    it keeps its own; the two synchronous frontends had the same 20 lines twice.
    """
    registry = chunk_registry.ChunkRegistry()
    made = {}
    for index, item in enumerate(nodes):
        module = registry.get_chunk_module(item.module_name)
        if module is None:
            continue
        chunks = module.create() if module.loaded else module.load()
        if not chunks:
            continue
        primary = chunks[0]
        apply_name(primary, item)
        # Rows before values: a list group starts empty on a fresh chunk, so its values
        # have nowhere to land until the rows it had are back.
        apply_list_sizes(primary, getattr(item, "list_sizes", []))
        apply_param_values(primary, item.values)
        with chunk_stage.creating_at(to_usd(item.rel_pos[0], item.rel_pos[1])):
            for chunk in chunks:
                scene.add_chunk(chunk)
        made[index] = primary
    return made


# ---------------------------------------------------------------------------
# Shared frontend operations, part two
#
# Everything below was written three times over -- once each in Flet, ImGui and Blender --
# and in every case the three copies were the same algorithm with a different coordinate
# convention and a different status string bolted on. Qt keeps its own for the reasons the
# section above records; these are for the synchronous frontends.
#
# The shape each one takes: the engine does the part that touches chunks and USD and hands
# back a plain result, the frontend does the part that touches its canvas. So a function
# here never reloads, never saves and never sets a status -- it returns what the caller
# needs in order to do those things itself.
# ---------------------------------------------------------------------------


def apply_selection(chunks: dict, paths, current=None, add: bool = False) -> "set[str]":
    """Mirror a selection onto the chunks' SELECTED bit. Returns the resulting path set.

    The bit rather than a frontend attribute: the shared operations ask the chunks what is
    selected instead of being handed a list by whichever frontend happened to call them, so
    a selection kept privately would be invisible to them.

    Presentation only -- chunk_scheduler.propagate_presentation -- so selecting cooks
    nothing.
    """
    paths = set(paths)
    if add and current:
        paths |= set(current)
    for path, chunk in chunks.items():
        try:
            if path in paths:
                chunk.set_state(ChunkFlags.State.SELECTED)
            else:
                chunk.unset_state(ChunkFlags.State.SELECTED)
        except AttributeError:
            # A chunk object that does not carry state bits at all. Narrow on purpose: a
            # bare Exception here hid a misspelled state method for a whole frontend's
            # worth of commits, and deselection silently never happened.
            continue
    return paths


def chunks_for(chunks: dict, paths) -> list:
    """The chunk objects behind these paths, in the dict's order."""
    paths = set(paths)
    return [c for p, c in chunks.items() if p in paths]


def copy_to_clipboard(nodes, edges) -> int:
    """Put a snapshot on the shared clipboard. Returns how many nodes were copied.

    The engine's own lists, assigned into rather than rebound, so a selection copied in one
    frontend can be pasted in another -- all four read these same two objects.
    """
    node_clipboard[:] = list(nodes)
    edge_clipboard[:] = list(edges)
    return len(node_clipboard)


def toggle_disabled(chunks) -> int:
    """Flip DISABLED on each of these chunks. Returns how many were touched.

    A disabled chunk stays on the graph, drawn greyed -- hiding it would lose the shape of
    the network the user is reasoning about.
    """
    chunks = list(chunks)
    for chunk in chunks:
        if chunk.has_state(ChunkFlags.State.DISABLED):
            chunk.unset_state(ChunkFlags.State.DISABLED)
        else:
            chunk.set_state(ChunkFlags.State.DISABLED)
    return len(chunks)


def write_position(prim: "Usd.Prim | None", x: float, y: float, owner=None) -> None:
    """Persist a node position, in the coordinates USD stores.

    The counterpart of graph_position, which was here on its own -- so three frontends each
    hand-rolled the CreateAttribute call. The conversion stays with the caller, since a
    canvas knows its own origin and Y sign; the authoring does not.

    Authors through the attr's own existing home layer, or owner's bookkeeping layer for a
    first-time write -- never bare, which relies on the stage's ambient Edit Target. That
    target moves as chunks are created and edited, so a position pulled from the canvas
    right after another node was authored would land in THAT node's layer instead of each
    node's own -- a procedural layer that then gets cleared and regenerated, snapping the
    other nodes back to (0, 0). Mirrors chunk_qt_nodegraph._write_pos_to_usd and the view-node position
    write in ensure_view_slot, both of which already do this.
    """
    if prim is None or not prim.IsValid():
        return
    stage = prim.GetStage()
    attr = prim.GetAttribute(POS_ATTR)
    home = chunk_utils.get_attr_home_layer(attr) if attr else None
    target = home or chunk_utils.get_bookkeeping_layer(stage, owner)
    with Usd.EditContext(stage, target):
        if not attr:
            attr = prim.CreateAttribute(POS_ATTR, Sdf.ValueTypeNames.Float2)
        attr.Set(Gf.Vec2f(float(x), float(y)))


def selection_origin(positions: dict, paths) -> "tuple[float, float] | None":
    """The top-left of these nodes, or None if none of them have a position.

    Where a duplicate should land is this plus an offset, and the offset's *direction* is a
    canvas convention -- Y-down in Qt and Flet, Y-up in Blender -- so it stays with the
    caller.
    """
    chosen = [p for p in paths if p in positions]
    if not chosen:
        return None
    return (min(positions[p][0] for p in chosen),
            min(positions[p][1] for p in chosen))


def rewire_pasted(stage: Usd.Stage, created: dict, edges, chunks: dict | None = None) -> int:
    """Re-create the copied wires among freshly pasted chunks. Returns how many were made.

    Ports are read off the stage rather than out of a frontend's cache, so this works
    before a reload -- which is what let the three copies of it differ: two looked wires up
    in their own port dict and had to rebuild it first, and the third built PortRefs by
    hand and hardcoded them as exec, quietly dropping every typed param wire.

    The pasted chunks are merged into `chunks` before connecting. `connect` resolves the
    destination's bookkeeping layer and authors its expression through that dict, and the
    destination of a pasted wire is by definition a chunk that did not exist when the
    caller built it.
    """
    merged = dict(chunks or {})
    ports: dict = {}
    for index, chunk in created.items():
        path = chunk_stage.get_chunk_prim_path(chunk)
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not (prim and prim.IsValid()):
            continue
        merged[path] = chunk
        ports[index] = (path, {p.attr_path: p for p in iter_ports(prim, chunk)})

    made = 0
    for edge in edges or ():
        src = ports.get(edge.src_index)
        dst = ports.get(edge.dst_index)
        if src is None or dst is None:
            continue  # one end was not pasted; dropped rather than wired to the original
        a = src[1].get("%s.%s" % (src[0], edge.src_attr))
        b = dst[1].get("%s.%s" % (dst[0], edge.dst_attr))
        if a is not None and b is not None and connect(stage, a, b, merged) is not None:
            made += 1
    return made


def paste_nodes(scene, stage: Usd.Stage, nodes, edges, to_usd, nest=None,
                chunks: dict | None = None) -> dict:
    """Materialize clipboard nodes and re-wire them. Returns {clipboard index: chunk}.

    Qt does NOT use this, for the reasons materialize records. The caller still owns
    saving, refreshing its chunk dict, reloading and selecting what landed.
    """
    if not nodes or scene is None:
        return {}
    with scene.creating_in(nest):
        made = materialize(scene, nodes, to_usd=to_usd)
    rewire_pasted(stage, made, edges, chunks)
    return made


def create_from_entry(scene, entry, usd_pos, nest=None) -> "tuple[list, str]":
    """Create one menu entry's chunk(s) at a USD position. Returns (created, error).

    The sequence every frontend runs: set the position hook chunk_stage reads while a chunk
    is being added, add through the scene so the normal load path authors the prim, then
    clear it. Writing a prim directly would skip the registration the rest of the framework
    relies on.

    A chunk module is arbitrary user code, so this can fail in ways no frontend can
    anticipate -- but not in EVERY way: KeyboardInterrupt and SystemExit must still travel,
    and catching bare Exception was hiding real bugs behind a status line.
    """
    if scene is None:
        return [], "no chunk session; cannot create"
    try:
        with chunk_stage.creating_at(usd_pos, pin=False), scene.creating_in(nest):
            created = entry.create()
            for chunk in created:
                scene.add_chunk(chunk)
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        logger.exception("could not create %s", getattr(entry, "module_name", entry))
        return [], "create failed: %s: %s" % (type(exc).__name__, exc)
    return created, ""


def group_into_chunkgroup(stage: Usd.Stage, scene, chunks: dict, prims: dict,
                          positions: dict, selected, to_usd) -> "tuple[str, int]":
    """Put the selection in a new chunkgroup that takes its place in the exec chain.

    Returns (group prim path, how many nodes moved) -- ("", 0) when there was nothing to
    do. The caller saves, refreshes its chunk dict and reloads.

    The sequence a user would otherwise do by hand: read the boundary, cut, make a group,
    paste inside it, then wire the group in where the selection used to be. The boundary is
    read BEFORE anything is removed, because deleting heals the exec chain closed as it
    goes and afterwards nothing records what used to attach where.

    The chunks pasted inside are wired to each other with connect_exec directly rather than
    through ports: they are inside a group the caller is not showing, so they have no ports
    there to look up.
    """
    chosen = [p for p in selected if p in prims and p in positions]
    if not chosen or scene is None:
        return "", 0

    centre_x = sum(positions[p][0] for p in chosen) / len(chosen)
    centre_y = sum(positions[p][1] for p in chosen) / len(chosen)
    feeds, consumers = exec_boundary(stage, set(chosen))

    nodes, edges = snapshot_selection(chunks, prims, positions, scene, stage, set(chosen))
    if not nodes:
        return "", 0
    delete_chunks(chunks_for(chunks, chosen))

    registry = chunk_registry.ChunkRegistry()
    module = registry.get_chunk_module(chunk_scene.CHUNKGROUP_MODULE)
    made = (module.create() if module.loaded else module.load()) if module else None
    if not made:
        logger.error("no chunkgroup module registered; the selection has been deleted")
        return "", 0
    group = made[0]
    with chunk_stage.creating_at(to_usd(centre_x, centre_y)):
        scene.add_chunk(group)
    group_path = chunk_stage.get_chunk_prim_path(group)

    # Pasting inside the group: pointing the scene's current context at it is what diving
    # in before a paste amounts to.
    with scene.creating_in(group):
        pasted = materialize(
            scene, nodes,
            to_usd=lambda rel_x, rel_y: to_usd(centre_x + rel_x, centre_y + rel_y))

    for edge in edges:
        src, dst = pasted.get(edge.src_index), pasted.get(edge.dst_index)
        if src is None or dst is None:
            continue
        if not (edge.src_attr == EXEC_OUTPUT and chunk_naming.is_exec_input_name(edge.dst_attr)):
            continue  # a typed param wire, not an exec one
        chunk_stage.connect_exec(stage, scene, chunk_stage.get_chunk_prim_path(src),
                                 chunk_stage.get_chunk_prim_path(dst),
                                 input_name=edge.dst_attr, append=True)

    attach_group(stage, scene, group_path, feeds, consumers)
    return group_path, len(nodes)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def can_dive(chunks: dict, path: str) -> bool:
    """Whether `path` is a subnet the user may navigate into.

    Keyed on is_subnet, never on the CONTEXT flag. Both a chunkgroup and a
    chunk-in-chunk (AMDMaterialXLibrary, which creates `reference` and `bind_material` as
    an implementation detail) carry CONTEXT, so the flag cannot tell them apart, and only
    the chunkgroup may be dived into. A chunk
    hosting a referenced network (chunk_stage.is_network_host) is a subnet too.
    """
    chunk = chunks.get(path)
    return chunk is not None and chunk_stage.is_subnet(chunk)


def parent_context(context: str, available) -> "str | None":
    """The context enclosing this one, or None at the top.

    Walks up until it finds a path that actually holds chunks, rather than assuming the
    parent prim is a graph: a single rsplit lands on the group's own parent scope, which is
    a context only sometimes.
    """
    if not context or context == CHUNKS_ROOT:
        return None
    available = set(available)
    parent = context.rsplit("/", 1)[0]
    while parent:
        if parent in available:
            return parent
        if "/" not in parent.lstrip("/"):
            break
        parent = parent.rsplit("/", 1)[0]
    return CHUNKS_ROOT if CHUNKS_ROOT in available else None


# ---------------------------------------------------------------------------
# View nodes
#
# A view is what drives execution: chunk_stage.recook_for_view cooks the active view's
# upstream tree and blocks everything else. A context with no view therefore cooks
# NOTHING, which is why every frontend makes one on open (default_view_slot).
# ---------------------------------------------------------------------------

VIEW_MODULE = "view"
VIEW_SLOT_ATTR = "view:slot"
ACTIVE_VIEW_ATTR = "chunk:activeView"
VIEW_SLOTS = (1, 2, 3)


def context_has_view(scene, context) -> bool:
    """True when this context -- or the whole graph, if context is None -- has a view."""
    return any(
        chunk_stage.is_view_chunk(chunk) and (context is None or chunk.get_context() == context)
        for chunk in scene.get_chunks()
    )


def _free_view_slot(stage: Usd.Stage, parent_path: str) -> "int | None":
    """The lowest unused slot among this container's views.

    Scoped to the container, not the stage: each graph has its own slots 1-3. A global
    scan runs out of slots once a few contexts have views, which made "Set View" silently
    do nothing and numbered the View{slot} names across contexts.
    """
    used = set()
    for prim in stage.Traverse():
        if prim.GetAttribute(CHUNK_TYPE_ATTR).Get() != VIEW_MODULE:
            continue
        if str(prim.GetPath().GetParentPath()) != parent_path:
            continue
        slot = prim.GetAttribute(VIEW_SLOT_ATTR).Get()
        if slot is not None:
            used.add(slot)
    return next((slot for slot in VIEW_SLOTS if slot not in used), None)


def existing_view_slot_for(stage: Usd.Stage, target_prim_path: str) -> "int | None":
    """The slot of a view already wired to this prim, if there is one."""
    for prim in stage.Traverse():
        if prim.GetAttribute(CHUNK_TYPE_ATTR).Get() != VIEW_MODULE:
            continue
        exec_in = prim.GetAttribute(EXEC_INPUT)
        if not (exec_in and exec_in.IsValid()):
            continue
        for conn in exec_in.GetConnections():
            if str(conn.GetPrimPath()) == target_prim_path:
                return prim.GetAttribute(VIEW_SLOT_ATTR).Get()
    return None


def create_view_for(stage: Usd.Stage, scene, target_prim_path: str) -> "int | None":
    """Create a view node wired to target_prim_path. Returns its slot, or None.

    Returns the EXISTING slot rather than making a duplicate when a view is already wired
    to that prim, so a caller can simply activate what comes back either way.

    All USD and registry work -- no toolkit -- so every frontend creates views the same way.
    """
    target = stage.GetPrimAtPath(Sdf.Path(target_prim_path))
    if not target.IsValid():
        return None
    if target.GetAttribute(CHUNK_TYPE_ATTR).Get() == VIEW_MODULE:
        return None

    existing = existing_view_slot_for(stage, target_prim_path)
    if existing is not None:
        return existing

    parent_path = str(Sdf.Path(target_prim_path).GetParentPath())
    slot = _free_view_slot(stage, parent_path)
    if slot is None:
        return None

    registry = chunk_registry.ChunkRegistry()
    module = registry.get_chunk_module(VIEW_MODULE)
    if module is None:
        return None
    made = module.create() if module.loaded else module.load()
    if not made:
        return None
    view_chunk = made[0]
    view_chunk.name = "View%d" % slot
    view_chunk.set_state(ChunkFlags.State.COLLAPSED)

    # Read the target's position BEFORE add_chunk: adding fires CHUNK_LOADED, which a
    # frontend may answer by rebuilding its graph.
    target_x, target_y = node_position(target)

    scene.add_chunk(view_chunk)
    view_path = "%s/%s" % (chunk_stage._resolve_chunk_root(view_chunk), view_chunk.name)
    view_prim = stage.GetPrimAtPath(Sdf.Path(view_path))
    if not view_prim.IsValid():
        return None

    # First-time bookkeeping for a fresh chunk -- its exec wiring, slot marker and
    # position -- so it belongs wherever this chunk's Edit Target resolves to, not
    # wherever the stage's ambient edit target happens to point.
    with Usd.EditContext(stage, chunk_utils.get_bookkeeping_layer(stage, view_chunk)):
        exec_in = view_prim.GetAttribute(EXEC_INPUT)
        if not (exec_in and exec_in.IsValid()):
            exec_in = view_prim.CreateAttribute(EXEC_INPUT, Sdf.ValueTypeNames.Token,
                                                custom=False)
        exec_in.SetConnections([Sdf.Path("%s.%s" % (target_prim_path, EXEC_OUTPUT))])

        slot_attr = view_prim.CreateAttribute(VIEW_SLOT_ATTR, Sdf.ValueTypeNames.Int,
                                              custom=True)
        slot_attr.SetVariability(Sdf.VariabilityUniform)
        slot_attr.Set(slot)

        pos = view_prim.GetAttribute(POS_ATTR)
        if not pos:
            pos = view_prim.CreateAttribute(POS_ATTR, Sdf.ValueTypeNames.Float2)
        pos.Set(Gf.Vec2f(float(target_x) + 280.0, float(target_y) + 80.0))
    return slot


def ensure_default_view(stage: Usd.Stage, scene, context) -> "int | None":
    """Give this context a view if it has none, wired to the end of its exec chain.

    Returns the slot to activate, or None if there was nothing to do. A context with no
    view cooks nothing, so this is what makes a freshly opened network actually run.
    """
    if context_has_view(scene, context):
        return None
    tails = chunk_stage.exec_tails(stage, scene, context)
    if not tails:
        return None
    return create_view_for(stage, scene, chunk_stage.get_chunk_prim_path(tails[0]))


def context_prim(stage: Usd.Stage, context: str) -> "Usd.Prim | None":
    """The prim a context's view state lives on, or None."""
    if not stage or not context:
        return None
    prim = stage.GetPrimAtPath(Sdf.Path(context))
    return prim if prim and prim.IsValid() else None


def read_active_slot(stage: Usd.Stage, context: str) -> int:
    """The view slot driving execution in this context, 0 for none.

    Stored as chunk:activeView on the CONTEXT prim, which is where every frontend reads and
    writes -- so a file saved with slot 2 active opens on slot 2 in any of them. Slots are
    per context: a tab view and a group view can both be slot 1 without colliding.
    """
    prim = context_prim(stage, context)
    if prim is None:
        return 0
    attr = prim.GetAttribute(ACTIVE_VIEW_ATTR)
    return int(attr.Get() or 0) if attr and attr.IsValid() else 0


def write_active_slot(stage: Usd.Stage, context: str, slot: int) -> None:
    """Record which slot is active, or clear the opinion when slot is 0.

    Cleared rather than written as a 0: an authored 0 and an absent attribute would
    otherwise be two spellings of "no active view", and only one of them survives a
    composition arc.
    """
    prim = context_prim(stage, context)
    if prim is None:
        return
    if slot:
        attr = prim.GetAttribute(ACTIVE_VIEW_ATTR)
        if not (attr and attr.IsValid()):
            attr = prim.CreateAttribute(ACTIVE_VIEW_ATTR, Sdf.ValueTypeNames.Int,
                                        custom=True)
        attr.Set(int(slot))
    elif (attr := prim.GetAttribute(ACTIVE_VIEW_ATTR)) and attr.IsValid():
        prim.RemoveProperty(ACTIVE_VIEW_ATTR)


def activate_view_slot(stage: Usd.Stage, scene, context: str, chunks: dict,
                       slot: int) -> "tuple[bool, str]":
    """Switch which view drives execution, and recook. Returns (changed, status).

    The cooking is chunk_stage.recook_for_view, the same call the Qt graph makes: it blocks
    and mutes everything outside the active view's upstream tree, so this is a real
    execution switch rather than a highlight.

    The slot is resolved BEFORE anything is written. Writing first meant switching to an
    empty slot persisted a slot with no view behind it -- the attribute said 3, nothing
    cooked, and the file carried that state to the next open.

    `changed` is False only when nothing was written, so a caller knows whether it needs to
    save. A recook that raises still counts as changed: the slot is authored by then.
    """
    if context_prim(stage, context) is None or scene is None:
        return False, "no context to switch views in"
    context_chunk = chunks.get(context)
    view_chunk = None
    if slot:
        view_chunk = chunk_stage.find_view_chunk_for_slot(slot, stage, scene, context_chunk)
        if view_chunk is None:
            return False, "no view in slot %d" % slot

    write_active_slot(stage, context, slot)
    try:
        chunk_stage.recook_for_view(stage, scene, context_chunk, view_chunk)
    except Exception as exc:  # noqa: BLE001
        # Deliberately wide, and one of the few left that is: a recook executes every chunk
        # upstream of the view, which is arbitrary user code and can raise anything. Losing
        # the window because one chunk is broken would be worse than reporting it. Logged
        # as well as returned, or the traceback is gone for good.
        logger.exception("recook failed while switching view slot")
        return True, "recook failed: %s" % type(exc).__name__
    return True, ("view %d active" % slot) if slot else "no active view"


def new_view_slot(stage: Usd.Stage, scene, context_chunk,
                  target_path: "str | None" = None) -> "int | None":
    """Wire a view to `target_path`, or to the end of the chain when it is None.

    Slot allocation is scoped to the container and a view already wired to the target is
    reused rather than duplicated -- see create_view_for.
    """
    if scene is None:
        return None
    if target_path is None:
        return ensure_default_view(stage, scene, context_chunk)
    return create_view_for(stage, scene, target_path)


def default_view_slot(stage: Usd.Stage, scene, context: str,
                      context_chunk) -> "tuple[int | None, bool]":
    """The view to give a context on open. Returns (slot, activate).

    slot is None when there is nothing to do; `activate` is False when the file already
    records a slot. That second half is the rule three frontends each documented and
    implemented separately: ensure_default_view returns the EXISTING slot when a view is
    already wired to the tail, so activating it on every open reset whichever view the user
    had chosen back to that one.

    Without a view nothing cooks at all, so a network would open looking correct and do
    nothing -- which is exactly how the non-Qt frontends each differed from Qt in turn.
    """
    if scene is None:
        return None, False
    already = read_active_slot(stage, context)
    try:
        slot = ensure_default_view(stage, scene, context_chunk)
    except (AttributeError, KeyError, RuntimeError, ValueError):
        logger.exception("could not create a default view")
        return None, False
    if slot is None:
        return None, False
    return slot, not already
