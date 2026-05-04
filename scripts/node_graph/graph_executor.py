"""
GraphExecutor — owns topological sort, dirty tracking, and the debounce timer.

NodeGraphWidget holds one instance and delegates all execution calls here.
Only PySide6.QtCore.QTimer and PySide6.QtWidgets.QApplication are imported
from Qt (no widget classes).
"""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from NodeGraphQt import GroupNode
from . import nodes as _nodes
from .nodes._base import _get_promotions, _get_promoted_outputs


class GraphExecutor:
    """Centralises node execution: topo-sort, debounce, dirty propagation.

    The widget reference is used only for:
    - widget._stage          (read-only)
    - widget._graph          (read-only)
    - widget._set_exec_state (state badge callback)
    - widget._prim_thumbs_enabled / widget._capture_prim_thumb (thumbnail)
    """

    def __init__(self, widget):
        self._widget = widget
        # node.id → layer_identifier; populated on execution, consumed on deletion
        self._node_layer_registry: dict = {}
        # Debounce: property changes collapse into a single run 150 ms after last change
        self._debounce_timer = QTimer()
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(150)
        self._debounce_node = None
        self._debounce_timer.timeout.connect(self._flush_debounce)

    # ── Public entry points ──────────────────────────────────────────────────

    def run_full(self):
        """Execute all nodes in topological order (initial load / undo-redo)."""
        widget = self._widget
        if not widget._stage:
            print("[NodeGraph] No stage set — cannot execute.")
            return

        visited: set = set()
        order: list = []

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

        for n in widget._graph.all_nodes():
            _topo(n)

        errored: set = set()
        for node in order:
            upstream_errored = any(
                cp.node().id in errored
                for port in (node.input_ports() if hasattr(node, 'input_ports') else [])
                for cp in port.connected_ports()
            )
            if upstream_errored:
                widget._set_exec_state(node, "skipped")
                errored.add(node.id)
                node._output_cache = {}
                continue
            if self._exec_node(node) is False:
                errored.add(node.id)
            # ForEach iteration: re-run downstream nodes per item
            elif isinstance(node, _nodes.ForEachNode) and node._iteration_list:
                downstream = self._get_downstream(node)
                for i in range(len(node._iteration_list)):
                    if not node.set_iteration_index(i):
                        break
                    for dn in downstream:
                        self._exec_node(dn)
            QApplication.processEvents()

    def run_from(self, start_node):
        """Re-execute start_node and its downstream nodes only."""
        widget = self._widget
        if not widget._stage:
            return

        up_visited: set = set()
        up_order: list = []

        def _upstream(node):
            if not hasattr(node, 'input_ports'):
                return
            nid = node.id
            if nid in up_visited:
                return
            up_visited.add(nid)
            for port in node.input_ports():
                for cp in port.connected_ports():
                    _upstream(cp.node())
            up_order.append(node)

        visited: set = set()
        order: list = []

        def _downstream(node):
            if not hasattr(node, 'output_ports'):
                return
            nid = node.id
            if nid in visited:
                return
            visited.add(nid)
            order.append(node)
            for port in node.output_ports():
                for cp in port.connected_ports():
                    _downstream(cp.node())

        _upstream(start_node)
        _downstream(start_node)
        full_order = up_order + [n for n in order if n.id not in up_visited]

        errored: set = set()
        for node in full_order:
            upstream_errored = any(
                cp.node().id in errored
                for port in (node.input_ports() if hasattr(node, 'input_ports') else [])
                for cp in port.connected_ports()
            )
            if upstream_errored:
                widget._set_exec_state(node, "skipped")
                errored.add(node.id)
                node._output_cache = {}
                continue
            if self._exec_node(node) is False:
                errored.add(node.id)
        QApplication.processEvents()

    def mark_dirty(self, node=None):
        """Schedule (debounced) re-execution from node.

        Called by property-change handlers and trigger nodes.  Multiple rapid
        calls coalesce into a single run 150 ms after the last call.
        """
        self._debounce_node = node
        self._debounce_timer.start()  # restart resets the interval

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _flush_debounce(self):
        node = self._debounce_node
        self._debounce_node = None
        if node is not None:
            self.run_from(node)
        else:
            self.run_full()

    def _exec_node(self, node):
        """Execute a single node — handles both UsdNodeBase and GroupNode."""
        if isinstance(node, GroupNode):
            self._exec_group(node)
            return
        if not isinstance(node, _nodes.UsdNodeBase):
            return
        widget = self._widget
        if node.disabled():
            node._output_cache = {}
            for out_port in node.output_ports():
                in_port = next(
                    (p for p in node.input_ports() if p.name() == out_port.name()), None
                )
                if in_port and in_port.connected_ports():
                    src_port = in_port.connected_ports()[0]
                    src_node = src_port.node()
                    if hasattr(src_node, "_output_cache"):
                        node._output_cache[out_port.name()] = src_node._output_cache.get(
                            src_port.name()
                        )
                if out_port.name() not in node._output_cache and node.input_ports():
                    first_in = node.input_ports()[0]
                    if first_in.connected_ports():
                        src_port = first_in.connected_ports()[0]
                        src_node = src_port.node()
                        if hasattr(src_node, "_output_cache"):
                            node._output_cache[out_port.name()] = src_node._output_cache.get(
                                src_port.name()
                            )
            return
        widget._set_exec_state(node, "running")
        try:
            result = node.execute(widget._stage)
            node._output_cache = result or {}
            vals = list((result or {}).values())
            all_null = vals and all(v is None for v in vals)
            widget._set_exec_state(node, "none" if all_null else "valid")
            # Track chunk layer so we can remove it from the stage on node deletion.
            try:
                chunk = getattr(node, '_my_chunk', None)
                if chunk is not None:
                    layer = chunk.get_edit_context()
                    if layer:
                        self._node_layer_registry[node.id] = layer.identifier
                elif result and result.get("layer") is not None:
                    self._node_layer_registry[node.id] = result["layer"].identifier
            except Exception:
                pass
            # Capture thumbnail only when the user has set a real prim path
            # (skip pseudo-root "/" which is valid but meaningless as a thumbnail)
            if (isinstance(node, _nodes.PrimPathNode)
                    and widget._prim_thumbs_enabled
                    and result and result.get("prim")
                    and not node._prim_thumb._pixmap
                    and str(result["prim"].GetPath()) not in ("", "/")):
                widget._capture_prim_thumb(node, result["prim"])
            return True
        except Exception as e:
            print(f"[NodeGraph] {node.name()} error: {e}")
            node._output_cache = {}
            widget._set_exec_state(node, "error", str(e)[:120])
            return False

    def _exec_group(self, group):
        """Execute a GroupNode by running its inner sub-graph."""
        from NodeGraphQt.nodes.port_node import PortInputNode, PortOutputNode
        widget = self._widget
        sub = widget._graph.expand_group_node(group)
        if not sub:
            return

        for n in sub.all_nodes():
            if getattr(n, '_folded', False):
                for w in n.view._widgets.values():
                    w.setVisible(False)

        for pin in sub.get_input_port_nodes():
            parent_port = pin.parent_port
            if parent_port:
                connected = parent_port.connected_ports()
                if connected:
                    src_port = connected[0]
                    src_node = src_port.node()
                    val = (
                        src_node._output_cache.get(src_port.name())
                        if hasattr(src_node, "_output_cache") else None
                    )
                    pin._output_cache = (
                        {pin.output_ports()[0].name(): val} if pin.output_ports() else {}
                    )

        # Apply promoted input param values to inner nodes before execution
        inner_nodes_by_id = {n.id: n for n in sub.all_nodes()}
        for port_name, info in _get_promotions(group).items():
            inner_node_id = info.get("inner_node_id")
            inner_param = info.get("inner_param")
            val = info.get("default_value")
            grp_port = group.get_input(port_name)
            if grp_port:
                connected = grp_port.connected_ports()
                if connected:
                    src_port = connected[0]
                    src_node = src_port.node()
                    ext_val = (
                        src_node._output_cache.get(src_port.name())
                        if hasattr(src_node, "_output_cache") else None
                    )
                    if ext_val is not None:
                        val = ext_val
            if val is not None and inner_node_id and inner_param:
                inner_node = inner_nodes_by_id.get(inner_node_id)
                if inner_node:
                    self._apply_promoted_value(inner_node, inner_param, val)

        visited: set = set()
        order: list = []

        def _topo(n):
            nid = n.id
            if nid in visited:
                return
            visited.add(nid)
            for p in n.input_ports():
                for cp in p.connected_ports():
                    _topo(cp.node())
            order.append(n)

        for n in sub.all_nodes():
            _topo(n)

        for idx, n in enumerate(order):
            if isinstance(n, (PortInputNode, PortOutputNode)):
                continue
            self._exec_node(n)
            if isinstance(n, _nodes.ForEachNode) and n._iteration_list:
                downstream_inner = [
                    x for x in order[idx + 1:]
                    if not isinstance(x, (PortInputNode, PortOutputNode))
                ]
                for i in range(len(n._iteration_list)):
                    if not n.set_iteration_index(i):
                        break
                    for dn in downstream_inner:
                        self._exec_node(dn)

        group._output_cache = {}
        for pout in sub.get_output_port_nodes():
            parent_port = pout.parent_port
            if parent_port and pout.input_ports():
                in_p = pout.input_ports()[0]
                connected = in_p.connected_ports()
                if connected:
                    src_port = connected[0]
                    src_node = src_port.node()
                    val = (
                        src_node._output_cache.get(src_port.name())
                        if hasattr(src_node, "_output_cache") else None
                    )
                    group._output_cache[parent_port.name()] = val

        # Collect promoted output values from inner nodes directly
        inner_nodes_by_id = {n.id: n for n in sub.all_nodes()}
        for port_name, info in _get_promoted_outputs(group).items():
            inner_node_id = info.get("inner_node_id")
            inner_port = info.get("inner_port")
            inner_node = inner_nodes_by_id.get(inner_node_id)
            if inner_node and inner_port:
                val = inner_node._output_cache.get(inner_port) if hasattr(inner_node, "_output_cache") else None
                if val is not None:
                    group._output_cache[port_name] = val

        for n in sub.all_nodes():
            if isinstance(n, _nodes.UsdNodeBase):
                n.set_color(*_nodes.CATEGORY_COLORS.get(n.CATEGORY, (80, 80, 80)))

        widget._graph.collapse_group_node(group)
        widget._set_exec_state(group, "valid")

    @staticmethod
    def _apply_promoted_value(inner_node, param_name, value):
        """Push a promoted param value into an inner node (chunk or regular)."""
        try:
            adapter = getattr(inner_node, "_cached_adapter", None)
            if adapter and getattr(adapter, "chunk", None):
                chunk_param = adapter.chunk.get_param(param_name)
                if chunk_param is not None:
                    adapter.set_param(param_name, value)
                    return
        except Exception:
            pass
        try:
            inner_node.set_property(param_name, str(value) if not isinstance(value, bool) else value)
        except Exception:
            pass

    def _get_downstream(self, start_node):
        """Collect downstream nodes in topological order from start_node."""
        visited: set = set()
        order: list = []

        def _walk(node):
            nid = node.id
            if nid in visited:
                return
            visited.add(nid)
            order.append(node)
            for port in node.output_ports():
                for cp in port.connected_ports():
                    _walk(cp.node())

        for port in start_node.output_ports():
            for cp in port.connected_ports():
                _walk(cp.node())
        return order
