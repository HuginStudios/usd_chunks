"""The Export USD chunk's baked layer export leaves no trace of the chunk graph.

It stripped the Chunk prims but left the empty `over "chunks"` / `over "ChunkGroup"`
shells, and left shader inputs connected to chunk params that were no longer in the file.
scene.export() already cleaned both; this checks the Export USD chunk does too.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene, chunk, chunk_naming


def _export_layer(scene, after, layer_name, out_dir):
    exporter = scene.create.export_usd(after, output_dir=str(out_dir), keep_session_layer=False)
    rows = exporter.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
    for i in range(rows.size):
        keep = rows.get_indexed_param(rows.get_param("layer_name"), i).value == layer_name
        rows.get_indexed_param(rows.get_param("export_enabled"), i).value = keep
        rows.get_indexed_param(rows.get_param("bake"), i).value = keep
    return exporter


def _all_links(layer):
    found = []

    def visit(path):
        if path.IsPropertyPath():
            prop = layer.GetPropertyAtPath(path)
            links = prop.connectionPathList if isinstance(prop, Sdf.AttributeSpec) else prop.targetPathList
            found.extend(links.GetAddedOrExplicitItems())

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return found


def test_baked_layer_export_has_no_chunk_graph(tmp_path) -> None:
    with Scene() as scene:
        layer = scene.create.layer(layer_name="look")
        target = scene.create.edit_target(layer, edit_target="look")
        with target:
            mat = scene.create.preview_surface(mat_path="/look/mat", roughness=0.7)
        _export_layer(scene, mat, "look", tmp_path)
        scene.commit()

    out = Sdf.Layer.FindOrOpen(str(tmp_path / "look.usda"))
    assert out.GetPrimAtPath("/look/mat"), "the material itself is exported"
    assert not out.GetPrimAtPath(chunk_naming.CHUNKS_ROOT), "no empty over left where the chunks were"
    assert not [t for t in _all_links(out) if t.HasPrefix(Sdf.Path(chunk_naming.CHUNKS_ROOT))]
