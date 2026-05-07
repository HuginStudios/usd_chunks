"""
GraphExecutor — owns topological sort, dirty tracking, and the debounce timer.

NodeGraphWidget holds one instance and delegates all execution calls here.
Only PySide6.QtCore.QTimer and PySide6.QtWidgets.QApplication are imported
from Qt (no widget classes).
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from NodeGraphQt import GroupNode
from NodeGraphQt.nodes.port_node import PortInputNode, PortOutputNode
from . import nodes as _nodes
from .nodes._base import _get_promotions, _get_promoted_outputs


class GraphExecutor:
    """Centralises node execution: topo-sort, debounce, dirty propagation.

    The widget reference is used only for:
    - widget._stage          (read-only)
    - widget._graph          (read-only)
    - widget._set_exec_state (state badge callback)
    """

    def __init__(self, widget):
        self._widget = widget
        # node.id → layer_identifier; populated on execution, consumed on deletion
        self._node_layer_registry: dict = {}
        # Guard: True while run_full/run_from is active — event nodes check this
        # to avoid re-triggering when their own downstream writes back to the stage.
        self._is_executing = False
        # Debounce: property changes collapse into a single run 150 ms after last change
        self._debounce_timer = QTimer()
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(150)
        self._debounce_node = None
        self._debounce_timer.timeout.connect(self._flush_debounce)

    # ── Public entry points ──────────────────────────────────────────────────

    def run_full(self) -> None:
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

        self._is_executing = True
        try:
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
        finally:
            self._is_executing = False

    def run_from(self, start_node) -> None:
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

        self._is_executing = True
        try:
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
        finally:
            self._is_executing = False

    def mark_dirty(self, node=None) -> None:
        """Schedule (debounced) re-execution from node.

        Called by property-change handlers and trigger nodes.  Multiple rapid
        calls coalesce into a single run 150 ms after the last call.
        Dropped silently while an execution is in progress — prevents event
        nodes from re-triggering the graph inside their own downstream writes.
        """
        if self._is_executing:
            return
        self._debounce_node = node
        self._debounce_timer.start()  # restart resets the interval

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _flush_debounce(self) -> None:
        node = self._debounce_node
        self._debounce_node = None
        if node is not None:
            self.run_from(node)
        else:
            self.run_full()

    def _exec_node(self, node) -> bool | None:
        """Execute a single node — handles both UsdNodeBase and GroupNode.

        Returns True on success, False on error, None for GroupNode / disabled pass-through.
        """
        if isinstance(node, GroupNode):
            self._exec_group(node)
            return
        if not isinstance(node, _nodes.UsdNodeBase):
            return
        # Give every node a callback so it can request re-execution without
        # knowing the executor or widget exist.  The lambda captures by name so
        # mark_dirty's own _is_executing guard still applies at call time.
        node._dirty_callback = lambda n=node: self.mark_dirty(n)
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
            return True
        except Exception as e:
            print(f"[NodeGraph] {node.name()} error: {e}")
            node._output_cache = {}
            widget._set_exec_state(node, "error", str(e)[:120])
            return False

    def _exec_group(self, group) -> None:
        """Execute a GroupNode by running its inner sub-graph."""
        widget = self._widget
        sub = widget._graph.expand_group_node(group)
        if not sub:
            return

        for n in sub.all_nodes():
            if getattr(n, '_folded', False):
                for w in n.view._widgets.values():
                    w.setVisible(False)

        # Restore persisted chunk instances from the group node so we reuse
        # the same chunk (and its layer) across expand/collapse cycles.
        chunk_cache = getattr(group, '_inner_chunk_cache', None)
        if chunk_cache is None:
            chunk_cache = {}
            group._inner_chunk_cache = chunk_cache
        for n in sub.all_nodes():
            if hasattr(n, '_ensure_chunk') and n.id in chunk_cache:
                n._my_chunk = chunk_cache[n.id]

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
        inner_nodes_by_name = {n.name(): n for n in sub.all_nodes()}
        promotions = _get_promotions(group)
        for port_name, info in promotions.items():
            inner_node_id = info.get("inner_node_id")
            inner_node_name = info.get("inner_node_name")
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
            if val is not None and inner_param:
                inner_node = inner_nodes_by_id.get(inner_node_id)
                # Fallback: find by name if ID doesn't match (nodes get new IDs on re-expand)
                if inner_node is None:
                    if inner_node_name:
                        inner_node = inner_nodes_by_name.get(inner_node_name)
                    # Last resort: find any inner node that has this property
                    if inner_node is None:
                        for _n in sub.all_nodes():
                            try:
                                if _n.has_property(inner_param):
                                    inner_node = _n
                                    break
                            except Exception:
                                continue
                    if inner_node:
                        info["inner_node_id"] = inner_node.id
                        info["inner_node_name"] = inner_node.name()
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

        # Persist chunk instances on the group node for reuse on next execution
        for n in sub.all_nodes():
            my_chunk = getattr(n, '_my_chunk', None)
            if my_chunk is not None:
                chunk_cache[n.id] = my_chunk

        widget._graph.collapse_group_node(group)
        widget._set_exec_state(group, "valid")

    @staticmethod
    def _apply_promoted_value(inner_node, param_name, value):
        """Write a promoted value to the inner node's property store."""
        try:
            str_val = str(value) if not isinstance(value, bool) else value
            inner_node.set_property(param_name, str_val)
        except Exception:
            pass

    def _get_downstream(self, start_node) -> list:
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
