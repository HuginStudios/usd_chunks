"""Python script exporter for the node graph."""
import re
import sys
from pathlib import Path

from . import nodes


def export_python_script(graph, path):
    """Export graph as a Python bootstrap script.

    Chunk nodes emit create_chunks + set-param code.
    Pure USD nodes emit their to_python() snippet.
    """
    visited = set()
    order = []

    def _topo(node):
        if not hasattr(node, 'input_ports'):
            return
        nid = node.id
        if nid in visited:
            return
        visited.add(nid)
        for port in node.input_ports():
            for connected in port.connected_ports():
                _topo(connected.node())
        order.append(node)

    for n in graph.all_nodes():
        _topo(n)

    lines = [
        '"""Auto-generated from Node Graph.',
        '',
        'Chunk bootstrap sections require usd_chunks + a running ChunkRegistry',
        '(i.e. run inside usdview or an equivalent host).',
        '"""',
        'from pxr import Usd, UsdGeom, UsdShade, Sdf, Gf, Vt',
        'from usd_chunks import chunk',
        '',
        'stage = Usd.Stage.CreateInMemory()',
        '',
    ]

    _INTERNAL_PROPS = {
        "id", "name", "color", "disabled", "selected",
        "width", "height", "icon", "pos", "type_", "thumb",
    }

    var_counter = [0]
    node_vars = {}

    def _var(node, port_name):
        key = (node.id, port_name)
        if key not in node_vars:
            var_counter[0] += 1
            node_vars[key] = f"v{var_counter[0]}"
        return node_vars[key]

    for node in order:
        if not isinstance(node, nodes.UsdNodeBase) or node.disabled():
            continue

        lines.append(f"# {node.name()}")

        if isinstance(node, nodes.ChunkWrapperBase):
            module = node._chunk_module
            lines.append(f"_chunks = chunk.create_chunks('{module}')")
            lines.append("_c = _chunks[0] if _chunks else None")
            try:
                props = node.model.custom_properties
            except Exception:
                props = {}
            for k, v in props.items():
                if k in _INTERNAL_PROPS:
                    continue
                lines.append(
                    f"if _c and _c.get_param('{k}'): "
                    f"_c.get_param('{k}').value = {v!r}"
                )
        else:
            var_map = {}
            for p in node.output_ports():
                var_map[p.name()] = _var(node, p.name())
            for p in node.input_ports():
                connected = p.connected_ports()
                if connected:
                    src_port = connected[0]
                    src_var = node_vars.get((src_port.node().id, src_port.name()))
                    if src_var:
                        var_map[p.name()] = src_var
            lines.append(node.to_python(var_map))

        lines.append("")

    lines.append("stage.GetRootLayer().Export('output.usda')")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[NodeGraph] Exported to {path}")


def export_group_as_chunk(group_node, out_dir=None, sub=None):
    """Export a GroupNode as a self-contained chunk .py module.

    Promoted params become chunk params; inner node code is generated
    via each node's to_python().  Writes to out_dir (default:
    ~/.usd_chunks/user_chunks/) and hot-registers with ChunkRegistry.

    ``sub`` is the expanded SubGraph — the caller is responsible for
    expanding and collapsing the group around this call.

    Returns the output Path.
    """
    from NodeGraphQt.nodes.port_node import PortInputNode, PortOutputNode
    from .nodes._base import _get_promotions

    name = group_node.name()
    snake = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "group_chunk"

    if sub is None:
        sub = group_node.get_sub_graph()
    if not sub:
        raise RuntimeError(
            f"Could not access inner nodes of group '{name}'. "
            f"Try right-clicking again — the group will be expanded automatically."
        )

    all_inner = sub.all_nodes()

    def is_port_node(n):
        return isinstance(n, (PortInputNode, PortOutputNode))

    # Topo-sort (skip port boundary nodes)
    visited, order = set(), []

    def _topo(n):
        if n.id in visited:
            return
        visited.add(n.id)
        for p in n.input_ports():
            for cp in p.connected_ports():
                if not is_port_node(cp.node()):
                    _topo(cp.node())
        if not is_port_node(n):
            order.append(n)

    for n in all_inner:
        _topo(n)

    promotions = _get_promotions(group_node)

    # Assign output variable names
    vc = [0]
    out_vars = {}  # (node_id, port_name) → var_name

    for n in order:
        for p in n.output_ports():
            vc[0] += 1
            out_vars[(n.id, p.name())] = f"_v{vc[0]}"

    PROMO = "_promo_"

    # Build param declaration lines
    param_lines = []
    for port_name, info in promotions.items():
        cls = info.get("chunk_param_class", "LineEdit")
        kw = info.get("chunk_param_kwargs") or {}
        default = info.get("default_value")
        parts = [f"{k}={v!r}" for k, v in kw.items() if v is not None]
        if default is not None:
            parts.append(f"default_value={default!r}")
        kw_str = (", " + ", ".join(parts)) if parts else ""
        param_lines.append(f"    {port_name} = chunk.{cls}({port_name!r}{kw_str})")

    # Build promoted-value assignments for execute body
    promo_assigns = []
    for port_name, info in promotions.items():
        cls = info.get("chunk_param_class", "LineEdit")
        sentinel = f"{PROMO}{port_name}"
        if cls in ("Slider", "Spinbox"):
            cast_fn = "float"
        elif cls == "Checkbox":
            cast_fn = "bool"
        else:
            cast_fn = "str"
        promo_assigns.append(
            f"        {sentinel} = {cast_fn}(owner.get_param({port_name!r}).value)"
        )

    # Reverse map: (inner_node_id, inner_param) → sentinel var name
    promo_rev = {}
    for port_name, info in promotions.items():
        promo_rev[(info.get("inner_node_id"), info.get("inner_param"))] = (
            f"{PROMO}{port_name}"
        )

    # Generate code per node
    node_blocks = []
    for n in order:
        var_map = {}
        for p in n.output_ports():
            var_map[p.name()] = out_vars.get((n.id, p.name()), "_unused")
        for p in n.input_ports():
            for cp in p.connected_ports():
                if not is_port_node(cp.node()):
                    sv = out_vars.get((cp.node().id, cp.name()))
                    if sv:
                        var_map[p.name()] = sv

        # Temporarily substitute promoted param values with sentinel vars
        overrides = {}
        for (nid, pname), sentinel in promo_rev.items():
            if nid == n.id:
                try:
                    overrides[pname] = n.get_property(pname)
                    n.set_property(pname, sentinel)
                except Exception:
                    pass

        if isinstance(n, nodes.ChunkWrapperBase):
            block = f"        # TODO: chunk node '{n.name()}' — implement manually"
        else:
            code = n.to_python(var_map)
            indented = "\n".join(f"        {ln}" for ln in code.splitlines())
            block = (f"        # {n.name()}\n{indented}"
                     if not code.startswith("#") else indented)

        # Restore overridden properties
        for pname, orig in overrides.items():
            try:
                n.set_property(pname, orig)
            except Exception:
                pass

        # Strip quotes wrapping sentinel names (handles string-typed to_python paths)
        for port_name in promotions:
            sentinel = f"{PROMO}{port_name}"
            block = block.replace(f'"{sentinel}"', sentinel)
            block = block.replace(f"'{sentinel}'", sentinel)

        node_blocks.append(block)

    body = "\n".join(node_blocks) if node_blocks else "        pass"

    all_param_names = list(promotions.keys()) + ["execute_btn"]
    params_list = "[" + ", ".join(all_param_names) + "]"

    # Assemble the file
    file_lines = [
        "from pxr import Usd, UsdGeom, UsdShade, Sdf, Gf, Vt",
        "from usd_chunks import chunk",
        "",
        "",
        "def config():",
        '    return chunk.DefaulConfig(category="User")',
        "",
        "",
        "def main():",
    ]
    file_lines.extend(param_lines)
    if param_lines:
        file_lines.append("")
    file_lines += [
        "    def _execute(owner, notice, sender):",
        "        stage = owner.get_stage()",
        "        if not stage:",
        "            return",
    ]
    file_lines.extend(promo_assigns)
    file_lines.append(body)
    file_lines += [
        "",
        '    execute_btn = chunk.Button("execute_btn", "Execute")',
        "",
        f"    c = chunk.Chunk({name!r})",
        f"    c.add_params({params_list})",
        "    c.add_callback(_execute, sender=execute_btn)",
        "    return c",
    ]

    if out_dir is None:
        out_dir = Path.home() / ".usd_chunks" / "user_chunks"
    out_path = Path(out_dir) / f"{snake}.py"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(file_lines), encoding="utf-8")
    print(f"[NodeGraph] Exported chunk to {out_path}")

    # Hot-register: drop stale entries, re-import, add to scene
    try:
        from usd_chunks.chunk_registry import ChunkRegistry
        from usd_chunks.chunk import create_chunks

        reg = ChunkRegistry()
        reg.registered_modules.pop(snake, None)
        sys.modules.pop(snake, None)
        reg.import_chunk(out_path)
        create_chunks(snake)
        print(f"[NodeGraph] Hot-registered '{snake}'")
    except Exception as exc:
        print(f"[NodeGraph] Hot-registration failed: {exc}")

    return out_path
