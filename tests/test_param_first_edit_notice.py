"""A param's first edit writes its attribute and value in one stage change.

It used to make the attribute, then set it: two changes, the first with an attribute that
held nothing. Anything listening to the stage saw that half-made state. Once, in usdview,
the set that followed raised "Accessed invalid attribute ... on expired 'Chunk' prim".
"""
from __future__ import annotations

from pxr import Sdf, Tf, Usd

from usd_chunks import Scene, chunk_stage


def _width_path(chunk):
    return Sdf.Path(chunk_stage.get_chunk_prim_path(chunk)).AppendProperty("inputs:cube:width")


def test_the_first_change_already_holds_the_value():
    with Scene.new() as scene:
        cube = scene.create.cube()
        stage = scene.get_stage()
        path = _width_path(cube)
        assert not stage.GetAttributeAtPath(path)
        values = []

        def observe(notice, sender):
            if path in notice.GetResyncedPaths() or path in notice.GetChangedInfoOnlyPaths():
                attr = sender.GetAttributeAtPath(path)
                values.append(attr.Get() if attr else None)

        key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, observe, stage)
        try:
            cube.get_param("width").value = 4.0
        finally:
            key.Revoke()

        assert values and values[0] == 4.0
        assert stage.GetAttributeAtPath(path).Get() == 4.0

