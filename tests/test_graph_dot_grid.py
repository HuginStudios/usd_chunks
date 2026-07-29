from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView


def test_grid_points_snap_to_spacing_lines_for_a_positive_rect() -> None:
    rect = QtCore.QRectF(12, 23, 96, 47)  # right=108, bottom=70
    points = ChunkGraphView._grid_points_in_rect(rect, 50)

    xs = sorted({p.x() for p in points})
    ys = sorted({p.y() for p in points})
    assert xs == [0, 50, 100]
    assert ys == [0, 50]
    assert len(points) == len(xs) * len(ys)


def test_grid_points_snap_correctly_on_the_negative_half_of_the_scene() -> None:
    """Python's `%` on a negative operand returns a non-negative remainder (floor-division
    semantics), which is exactly what a "snap down to the nearest grid line" needs — confirm
    it holds for coordinates on the negative side of the scene's huge (-50000, 50000) extent."""
    rect = QtCore.QRectF(-77, -130, 40, 60)  # right=-37, bottom=-70

    points = ChunkGraphView._grid_points_in_rect(rect, 50)

    xs = sorted({p.x() for p in points})
    ys = sorted({p.y() for p in points})
    assert xs == [-100, -50]
    assert ys == [-150, -100]


def test_grid_points_always_cover_a_rect_smaller_than_the_spacing() -> None:
    """The snap-down design guarantees at least the grid line at-or-before the rect's corner
    is included (left <= rect.left() < rect.right() always holds), so even a rect entirely
    within a single grid cell still gets exactly one covering dot, not zero."""
    rect = QtCore.QRectF(5, 5, 3, 3)  # right=8, bottom=8 — inside the [0, 50) cell
    points = ChunkGraphView._grid_points_in_rect(rect, 50)
    assert points == [QtCore.QPointF(0, 0)]


def test_grid_points_scale_count_with_rect_size_not_absolute_position() -> None:
    """Same-sized rect far from the origin should produce the same point count as one near
    it — spacing is fixed in scene space, not dependent on where in the huge scene you are."""
    near_origin = ChunkGraphView._grid_points_in_rect(QtCore.QRectF(0, 0, 200, 200), 50)
    far_away = ChunkGraphView._grid_points_in_rect(QtCore.QRectF(40000, -40000, 200, 200), 50)
    assert len(near_origin) == len(far_away)


def test_draw_background_renders_dots_without_crashing(qapp) -> None:
    """End-to-end smoke test: build a real ChunkGraphView, render it to an offscreen image,
    and confirm pixels other than the flat background color and app chrome actually appear —
    i.e. the grid is really painted, not just mathematically computed."""
    gv = ChunkGraphView()
    gv.resize(400, 300)
    gv.show()
    QtWidgets.QApplication.instance().processEvents()

    img = QtGui.QImage(400, 300, QtGui.QImage.Format.Format_RGB32)
    painter = QtGui.QPainter(img)
    gv.render(painter)
    painter.end()

    bg = QtGui.QColor("#252525").rgb()
    non_bg_pixels = sum(
        1
        for x in range(0, 400, 2)
        for y in range(50, 280, 2)  # avoid the menubar strip and bottom badge area
        if img.pixel(x, y) != bg
    )
    assert non_bg_pixels > 0
