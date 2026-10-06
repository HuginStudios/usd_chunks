"""Chunkgroups in Houdini: the ``usdchunks::chunkgroup`` node, importing and exporting.

A chunkgroup node keeps a usd_chunks network contained: chunk nodes inside it, the group's
public params as parms on it. It is also the export boundary -- Export Network writes it as
a network file, and only what has a chunk to become can be inside it (chunk nodes, nested
chunkgroups, and the Merge LOPs import puts where several chunks feed one). A Solaris node
in there has no chunk to become, so export refuses and names it, rather than dropping a wire
or writing a node usdview and Blender cannot run. Solaris nodes belong upstream, feeding the
group's input, as a chunkgroup takes whatever its upstream produced.

Import: a saved network is chunk prims under /chunks, wired with USD connections. Here it
becomes what Houdini would have built by hand:

- a chunkgroup -> a chunkgroup node of the same name, its public params (the group prim's
  own ``inputs:*``, which inner chunks connect to) as parms on it;
- a chunk -> a chunk LOP (lop.create), its saved values on its parms;
- an exec wire -> a LOP wire; several into one chunk go through a Merge LOP. An inner chunk
  with no exec input takes the subnet's input, as it took the group's upstream;
- a param wired to another param (``@AddMesh.prim_path@``) -> a channel reference,
  ``chs("../AddMesh/prim_path")``; to a group's public param -> ``ch("../floors")``;
- a View -> the subnet's output (the active view slot's, chunk:activeView), or the display
  flag at the top level. Views are usd_chunks' way of picking an output; Houdini has its own.

Values go chunk-side first (the param, as the loader sets it) and reach the parms through
parms.pull, so a parm is named and typed exactly as one made from the TAB menu.

Export is the reverse, written by export_worker in a process of its own (see there).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import hou
import loptoolutils
from pxr import Sdf, Usd

from usd_chunks import chunk_core as core
from usd_chunks import chunk_stage
from usd_chunks.chunk_naming import CHUNKS_ROOT
from usd_chunks.chunk_process import python_executable
from usd_chunks.graph import engine as chunk_graph
from usd_chunks.hosts.houdini import lop, parms, space
from usd_chunks.hosts.houdini.scene import ensure_scene

logger = logging.getLogger(__name__)

#: The chunkgroup node type.
GROUP_TYPE = "usdchunks::chunkgroup"
#: Its Export Network button.
EXPORT_PARM = "export_network"
EXPORT_SCRIPT = "from usd_chunks.hosts.houdini import chunkgroup; chunkgroup.export_from_button(kwargs)"
#: Nodes a chunkgroup may hold besides chunks and chunkgroups: its output, and the Merge
#: LOPs that stand for several exec inputs into one chunk.
_PLUMBING = {"output", "merge"}
#: A channel reference as import writes one: ch("../X") or chs("../Node/parm").
_REFERENCE = re.compile(r'^\s*chs?\(\s*"([^"]+)"\s*\)\s*$')

_INPUTS = "inputs:"
_EXEC = "inputs:chunk"
_TYPE = "info:chunkType"
_POS = "ui:nodegraph:node:pos"
#: usd_chunks node-graph pixels per Houdini network unit.
_SCALE = 130.0


def to_houdini(pos) -> hou.Vector2:
    """A usd_chunks node-graph position as a Houdini network position.

    The axes swap: a usd_chunks graph flows left to right (x grows downstream, y grows
    down the screen), a Houdini network top to bottom. So the graph's x becomes Houdini's
    downward axis (-y), and its y Houdini's x. from_houdini is the inverse.
    """
    x, y = pos
    return hou.Vector2(y / _SCALE, -x / _SCALE)


def from_houdini(pos) -> list[float]:
    """A Houdini network position as a usd_chunks node-graph position; see to_houdini."""
    hx, hy = pos
    return [-hy * _SCALE, hx * _SCALE]


def _chunk_type(prim: Usd.Prim) -> str:
    attr = prim.GetAttribute(_TYPE)
    return str(attr.Get()) if attr and attr.HasValue() else ""


def _chunk_children(prim: Usd.Prim) -> list[Usd.Prim]:
    """The graph's nodes, as every frontend reads them (disabled ones included)."""
    return [c for c in chunk_graph.resolve_graph_nodes(prim.GetStage(), str(prim.GetPath())) if _chunk_type(c)]


def _is_exec(attr: Usd.Attribute) -> bool:
    name = attr.GetName()
    return name == _EXEC or name.startswith(_EXEC + ":")


def _place(node: hou.Node, prim: Usd.Prim) -> None:
    attr = prim.GetAttribute(_POS)
    if attr and attr.HasValue():
        node.setPosition(to_houdini(attr.Get()))


def _parm_name(chunk, param) -> str | None:
    for e in parms.entries(chunk):
        if e.param is param:
            return e.name
    return None


def _group_parm(subnet: hou.Node, attr: Usd.Attribute) -> None:
    """A chunkgroup's public param, as a parm on its subnet."""
    name = attr.GetName()[len(_INPUTS):].replace(":", "_")
    value = attr.Get()
    label = name.replace("_", " ")
    type_name = attr.GetTypeName()
    if type_name == Sdf.ValueTypeNames.Bool:
        template = hou.ToggleParmTemplate(name, label, default_value=bool(value))
    elif type_name in (Sdf.ValueTypeNames.Int, Sdf.ValueTypeNames.Int64):
        template = hou.IntParmTemplate(name, label, 1, default_value=(int(value or 0),))
    elif type_name in (Sdf.ValueTypeNames.Float, Sdf.ValueTypeNames.Double, Sdf.ValueTypeNames.Half):
        template = hou.FloatParmTemplate(name, label, 1, default_value=(float(value or 0.0),))
    elif value is not None and hasattr(value, "__len__") and not isinstance(value, str) and len(value) <= 4:
        template = hou.FloatParmTemplate(name, label, len(value), default_value=tuple(float(v) for v in value))
    else:
        template = hou.StringParmTemplate(name, label, 1, default_value=(str(value or ""),))
    group = subnet.parmTemplateGroup()
    group.append(template)
    subnet.setParmTemplateGroup(group)


def _restore_values(node: hou.Node, prim: Usd.Prim) -> list[tuple[object, Usd.Attribute]]:
    """Set the chunk's params from its prim; returns the params that are wired instead.

    Values go onto the chunk, as the loader sets them, then onto the parms with pull.
    """
    chunk = lop.chunk_for(node)
    scene = ensure_scene()
    wired = []
    with lop._no_stage(scene):
        for attr in prim.GetAttributes():
            name = attr.GetName()
            if not name.startswith(_INPUTS) or _is_exec(attr):
                continue
            path = name[len(_INPUTS):].replace(":", ".")
            parent_path, _, template = path.rpartition(".")
            parent = chunk.get_param(parent_path) if parent_path else None
            if isinstance(parent, core.ChunkListGroupBase):
                values = attr.Get()
                if values is None or isinstance(values, str):
                    continue
                values = list(values)
                if parent.size < len(values):
                    parent.size = len(values)
                for i, v in enumerate(values[: len(parent.get_my_params())]):
                    if (proxy := parent.get_indexed_param(template, i)) is not None:
                        proxy.value = v
                continue
            param = chunk.get_param(path)
            if param is None:
                logger.info("%s: no param %s on %s; skipped", prim.GetPath(), path, node.path())
                continue
            if attr.HasAuthoredConnections():
                wired.append((param, attr))
                continue
            if isinstance(param, core.ChunkCommon) and param.has_flag(parms.ChunkFlags.ParamFlags.OUTPUT):
                continue
            value = attr.Get()
            if value is None:
                continue
            if isinstance(value, str) and value.startswith("@") and value.endswith("@"):
                continue  # an expression; its connection carries the wire
            try:
                param.value = tuple(value) if hasattr(value, "__len__") and not isinstance(value, str) else value
            except Exception as exc:
                logger.warning("%s: could not set %s = %r (%s)", node.path(), path, value, exc)
    parms.pull(node, chunk)
    return wired


def _reference(node: hou.Node, param, attr: Usd.Attribute, group_prim: Usd.Prim | None,
               nodes: dict[Sdf.Path, hou.Node]) -> None:
    """Make a wired param a channel reference to what it is wired to."""
    chunk = lop.chunk_for(node)
    name = _parm_name(chunk, param)
    parm = node.parm(name) if name else None
    if parm is None:
        return
    target = attr.GetConnections()[0]
    source_prim, source_attr = target.GetPrimPath(), target.name
    if not source_attr.startswith(_INPUTS):
        return
    if group_prim is not None and source_prim == group_prim.GetPath():
        rel = "../" + source_attr[len(_INPUTS):].replace(":", "_")
    elif (source := nodes.get(source_prim)) is not None:
        source_chunk = lop.chunk_for(source)
        source_param = source_chunk.get_param(source_attr[len(_INPUTS):].replace(":", "."))
        source_name = _parm_name(source_chunk, source_param) if source_param is not None else None
        if source_name is None:
            return
        rel = f"{node.relativePathTo(source)}/{source_name}"
    else:
        logger.info("%s: %s is wired outside the imported network; kept its value", node.path(), name)
        return
    is_string = parm.parmTemplate().type() == hou.parmTemplateType.String
    parm.setExpression(f'chs("{rel}")' if is_string else f'ch("{rel}")', hou.exprLanguage.Hscript)


def _import_children(container: Usd.Prim, parent: hou.Node, group_prim: Usd.Prim | None) -> hou.Node | None:
    """Create container's chunks in parent and wire them. Returns the node the output is."""
    nodes: dict[Sdf.Path, hou.Node] = {}
    views: list[tuple[int, Usd.Prim]] = []
    wired: list[tuple[hou.Node, object, Usd.Attribute]] = []
    for prim in _chunk_children(container):
        kind = _chunk_type(prim)
        if kind == "view":
            slot = prim.GetAttribute("view:slot")
            views.append((int(slot.Get()) if slot and slot.HasValue() else 0, prim))
            continue
        if kind == "chunkgroup":
            node = import_group(prim, parent)
        else:
            try:
                node = lop.create(parent, kind, name=prim.GetName())
            except hou.Error as exc:
                logger.warning("%s: could not create chunk %s (%s)", prim.GetPath(), kind, exc)
                continue
            wired += [(node, p, a) for p, a in _restore_values(node, prim)]
        _place(node, prim)
        if not prim.IsActive():
            node.bypass(True)  # a disabled chunk stays in the graph, passing its input through
        nodes[prim.GetPath()] = node

    for prim_path, node in nodes.items():
        sources = [nodes[c.GetPrimPath()] for c in chunk_stage.exec_input_connections(container.GetStage().GetPrimAtPath(prim_path))
                   if c.GetPrimPath() in nodes]
        if len(sources) > 1:
            merge = parent.createNode("merge", f"{node.name()}_merge")
            for i, source in enumerate(sources):
                merge.setInput(i, source)
            merge.setPosition(node.position() + hou.Vector2(0, 1))
            node.setInput(0, merge)
        elif sources:
            node.setInput(0, sources[0])
        elif group_prim is not None and (entry := parent.node(space.IN_NODE)) is not None:
            node.setInput(0, entry)  # the group's input, turned into its network space
        elif group_prim is not None and parent.indirectInputs():
            node.setInput(0, parent.indirectInputs()[0])

    for node, param, attr in wired:
        _reference(node, param, attr, group_prim, nodes)

    active = None
    if group_prim is not None:
        attr = group_prim.GetAttribute("chunk:activeView")
        active = int(attr.Get()) if attr and attr.HasValue() else None
    views.sort(key=lambda v: (v[0] != active, v[0]))
    for _, view in views:
        targets = [c.GetPrimPath() for c in chunk_stage.exec_input_connections(view)]
        if targets and targets[0] in nodes:
            return nodes[targets[0]]
    return None


def _prepare(group: hou.Node, network_space: tuple[str, float] | None = None) -> hou.Node:
    """Unlock a new chunkgroup node, give it its network space and its boundary nodes."""
    group.allowEditingOfContents()
    up, mpu = network_space or space.stage_space(None)
    # A node type built before the Network Space parms has none to set; its group then
    # cooks in Houdini's space, as groups did before (space.group_space reads None).
    if group.parm(space.UP_PARM) is not None:
        group.parm(space.UP_PARM).set(up)
        group.parm(space.MPU_PARM).set(mpu)
    space.ensure_nodes(group)
    return group


def create_group(parent: hou.Node, name: str = "chunkgroup",
                 network_space: tuple[str, float] | None = None) -> hou.Node:
    """An empty chunkgroup node in parent, its contents the user's to edit.

    network_space is the (up axis, meters per unit) its chunks cook in; Houdini's own when
    omitted, as for a group started here.
    """
    return _prepare(parent.createNode(GROUP_TYPE, name), network_space)


def create_from_tool(kwargs) -> hou.Node | None:
    """The TAB menu's Chunkgroup entry: an empty chunkgroup node where the user is working."""
    node = loptoolutils.genericTool(kwargs, GROUP_TYPE, nodename="chunkgroup")
    return _prepare(node) if node is not None else None


def import_group(prim: Usd.Prim, parent: hou.Node) -> hou.Node:
    """A chunkgroup prim as a chunkgroup node in parent, its chunks inside.

    The group keeps the space the file was authored in, so its chunks cook as they did there.
    """
    group = create_group(parent, prim.GetName(), space.stage_space(prim.GetStage()))
    inner = {a.GetPrimPath() for c in _chunk_children(prim) for attr in c.GetAttributes()
             for a in attr.GetConnections()}
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if name.startswith(_INPUTS) and not _is_exec(attr) and prim.GetPath() in inner:
            _group_parm(group, attr)
    out = _import_children(prim, group, prim)
    exit_node = group.node(space.OUT_NODE) or next(
        (c for c in group.children() if c.type().name() == "output"), None)
    if exit_node is not None and out is not None:
        exit_node.setInput(0, out)
        out.setDisplayFlag(True)
    if not any(c.GetAttribute(_POS) for c in _chunk_children(prim)):
        group.layoutChildren()
    return group


def import_file(path: str, parent: hou.Node | None = None) -> list[hou.Node]:
    """Import every network in a chunk .usda into a LOP network. Returns the top nodes."""
    parent = parent or hou.node("/stage")
    ensure_scene()
    stage = Usd.Stage.Open(path, Usd.Stage.LoadNone)
    root = stage.GetPrimAtPath(CHUNKS_ROOT)
    if not root:
        raise hou.OperationFailed(f"{path} holds no usd_chunks network (no {CHUNKS_ROOT})")
    before = set(parent.children())
    out = _import_children(root, parent, None)
    if out is not None:
        out.setDisplayFlag(True)
    created = [n for n in parent.children() if n not in before]
    for node in created:
        node.setSelected(True, clear_all_selected=node is created[0])
    return created


def import_from_tool(kwargs) -> list[hou.Node]:
    """The TAB menu's Import entry: pick a file, import it where the user is working."""
    path = hou.ui.selectFile(title="Import usd_chunks network", pattern="*.usda *.usd *.usdc",
                             file_type=hou.fileType.Usd, chooser_mode=hou.fileChooserMode.Read)
    if not path:
        return []
    path = hou.text.expandString(path)
    pane = kwargs.get("pane")
    parent = pane.pwd() if pane is not None and hasattr(pane, "pwd") else hou.node("/stage")
    if parent.childTypeCategory() != hou.lopNodeTypeCategory():
        parent = hou.node("/stage")
    created = import_file(os.path.normpath(path), parent)
    return created


# --------------------------------------------------------------------------- export


class ExportError(hou.OperationFailed):
    """A chunkgroup that cannot be written as a network, and why."""


def _jsonable(value):
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if isinstance(value, Sdf.AssetPath):
        return value.path
    if hasattr(value, "__len__"):
        return [_jsonable(v) for v in value]
    return str(value)


def _exec_inputs(node: hou.Node) -> list[str]:
    """The names of the chunk or chunkgroup nodes feeding node, through Merge LOPs."""
    names = []
    for source in node.inputs():
        if source is None:
            continue
        if space.is_space_node(source):
            # space_in is the group's input, and a chunk wired to it has no exec input;
            # space_out passes through to what feeds the group's output.
            names += _exec_inputs(source) if source.name() == space.OUT_NODE else []
        elif source.type().name() == "merge":
            names += _exec_inputs(source)
        else:
            names.append(source.name())
    return names


def _is_default(param, value) -> bool:
    default = getattr(param, "default_value", None)
    return default is not None and parms._same(default, value)


def _parm_expression(pt: hou.ParmTuple) -> str | None:
    try:
        return pt[0].expression()
    except hou.OperationFailed:
        return None


def _chunk_spec(node: hou.Node, group: hou.Node, public: set[str]) -> dict:
    """One chunk node: its type, inputs, the values that differ from default, and its wires.

    A parm referencing a sibling chunk's parm becomes a param wire, one referencing a group
    parm a lofted param -- the two references import writes. Any other expression is a
    Houdini one with no usd_chunks form, so it goes out as the value it has now.
    """
    chunk = lop.chunk_for(node)
    with lop._no_stage(ensure_scene()):
        parms.push(node, chunk)
    spec = {"kind": "chunk", "name": node.name(), "type": node.evalParm(parms.TYPE_PARM),
            "inputs": _exec_inputs(node), "values": {}, "lists": {}, "wires": {}, "lofts": {}}
    for e in parms.entries(chunk, rows=False):
        param = e.param
        if e.kind == "list":
            count = len(param.get_my_params())
            rows = {t.name: [_jsonable(param.get_indexed_param(t.name, i).value) for i in range(count)]
                    for t in parms._list_templates(param)}
            spec["lists"][parms.param_path(chunk, param)] = {"size": param.size, "rows": rows}
            continue
        if e.kind not in ("value", "vector") or parms._is_output(param):
            continue
        path = parms.param_path(chunk, param)
        pt = node.parmTuple(e.name)
        expression = _parm_expression(pt) if pt is not None else None
        match = _REFERENCE.match(expression or "")
        if match:
            target = match.group(1)
            if target.startswith("../") and target[3:] in public:
                spec["lofts"][path] = target[3:]
                continue
            source_path, _, source_parm = target.rpartition("/")
            source_node = node.node(source_path) if source_path else None
            if source_node is not None and source_node.parent() == group \
                    and source_node.type().name() == lop.HDA_TYPE:
                source_chunk = lop.chunk_for(source_node)
                source_param = parms.find(source_chunk, source_parm)
                if source_param is not None:
                    spec["wires"][path] = [source_node.name(), parms.param_path(source_chunk, source_param)]
                    continue
        if expression:
            logger.warning("%s: %s is a Houdini expression (%s); exported as its value",
                           node.path(), e.name, expression)
        if not _is_default(param, param.value):
            spec["values"][path] = _jsonable(param.value)
    return spec


def _ordered(nodes: list[dict]) -> list[dict]:
    """nodes with every node after the ones feeding it: the scripting API wires on create."""
    by_name = {n["name"]: n for n in nodes}
    done: set[str] = set()
    out: list[dict] = []

    def visit(node, seen=()):
        if node["name"] in done or node["name"] in seen:
            return
        for name in node.get("inputs", []):
            if name in by_name:
                visit(by_name[name], (*seen, node["name"]))
        done.add(node["name"])
        out.append(node)

    for node in nodes:
        visit(node)
    return out


def collect(group: hou.Node) -> dict:
    """A chunkgroup node as export_worker's spec.

    Raises:
        ExportError: A node inside it has no chunk to become -- a Solaris node. Named, so
            the user can move it upstream of the group.
    """
    foreign: list[str] = []
    spec = _collect(group, foreign)
    if foreign:
        raise ExportError(
            "These nodes are not chunks, so the network cannot hold them:\n  "
            + "\n  ".join(foreign)
            + "\nMove them upstream of the chunkgroup (feeding its input), or remove them.")
    return spec


def _collect(group: hou.Node, foreign: list[str]) -> dict:
    public = {p.tuple().name() for p in group.spareParms()} - {EXPORT_PARM}
    nodes = []
    for child in group.children():
        kind = child.type().name()
        if kind in _PLUMBING or space.is_space_node(child):
            continue
        if kind == lop.HDA_TYPE:
            node = _chunk_spec(child, group, public)
        elif kind == GROUP_TYPE:
            node = {"kind": "group", **_collect(child, foreign), "inputs": _exec_inputs(child)}
        else:
            foreign.append(child.path())
            continue
        node["pos"] = from_houdini(child.position())
        nodes.append(node)
    output = next((c for c in group.children() if c.type().name() == "output"), None)
    feeding = _exec_inputs(output) if output is not None else []
    params = {}
    for name in sorted(public):
        if (pt := group.parmTuple(name)) is not None:
            values = pt.eval()
            params[name] = _jsonable(values[0] if len(values) == 1 else values)
    spec = {"name": group.name(), "output": feeding[0] if feeding else "",
            "params": params, "nodes": _ordered(nodes)}
    if group.parm(space.UP_PARM) is not None:
        spec["up_axis"] = group.evalParm(space.UP_PARM).upper()
        spec["meters_per_unit"] = float(group.evalParm(space.MPU_PARM))
    return spec


def export(group: hou.Node, path: str) -> str:
    """Write a chunkgroup node as a usd_chunks network file. Returns the path written.

    Raises:
        ExportError: It holds a Solaris node (see collect), or the writer failed.
    """
    spec = collect(group)
    folder = Path(tempfile.mkdtemp(prefix="usdchunks_export_"))
    spec_file = folder / "spec.json"
    spec_file.write_text(json.dumps(spec, indent=1), encoding="utf-8")
    # The child imports this same usd_chunks, and Houdini's pxr through the folders on
    # this process's path -- the way sim_bake starts its workers.
    root = str(Path(__file__).resolve().parents[3])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([root] + [p for p in sys.path if p and p != root]))
    proc = subprocess.run(
        [python_executable(), "-m", "usd_chunks.hosts.houdini.export_worker", str(spec_file), path],
        capture_output=True, text=True, env=env, cwd=str(folder),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0 or "USD_CHUNKS_EXPORTED" not in proc.stdout:
        raise ExportError(f"Writing {path} failed:\n{(proc.stderr or proc.stdout)[-2000:]}")
    return path


def export_from_button(kwargs) -> None:
    """The chunkgroup node's Export Network button."""
    group = kwargs["node"]
    path = hou.ui.selectFile(title="Export usd_chunks network", pattern="*.usda",
                             default_value=f"{group.name()}.usda",
                             chooser_mode=hou.fileChooserMode.Write)
    if not path:
        return
    path = os.path.normpath(hou.text.expandString(path))
    if not os.path.splitext(path)[1]:
        path += ".usda"
    try:
        export(group, path)
    except ExportError as exc:
        hou.ui.displayMessage(str(exc), severity=hou.severityType.Warning, title="Export Network")
        return
    hou.ui.displayMessage(f"Wrote {path}", title="Export Network")
