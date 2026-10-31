"""A masked cook's stage holds only the layers it sees, on a stand-in session layer.

Muting the layers a cook does not see cost a layer each, most of opening its stage in a
900-chunk scene; a stage opened on a stand-in that leaves them out costs next to nothing.
What a cook authors into the session layer there reaches the scene's.
"""

from __future__ import annotations

import pytest
from chunk_stubs import register_stub_module
from pxr import Sdf

from usd_chunks import Scene, chunk, chunk_registry, skip_cook


def _add(scene, module):
    (c,) = module.load()
    scene.add_chunk(c)
    return c


def _generator(name: str, path: str | None = None):
    """Writes <path>.size."""

    def main():
        c = chunk.Chunk(name)
        c.add_param(chunk.LineEdit("path", default_value=path or f"/{name}"))
        c.add_param(chunk.Slider("size", default_value=1.0))
        return c

    def execute(node):
        prim = node.get_stage().DefinePrim(node.get_param("path").value, "Xform")
        prim.CreateAttribute("size", Sdf.ValueTypeNames.Double).Set(node.get_param("size").value)

    return register_stub_module(main=main, execute=execute)


def _reader(name: str, *, into_session: bool = False):
    """Writes twice /Gen's size: at /<name>.size, or into the session layer at /Note."""

    def main():
        c = chunk.Chunk(name)
        c.add_param(chunk.LineEdit("target", default_value="/Gen",
                                   flags=chunk.ChunkFlags.ParamFlags.PRIM_SELECTION))
        return c

    def execute(node):
        stage = node.get_stage()
        source = stage.GetPrimAtPath(node.get_param("target").value)
        size = (source.GetAttribute("size").Get() if source else None) or 0.0
        if into_session:
            session = stage.GetSessionLayer()
            spec = Sdf.CreatePrimInLayer(session, "/Note")
            attr = session.GetAttributeAtPath("/Note.size") or Sdf.AttributeSpec(spec, "size", Sdf.ValueTypeNames.Double)
            attr.default = size * 2
            return
        prim = stage.DefinePrim(f"/{name}", "Xform")
        prim.CreateAttribute("size", Sdf.ValueTypeNames.Double).Set(size * 2)

    return register_stub_module(main=main, execute=execute)


@pytest.fixture
def stood_in(monkeypatch):
    """How many cook stages were opened on a stand-in."""
    made = []
    real = skip_cook._stand_in

    def counting(main, hidden):
        stand_in = real(main, hidden)
        if stand_in is not None:
            made.append(stand_in)
        return stand_in

    monkeypatch.setattr(skip_cook, "_stand_in", counting)
    return made


def _network(scene, reader, *, below: bool = False):
    gen = _add(scene, _generator("Gen"))
    for i in range(6):  # siblings: what the stand-in leaves out
        _add(scene, _generator(f"Sibling{i}"))
    reader = _add(scene, reader)
    reader.add_dependency(gen)
    if below:
        under = _add(scene, _generator("Below", path="/Gen"))
        under.get_param("size").value = 100.0
        under.add_dependency(reader)
    gen.get_param("size").value = 0.5  # the first cook after wiring records the masks
    return gen


def test_a_stand_in_leaves_out_the_siblings_and_keeps_what_is_below(stood_in) -> None:
    """Below stays, as muting left it: a cook sees what its first cook, on the whole stage, saw."""
    with Scene() as scene:
        gen = _network(scene, _reader("Reader"), below=True)
        stood_in.clear()

        gen.get_param("size").value = 3.0

        assert stood_in, "the reader cooked on a stand-in"
        stand_in = stood_in[-1]
        layer_of = chunk_registry.ChunkLayerManager().get_layer
        kept = {path for layer in (stand_in.layer, *stand_in.held) for path in layer.subLayerPaths}
        assert stand_in.dropped == {layer_of(c).identifier for c in scene.get_chunks() if c.name.startswith("Sibling")}
        assert {layer_of(c).identifier for c in scene.get_chunks() if c.name in ("Gen", "Reader", "Below")} <= kept


def test_what_a_cook_authors_into_the_session_layer_reaches_the_scene(stood_in) -> None:
    with Scene() as scene:
        gen = _network(scene, _reader("Noter", into_session=True))
        stood_in.clear()

        gen.get_param("size").value = 4.0

        assert stood_in, "the noter cooked on a stand-in"
        assert stood_in[-1].layer is not scene.get_stage().GetSessionLayer()
        assert scene.get_stage().GetSessionLayer().GetAttributeAtPath("/Note.size").default == 8.0
