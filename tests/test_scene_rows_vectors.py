"""A list param's rows set from a script give a vector in the row its values.

A vector inside a list row is a group of its components, whose own value is None; a tuple given
for it was assigned to the group and silently dropped, leaving the components at their defaults.
"""

from __future__ import annotations

from usd_chunks import Scene, chunk


def _row_chunk():
    c = chunk.Chunk("RowVectors")
    rows = chunk.ListGroup("rows", "Rows")
    rows.add_params([
        chunk.LineEdit("label", "Label"),
        chunk.Vector("point", "Point", param=chunk.Slider(), length=3, suffixes="XYZ", default_value=(0.0, 0.0, 0.0)),
    ])
    c.add_param(rows)
    return c


def _points(node):
    rows = node.get_param("rows", chunk.is_inst(chunk.ListGroup))
    vec = rows.get_param("point")
    return [tuple(k.value for k in rows.get_indexed_param(vec, i).get_my_params()) for i in range(rows.size)]


def test_a_tuple_reaches_the_vectors_components() -> None:
    with Scene() as scene:
        node = scene.place(_row_chunk(), rows=[{"label": "a", "point": (1.0, 2.0, 3.0)},
                                               {"label": "b", "point": (4.0, 5.0, 6.0)}])
        assert _points(node) == [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]


def test_a_row_without_the_vector_gets_its_default_back() -> None:
    with Scene() as scene:
        node = scene.place(_row_chunk(), rows=[{"label": "a", "point": (1.0, 2.0, 3.0)}])
        scene.set(node, rows=[{"label": "a"}])
        assert _points(node) == [(0.0, 0.0, 0.0)]
