"""Nodes buried under an exactly-overlapping node don't draw their bodies.

Chunks created without an authored ui:nodegraph:node:pos all land on the same spot, so a
network built programmatically arrives as one pile. Qt does no occlusion culling of its own,
so without this it paints every body in that pile: 57 stacked nodes measured 141.6ms a frame
against 8.7ms with only the topmost body live.
"""
from PySide6 import QtCore

from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView


class _FakeNode:
    def __init__(self, x, y, z=0.0):
        self._pos = QtCore.QPointF(x, y)
        self._z = z

    def pos(self):
        return self._pos

    def zValue(self):
        return self._z


buried = ChunkGraphView._buried_nodes


def test_nothing_buried_when_every_node_has_its_own_place():
    nodes = [_FakeNode(x * 400, 0) for x in range(6)]
    assert buried(nodes) == frozenset()


def test_a_pile_leaves_only_the_topmost():
    nodes = [_FakeNode(0, 0, z) for z in range(5)]
    result = buried(nodes)
    assert len(result) == 4
    assert nodes[-1] not in result, "the topmost node must keep its body"
    assert all(n in result for n in nodes[:-1])


def test_separate_piles_each_keep_their_own_top():
    pile_a = [_FakeNode(0, 0, z) for z in range(3)]
    pile_b = [_FakeNode(900, 250, z) for z in range(4)]
    loner = _FakeNode(-500, 80)
    result = buried(pile_a + pile_b + [loner])

    assert loner not in result
    assert pile_a[-1] not in result
    assert pile_b[-1] not in result
    assert len(result) == 5  # 2 from A, 3 from B


def test_near_misses_are_not_treated_as_stacked():
    """Only exact overlaps. A node merely close to another is still visible, and blanking
    one the user can see is far worse than painting one they cannot."""
    nodes = [_FakeNode(0, 0), _FakeNode(0, 40), _FakeNode(30, 0)]
    assert buried(nodes) == frozenset()


def test_positions_are_compared_at_display_precision():
    """Float drift must not read as two separate positions."""
    nodes = [_FakeNode(0.0, 0.0, 0), _FakeNode(0.00001, 0.00001, 1)]
    assert len(buried(nodes)) == 1


def test_empty_graph_is_fine():
    assert buried([]) == frozenset()
