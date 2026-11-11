"""One row's param of a list lofts onto a group, like any other param.

A list keeps each column in one array attribute (inputs:prototypes:proto_path), so a row
has no attribute of its own to connect, and loft_param_to_context warned "has no USD
attribute bound" and lofted nothing: a ChunkGroup could not expose which prim a Scatter
scatters, or which file a Sublayer brings in. The group's attribute now records the row
it drives, the two are kept in step, and the link comes back when the file is opened.
"""
from __future__ import annotations

from usd_chunks import Scene, chunk_stage


def _group(scene):
    group = scene.create.chunkgroup(name="G")
    with group:
        scatter = scene.create.scatter_points(
            name="Scatter", prototypes=[{"proto_path": "/p/rock"}, {"proto_path": "/p/bush"}])
        scene.set_output(scatter)
    rows = scatter.get_param("prototypes")
    chunk_stage.loft_param_to_context(rows.get_indexed_param("proto_path", 1), group)
    return group, scatter


def _row(scatter, index=1):
    return scatter.get_param("prototypes").get_indexed_param("proto_path", index).value


def test_a_row_lofts_and_drives_its_row(caplog) -> None:
    with Scene.new() as scene:
        group, scatter = _group(scene)
        lofted = group.get_param("Scatter_proto_path")
        assert lofted is not None and lofted.value == "/p/bush"
        assert "no USD attribute bound" not in caplog.text

        lofted.value = "/p/tree"
        assert _row(scatter) == "/p/tree"
        assert _row(scatter, 0) == "/p/rock", "only its own row"
        # And the other way: an edit to the row shows on the group.
        scatter.get_param("prototypes").get_indexed_param("proto_path", 1).value = "/p/fern"
        assert lofted.value == "/p/fern"


def test_lofting_a_row_twice_makes_one_param() -> None:
    with Scene.new() as scene:
        group, scatter = _group(scene)
        chunk_stage.loft_param_to_context(scatter.get_param("prototypes").get_indexed_param("proto_path", 1), group)
        assert [p.name for p in group.get_my_params()].count("Scatter_proto_path") == 1


def test_a_lofted_row_comes_back_when_the_file_is_opened(tmp_path) -> None:
    path = str(tmp_path / "net.usda")
    with Scene.new() as scene:
        group, _ = _group(scene)
        scene.set(group, Scatter_proto_path="/p/tree")
        scene.save(path)

    with Scene.open(path) as scene:
        group = next(c for c in scene.get_contexts() if c.name == "G")
        scatter = next(c for c in scene.get_chunks() if c.name == "Scatter")
        assert group.get_param("Scatter_proto_path").value == "/p/tree"
        assert _row(scatter) == "/p/tree"
        scene.set(group, Scatter_proto_path="/p/stone")
        assert _row(scatter) == "/p/stone"
