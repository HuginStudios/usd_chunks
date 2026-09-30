"""scene.export() leaves no links to the Chunk prims it removes.

A Camera chunk connects the camera's focalLength to its own param. Export removed the
Chunk prims but kept that `.connect`, so the "plain USD" file pointed at /chunks/... prims
it did not have. Links to prims that stay, like a material's shader wires, are kept.
"""

from __future__ import annotations

from pxr import Sdf, Usd

from usd_chunks import Scene, chunk_naming


def _links(layer):
    found = {}

    def visit(path):
        if not path.IsPropertyPath():
            return
        prop = layer.GetPropertyAtPath(path)
        links = prop.connectionPathList if isinstance(prop, Sdf.AttributeSpec) else prop.targetPathList
        if links.isExplicit or links.GetAddedOrExplicitItems():
            found[str(path)] = [str(t) for t in links.GetAddedOrExplicitItems()]

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return found


def test_export_drops_links_into_removed_chunks(tmp_path) -> None:
    out = tmp_path / "result.usda"
    with Scene() as scene:
        ground = scene.create.ground(prim_path="/set/ground")   # brings a material
        scene.create.camera(ground, primpath_location="/set/cam", focalLength=35.0)
        scene.export(str(out))

    layer = Sdf.Layer.FindOrOpen(str(out))
    links = _links(layer)
    root = chunk_naming.CHUNKS_ROOT
    assert not [p for p, targets in links.items() if any(t.startswith(root) for t in targets)]
    assert all(targets for targets in links.values()), "no empty `.connect = None` left behind"
    assert any("/materials/" in t for targets in links.values() for t in targets), \
        "links to prims that stay are kept"

    stage = Usd.Stage.Open(layer)
    assert stage.GetPrimAtPath("/set/cam").GetAttribute("focalLength").Get() == 35.0
