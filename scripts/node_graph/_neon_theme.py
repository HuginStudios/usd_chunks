"""Dark neon theme installer for NodeGraphQt — monkey-patches NodeItem, PipeItem, NodeViewer."""
from PySide6 import QtCore, QtGui


def install_neon_theme(graph_widget):
    """Apply dark neon visual theme and exec-state painting to the graph.

    Monkey-patches NodeItem._paint_horizontal, NodeItem.__init__, hover/click events,
    PipeItem.paint, and NodeViewer._validate_accept_connection. Sets up the pulse animation
    timer stored as graph_widget._exec_pulse_timer.
    """
    import math as _math
    from NodeGraphQt.qgraphics.node_base import NodeItem
    from NodeGraphQt.qgraphics.pipe import PipeItem
    from NodeGraphQt.widgets.viewer import NodeViewer
    from NodeGraphQt.constants import NodeEnum, PortEnum
    from .nodes import CATEGORY_COLORS

    # Guard against NodeGraphQt bug where auto_switch_mode is called with no viewer
    _orig_auto_switch = NodeItem.auto_switch_mode
    def _safe_auto_switch(self):
        if self.viewer() is None:
            return
        _orig_auto_switch(self)
    NodeItem.auto_switch_mode = _safe_auto_switch

    graph = graph_widget._graph

    graph.set_background_color(8, 9, 12)
    graph.set_grid_color(30, 36, 56)
    graph.set_grid_mode(1)
    graph.set_pipe_style(1)
    graph.set_pipe_slicing(True)

    NodeEnum.SELECTED_COLOR._value_ = (255, 255, 255, 12)
    NodeEnum.SELECTED_BORDER_COLOR._value_ = (50, 210, 220, 255)
    PortEnum.COLOR._value_ = (30, 80, 70, 255)
    PortEnum.BORDER_COLOR._value_ = (50, 210, 220, 255)
    PortEnum.ACTIVE_COLOR._value_ = (14, 35, 55, 255)
    PortEnum.ACTIVE_BORDER_COLOR._value_ = (100, 200, 230, 255)
    PortEnum.HOVER_COLOR._value_ = (20, 50, 80, 255)
    PortEnum.HOVER_BORDER_COLOR._value_ = (50, 230, 180, 255)

    _CAT_ACCENTS = {
        "Input":       (60, 210, 120),
        "USD":         (50, 160, 230),
        "Create":      (60, 200, 100),
        "Transform":   (60, 200, 140),
        "Hierarchy":   (160, 100, 220),
        "Composition": (200, 160, 60),
        "Material":    (220, 80, 160),
        "Lighting":    (240, 200, 60),
        "Camera":      (100, 170, 220),
        "Animation":   (230, 170, 50),
        "Utility":     (140, 150, 170),
        "Math":        (50, 210, 220),
        "Code":        (140, 140, 160),
        "Chunks":      (80, 170, 210),
        "Debug":       (220, 80, 80),
        "Event":       (200, 80, 220),
    }
    _COLOR_TO_CAT = {}
    for cat_name, dark in CATEGORY_COLORS.items():
        _COLOR_TO_CAT[dark] = (cat_name, _CAT_ACCENTS.get(cat_name, (140, 150, 170)))

    _pulse = [0.0]
    _STATE_COLORS = {
        "running": (220, 170, 40),
        "valid":   (50, 200, 100),
        "none":    (100, 120, 150),
        "error":   (220, 60, 60),
        "skipped": (70, 80, 110),
    }

    def _neon_paint(self_node, painter, option, widget):
        painter.save()
        try:
            _neon_paint_inner(self_node, painter, option, widget)
        except Exception:
            pass
        painter.restore()

    def _neon_paint_inner(self_node, painter, option, widget):
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtCore.Qt.NoBrush)

        margin = 1.0
        rect = self_node.boundingRect()
        rect = QtCore.QRectF(rect.left() + margin, rect.top() + margin,
                             rect.width() - margin * 2, rect.height() - margin * 2)
        radius = 10.0
        is_hover = getattr(self_node, '_is_hovered', False)
        is_sel = self_node.selected
        r, g, b = self_node.color[:3]

        cat_name, accent = _COLOR_TO_CAT.get((r, g, b), ("", (140, 150, 170)))
        ar, ag, ab = accent

        exec_state = getattr(self_node, '_exec_state', '')
        sr, sg, sb = _STATE_COLORS.get(exec_state, (0, 0, 0))
        phase = _pulse[0]

        if exec_state == 'none':
            painter.setOpacity(0.7)

        if is_sel or is_hover:
            glow_alpha = 18 if is_sel else 10
            for i in range(4):
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.setPen(QtGui.QPen(QtGui.QColor(ar, ag, ab, glow_alpha - i * 4),
                                          4 + i * 3))
                painter.drawRoundedRect(rect.adjusted(-i*3, -i*3, i*3, i*3),
                                        radius + i*2, radius + i*2)
            painter.setPen(QtCore.Qt.NoPen)

        if is_sel:
            for i in range(3):
                painter.setPen(QtGui.QPen(QtGui.QColor(ar, ag, ab, 22 - i * 6), 2.5 + i * 2))
                painter.drawRoundedRect(rect.adjusted(-i*2, -i*2, i*2, i*2),
                                        radius + i, radius + i)
            painter.setPen(QtCore.Qt.NoPen)

        if exec_state == 'valid':
            painter.setPen(QtGui.QPen(QtGui.QColor(sr, sg, sb, 50), 1.5))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), radius, radius)
            painter.setPen(QtCore.Qt.NoPen)
        elif exec_state == 'error':
            painter.setPen(QtGui.QPen(QtGui.QColor(sr, sg, sb, 70), 1.5))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), radius, radius)
            painter.setPen(QtCore.Qt.NoPen)
        elif exec_state == 'running':
            pulse_a = int(50 + 60 * (0.5 + 0.5 * _math.sin(phase * 5)))
            painter.setPen(QtGui.QPen(QtGui.QColor(sr, sg, sb, pulse_a), 2.5))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), radius, radius)
            painter.setPen(QtCore.Qt.NoPen)

        painter.setBrush(QtGui.QColor(14, 16, 22))
        painter.drawRoundedRect(rect, radius, radius)

        header_h = 28.0
        header_rect = QtCore.QRectF(rect.x(), rect.y(), rect.width(), header_h)
        grad = QtGui.QLinearGradient(header_rect.topLeft(), header_rect.topRight())
        grad.setColorAt(0.0, QtGui.QColor(ar, ag, ab, 25))
        grad.setColorAt(0.7, QtGui.QColor(ar, ag, ab, 5))
        grad.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        painter.save()
        painter.setBrush(grad)
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)
        painter.drawRect(header_rect)
        painter.restore()

        stripe_margin = 10.0
        stripe_w = 4.0 if exec_state == 'running' else 2.5
        stripe_rect = QtCore.QRectF(rect.x() + 1, rect.y() + stripe_margin,
                                    stripe_w, rect.height() - stripe_margin * 2)
        if exec_state == 'running':
            pulse_a = int(140 + 115 * (0.5 + 0.5 * _math.sin(phase * 5)))
            painter.setBrush(QtGui.QColor(sr, sg, sb, pulse_a))
        elif exec_state in _STATE_COLORS:
            painter.setBrush(QtGui.QColor(sr, sg, sb, 180))
        else:
            painter.setBrush(QtGui.QColor(ar, ag, ab, 180))
        painter.drawRoundedRect(stripe_rect, 1.2, 1.2)

        badge_font = QtGui.QFont("Consolas", 7)
        badge_font.setBold(True)
        fm = QtGui.QFontMetrics(badge_font)
        if exec_state == 'valid':
            badge_text, br, bg, bb = "✓ OK", sr, sg, sb
        elif exec_state == 'none':
            badge_text, br, bg, bb = "∅ NONE", sr, sg, sb
        elif exec_state == 'error':
            badge_text, br, bg, bb = "! ERR", sr, sg, sb
        elif exec_state == 'running':
            badge_text, br, bg, bb = "⟳ RUN", sr, sg, sb
        elif exec_state == 'skipped':
            badge_text, br, bg, bb = "⊘ SKIP", sr, sg, sb
        elif cat_name:
            badge_text, br, bg, bb = cat_name.upper(), ar, ag, ab
        else:
            badge_text = None
        if badge_text:
            tw = fm.horizontalAdvance(badge_text) + 10
            badge_rect = QtCore.QRectF(rect.right() - tw - 6, rect.y() + 5, tw, 16)
            painter.setBrush(QtGui.QColor(br, bg, bb, 30))
            painter.setPen(QtGui.QPen(QtGui.QColor(br, bg, bb, 80), 0.8))
            painter.drawRoundedRect(badge_rect, 4, 4)
            painter.setFont(badge_font)
            painter.setPen(QtGui.QColor(br, bg, bb, 200))
            painter.drawText(badge_rect, QtCore.Qt.AlignCenter, badge_text)

        painter.setPen(QtCore.Qt.NoPen)
        padding = 3.0, 2.0
        text_rect = self_node._text_item.boundingRect()
        text_bg = QtCore.QRectF(text_rect.x() + padding[0], rect.y() + padding[1],
                                rect.width() * 0.55,
                                text_rect.height() - padding[1] * 2)
        painter.setBrush(QtGui.QColor(0, 0, 0, 40))
        painter.drawRoundedRect(text_bg, 3.0, 3.0)

        type_name = getattr(self_node, '_node_type_name', None)
        if type_name:
            type_font = QtGui.QFont("Consolas", 6)
            type_font.setItalic(True)
            painter.setFont(type_font)
            painter.setPen(QtGui.QColor(ar, ag, ab, 70))
            tr = QtCore.QRectF(rect.x() + 12, rect.y() + header_h - 10,
                               rect.width() * 0.55, 10)
            painter.drawText(tr, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, type_name)

        if hasattr(self_node, '_folded_state'):
            folded = self_node._folded_state
            ix = rect.x() + 8
            iy = rect.y() + 8
            tri = QtGui.QPainterPath()
            if folded:
                tri.moveTo(ix, iy)
                tri.lineTo(ix + 8, iy + 5)
                tri.lineTo(ix, iy + 10)
            else:
                tri.moveTo(ix, iy)
                tri.lineTo(ix + 10, iy)
                tri.lineTo(ix + 5, iy + 8)
            tri.closeSubpath()
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(ar, ag, ab, 180 if is_hover else 120))
            painter.drawPath(tri)
            try:
                _node = graph.get_node_by_id(self_node.id)
                if _node:
                    _has_hidden_wires = any(
                        not p.visible() and p.connected_ports()
                        for p in list(_node.input_ports()) + list(_node.output_ports())
                    )
                    if _has_hidden_wires:
                        painter.setBrush(QtGui.QColor(200, 80, 220, 220))
                        painter.setPen(QtCore.Qt.NoPen)
                        painter.drawEllipse(QtCore.QPointF(ix + 14, iy + 5), 3, 3)
            except Exception:
                pass

        if is_sel:
            border_color = QtGui.QColor(ar, ag, ab, 200)
            border_width = 1.5
        elif is_hover:
            border_color = QtGui.QColor(ar, ag, ab, 90)
            border_width = 1.0
        else:
            border_color = QtGui.QColor(ar, ag, ab, 22)
            border_width = 0.8
        pen = QtGui.QPen(border_color, border_width)
        pen.setCosmetic(self_node.viewer().get_zoom() < 0.0)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(pen)
        painter.drawPath(path)

        if exec_state == 'running':
            sweep = (phase * 0.8) % 1.0
            bar_w = rect.width() * 0.35
            bar_x = rect.x() + 2 + (rect.width() - 4 - bar_w) * sweep
            bar_rect = QtCore.QRectF(bar_x, rect.bottom() - 4, bar_w, 3)
            painter.setPen(QtCore.Qt.NoPen)
            bar_grad = QtGui.QLinearGradient(bar_rect.topLeft(), bar_rect.topRight())
            bar_grad.setColorAt(0.0, QtGui.QColor(sr, sg, sb, 0))
            bar_grad.setColorAt(0.3, QtGui.QColor(sr, sg, sb, 240))
            bar_grad.setColorAt(0.7, QtGui.QColor(sr, sg, sb, 240))
            bar_grad.setColorAt(1.0, QtGui.QColor(sr, sg, sb, 0))
            painter.setBrush(bar_grad)
            painter.drawRoundedRect(bar_rect, 1.5, 1.5)

        if exec_state == 'error':
            err_msg = getattr(self_node, '_exec_error', '')
            if err_msg:
                err_font = QtGui.QFont("Consolas", 6)
                painter.setFont(err_font)
                painter.setPen(QtGui.QColor(220, 60, 60, 180))
                err_rect = QtCore.QRectF(rect.x() + 6, rect.bottom() - 14,
                                         rect.width() - 12, 12)
                painter.drawText(err_rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                                 err_msg[:60])

    NodeItem._paint_horizontal = _neon_paint

    _orig_validate_accept = NodeViewer._validate_accept_connection

    def _typed_validate_accept(self_v, from_port, to_port):
        if not _orig_validate_accept(self_v, from_port, to_port):
            return False
        return True

    NodeViewer._validate_accept_connection = _typed_validate_accept

    exec_pulse_timer = QtCore.QTimer(graph_widget)

    def _tick_pulse():
        _pulse[0] += 0.08
        try:
            for n in graph.all_nodes():
                if getattr(n.view, '_exec_state', '') == 'running':
                    n.view.update()
        except Exception:
            pass

    exec_pulse_timer.timeout.connect(_tick_pulse)
    exec_pulse_timer.start(50)
    graph_widget._exec_pulse_timer = exec_pulse_timer

    _orig_node_init = NodeItem.__init__

    def _patched_node_init(self_node, *args, **kwargs):
        _orig_node_init(self_node, *args, **kwargs)
        self_node.setAcceptHoverEvents(True)
        self_node._is_hovered = False
        self_node._exec_state = ''
        self_node._exec_error = ''

    def _hover_enter(self_node, event):
        if not self_node.scene():
            return
        self_node._is_hovered = True
        self_node.update()

    def _hover_leave(self_node, event):
        if not self_node.scene():
            return
        self_node._is_hovered = False
        self_node.update()

    NodeItem.__init__ = _patched_node_init
    NodeItem.hoverEnterEvent = _hover_enter
    NodeItem.hoverLeaveEvent = _hover_leave

    _orig_mouse_press = NodeItem.mousePressEvent

    def _patched_mouse_press(self_node, event):
        if hasattr(self_node, '_folded_state') and event.button() == QtCore.Qt.LeftButton:
            pos = event.pos()
            rect = self_node.boundingRect()
            if pos.x() < rect.x() + 22 and pos.y() < rect.y() + 22:
                node = graph.get_node_by_id(self_node.id)
                if node:
                    graph_widget._toggle_chunk_fold(node)
                    event.accept()
                    return
        _orig_mouse_press(self_node, event)

    NodeItem.mousePressEvent = _patched_mouse_press

    _orig_pipe_paint = PipeItem.paint

    _AMBER = QtGui.QColor(0xd4, 0xa0, 0x17, 200)

    def _pipe_type_mismatch(self_pipe):
        try:
            out_port = getattr(self_pipe, '_output_port', None) or self_pipe.output_port
            in_port = getattr(self_pipe, '_input_port', None) or self_pipe.input_port
            if out_port is None or in_port is None:
                return False
            t_out = getattr(out_port, '_data_type', 'any')
            t_in = getattr(in_port, '_data_type', 'any')
            return t_out != 'any' and t_in != 'any' and t_out != t_in
        except Exception:
            return False

    def _state_pipe_paint(self_pipe, painter, option, widget):
        src_state = ''
        try:
            src_port = getattr(self_pipe, '_output_port', None) or self_pipe.output_port
            if src_port:
                src_view = src_port.node
                src_state = getattr(src_view, '_exec_state', '')
        except Exception:
            pass
        if src_state in ('none', 'error'):
            painter.save()
            pen = painter.pen() if painter.pen().style() != QtCore.Qt.NoPen else QtGui.QPen()
            if src_state == 'error':
                pen.setColor(QtGui.QColor(220, 60, 60, 160))
            else:
                pen.setColor(QtGui.QColor(100, 120, 150, 100))
            pen.setStyle(QtCore.Qt.DashLine)
            pen.setWidthF(1.5)
            painter.setPen(pen)
            if hasattr(self_pipe, 'path'):
                painter.drawPath(self_pipe.path())
            painter.restore()
        elif _pipe_type_mismatch(self_pipe):
            painter.save()
            pen = QtGui.QPen(_AMBER, 1.8, QtCore.Qt.SolidLine)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            painter.setPen(pen)
            if hasattr(self_pipe, 'path'):
                painter.drawPath(self_pipe.path())
            painter.restore()
        else:
            _orig_pipe_paint(self_pipe, painter, option, widget)

    PipeItem.paint = _state_pipe_paint
