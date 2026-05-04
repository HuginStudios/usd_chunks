"""Python script exporter for the node graph."""
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
