"""The node graph as data: nodes, ports and edges, with no toolkit attached.

It depends on `chunk_naming` for the chunk vocabulary and reaches for `chunk_usd` only
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
from typing import Protocol, runtime_checkable

from pxr import Sdf, Usd

from usd_chunks import chunk_core as core
from usd_chunks import chunk_utils
from usd_chunks.chunk import Checkbox, ColorPicker, FileBrowser, LineEdit, Menu, Slider, Spinbox, TextEdit
from usd_chunks import chunk_naming
from usd_chunks.chunk_flags import ChunkFlags
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

EXEC_INPUT = chunk_naming.EXEC_INPUT
EXEC_OUTPUT = chunk_naming.EXEC_OUTPUT
CHUNKS_ROOT = chunk_naming.CHUNKS_ROOT

#: The prim type a chunk is, and the root scope, both taken from chunk_usd rather than
#: spelled out again -- renaming either there should not leave the graph quietly finding
#: nothing. Bound directly, not via getattr with a fallback: a fallback would paper over
#: exactly the rename it is supposed to catch.
CHUNK_TYPENAME = chunk_naming.CHUNK_PRIM_TYPENAME

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
    see or set directly -- e.g. scripts/create_layer.py's _new_layer_id/_layer_name_used/
    _parent_layer_id, which cache identifiers for the anon Sdf.Layer *that specific chunk
    instance* created. Copying those verbatim onto a pasted chunk hands it the ORIGINAL
    instance's cached identifier; since that layer is still alive (Sdf.Layer.Find succeeds
    process-wide, not scoped to this chunk), the pasted chunk's own next execute() silently
    adopts and reuses the original's real layer object instead of creating an independent one
    of its own -- the two nodes end up secretly sharing state. Not scoped to
    scripts/create_layer.py specifically -- any chunk module using a HIDDEN param the same way
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
    __slots__ = ("module_name", "values", "rel_pos", "children", "child_edges", "list_sizes")

    def __init__(
        self, module_name: str, values: list, rel_pos: tuple[float, float],
        children: "list[ClipboardNode] | None" = None,
        child_edges: "list[ClipboardEdge] | None" = None,
        list_sizes: "list[tuple[str, int]] | None" = None,
    ):
        self.module_name = module_name
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
            if chunk_usd.is_exec_input_name(attr_name):
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
        chunk_usd.get_chunk_prim_path(c): c
        for c in scene_sender.get_chunks()
        if c.get_context() == chunk and c is not chunk and not chunk_usd.is_view_chunk(c)
    }
    root_prim = stage.GetPrimAtPath(Sdf.Path(chunk.prim_path)) if chunk.prim_path else None
    if root_prim is not None and root_prim.IsValid():
        for prim in root_prim.GetFilteredChildren(INCLUDE_INACTIVE):
            path = str(prim.GetPath())
            if path in by_path:
                continue
            ctx_chunk = scene_sender.get_context_for_prim_path(path)
            if ctx_chunk is not None and not chunk_usd.is_view_chunk(ctx_chunk):
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
        list_sizes=snapshot_list_sizes(chunk),
    )
    if not chunk_usd.is_group_chunk(chunk):
        return clip_node

    children = iter_group_children(chunk, scene_sender, stage)
    child_prims: dict[str, Usd.Prim] = {}
    child_pos: dict[str, tuple[float, float]] = {}
    for child in children:
        path = chunk_usd.get_chunk_prim_path(child)
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
        path = chunk_usd.get_chunk_prim_path(child)
        pos = child_pos[path]
        child_clip = snapshot_chunk_node(
            child, scene_sender, stage, (pos[0] - min_x, pos[1] - min_y)
        )
        if child_clip is None:
            continue
        kept_index[path] = len(clip_node.children)
        clip_node.children.append(child_clip)

    for child in children:
        path = chunk_usd.get_chunk_prim_path(child)
        dst_index = kept_index.get(path)
        prim = child_prims.get(path)
        if dst_index is None or prim is None:
            continue
        for dst_attr, src_prim_path, src_attr in iter_prim_connections(prim):
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

        Must key on chunk_usd.is_chunkgroup, not on the CONTEXT flag: a chunk-in-chunk
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

        Must go through chunk_usd.recook_for_view -- a highlight is not a view switch --
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
