"""Deleting a chunk from a file that was opened, then saving it.

Opening a file leaves each chunk's bookkeeping in that file's root layer (load never
rewrites the file it reads). The delete used to re-home the whole scene without that
protection, so the first delete after opening moved every remaining chunk out of the root
layer and re-cooked all of them: slow on a real network, a crash when a cook hit an
expression that referenced the chunk just deleted, and an emptied root layer that Sdf hands
back to the next open of the same path.
"""

from __future__ import annotations

from pxr import Usd

from usd_chunks import Scene, chunk_naming, chunk_scene, chunk_stage

_CHAIN = 5


def _save_chain(path):
    with Scene() as scene:
        wedge = scene.create.wedge(seed=3, count=4, ranges="x 1 9")
        upstream = wedge
        for i in range(_CHAIN):
            upstream = scene.create.transform(upstream, primpath_location=f"/World/box{i}",
                                              tx="@Wedge.x@")
        scene.save(str(path))


def _chunks_by_name(scene):
    return {c.name: c for c in chunk_scene.all_chunks(scene.get_chunks())}


def _chunk_names_on_disk(path):
    """Chunk prims in the file, less the ChunkGroup a scene saves its chunks inside."""
    stage = Usd.Stage.Open(str(path))
    return sorted(
        p.GetName() for p in stage.TraverseAll()
        if p.GetTypeName() == chunk_naming.CHUNK_PRIM_TYPENAME and p.GetName() != "ChunkGroup"
    )


def _root_layer_chunk_specs(scene):
    root = scene.get_stage().GetRootLayer()
    found = []

    def visit(path):
        spec = root.GetPrimAtPath(path)
        if spec and spec.typeName == chunk_naming.CHUNK_PRIM_TYPENAME:
            found.append(str(path))

    root.Traverse("/", visit)
    return found


def test_delete_after_open_does_not_rehome_the_rest(tmp_path, monkeypatch):
    path = tmp_path / "chain.usda"
    _save_chain(path)

    with Scene.open(str(path)) as scene:
        chunks = _chunks_by_name(scene)
        assert len(chunks) == _CHAIN + 1

        moves = []
        real = chunk_stage._migrate_chunk_bookkeeping

        def spying(stage, prim_path, *args, **kwargs):
            moved = real(stage, prim_path, *args, **kwargs)
            moves.append((prim_path, moved))
            return moved

        monkeypatch.setattr(chunk_stage, "_migrate_chunk_bookkeeping", spying)

        before = _root_layer_chunk_specs(scene)
        last = chunks[sorted(n for n in chunks if n != "Wedge")[-1]]
        last.request_delete()

        assert [p for p, moved in moves if moved] == [], "a delete re-homed chunks it did not touch"
        after = _root_layer_chunk_specs(scene)
        assert len(after) == len(before) - 1, (
            f"root layer went from {len(before)} chunk specs to {len(after)}"
        )


def test_deleting_the_source_of_expressions_does_not_raise(tmp_path):
    path = tmp_path / "chain.usda"
    _save_chain(path)

    with Scene.open(str(path)) as scene:
        chunks = _chunks_by_name(scene)
        chunks["Wedge"].request_delete()

        assert "Wedge" not in _chunks_by_name(scene)
        assert len(_chunks_by_name(scene)) == _CHAIN

        out = tmp_path / "out.usda"
        scene.save(str(out))

    assert "Wedge" not in _chunk_names_on_disk(out)
    assert len(_chunk_names_on_disk(out)) == _CHAIN


def test_deleting_a_middle_chunk_saves_the_rest(tmp_path):
    path = tmp_path / "chain.usda"
    _save_chain(path)
    names = _chunk_names_on_disk(path)
    victim = sorted(n for n in names if n != "Wedge")[_CHAIN // 2]

    with Scene.open(str(path)) as scene:
        _chunks_by_name(scene)[victim].request_delete()
        out = tmp_path / "out.usda"
        scene.save(str(out))

    assert _chunk_names_on_disk(out) == sorted(n for n in names if n != victim)


def test_reopening_the_same_path_after_a_delete_keeps_the_rest(tmp_path):
    """Sdf caches the opened layer, so an unsaved delete is what a same-process reopen
    sees -- the deleted chunk gone and every other chunk still there, not an empty layer."""
    path = tmp_path / "chain.usda"
    _save_chain(path)

    with Scene.open(str(path)) as scene:
        _chunks_by_name(scene)["Wedge"].request_delete()

    with Scene.open(str(path)) as again:
        assert len(_chunks_by_name(again)) == _CHAIN, (
            "the first delete emptied the file's root layer and the next open got it that way"
        )


def test_deleting_everything_then_saving_gives_an_empty_network(tmp_path):
    path = tmp_path / "chain.usda"
    _save_chain(path)

    with Scene.open(str(path)) as scene:
        for chunk in list(_chunks_by_name(scene).values()):
            chunk.request_delete()
        assert _chunks_by_name(scene) == {}
        out = tmp_path / "out.usda"
        scene.save(str(out))

    assert _chunk_names_on_disk(out) == []
