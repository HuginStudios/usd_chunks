"""The diff cooks of one solve share a copy of the session layer, brought up to date only when needed.

Bringing it up to date walks every spec in the session layer, which holds every chunk
prim's bookkeeping, so doing it before every diff cook made a cook cost the size of the
network. These check it runs only after the session layer or the copy changed, and that
what changed still reaches the cooks and the scene.
"""

from __future__ import annotations

import pytest
from chunk_stubs import register_stub_module
from pxr import Sdf

from usd_chunks import Scene, chunk, layer_reconcile, skip_cook


@pytest.fixture(autouse=True)
def _diff_cook_on(monkeypatch):
    monkeypatch.delenv("USD_CHUNKS_DIFF_COOK", raising=False)


def _add(scene, module):
    (c,) = module.load()
    scene.add_chunk(c)
    return c


def _generator(*, into_scene_session: bool = False):
    """Writes /Gen.size; and, given into_scene_session, the same size straight into the
    scene's session layer at /Gen.bonus -- a session edit made in the middle of a solve, not
    through the cook stage's copy, as the framework's own bookkeeping is."""

    def main():
        c = chunk.Chunk("Gen")
        c.add_param(chunk.Slider("size", default_value=1.0))
        return c

    def execute(node):
        size = node.get_param("size").value
        prim = node.get_stage().DefinePrim("/Gen", "Xform")
        prim.CreateAttribute("size", Sdf.ValueTypeNames.Double).Set(size)
        if into_scene_session:
            session = node.get_scene_stage().GetSessionLayer()
            spec = Sdf.CreatePrimInLayer(session, "/Gen")
            attr = session.GetAttributeAtPath("/Gen.bonus") or Sdf.AttributeSpec(spec, "bonus", Sdf.ValueTypeNames.Double)
            attr.default = size

    return register_stub_module(main=main, execute=execute)


def _reader(*, into_session: bool = False, attr_name: str = "size"):
    """Writes twice /Gen's size (or attr_name): at /Reader.size, or into the session layer at /Note."""

    def main():
        c = chunk.Chunk("Reader")
        c.add_param(chunk.LineEdit("target", default_value="/Gen",
                                   flags=chunk.ChunkFlags.ParamFlags.PRIM_SELECTION))
        return c

    def execute(node):
        stage = node.get_stage()
        source = stage.GetPrimAtPath(node.get_param("target").value)
        size = (source.GetAttribute(attr_name).Get() if source else None) or 0.0
        if into_session:
            session = stage.GetSessionLayer()
            spec = Sdf.CreatePrimInLayer(session, "/Note")
            attr = session.GetAttributeAtPath("/Note.size") or Sdf.AttributeSpec(spec, "size", Sdf.ValueTypeNames.Double)
            attr.default = size * 2
            return
        prim = stage.DefinePrim("/Reader", "Xform")
        prim.CreateAttribute("size", Sdf.ValueTypeNames.Double).Set(size * 2)

    return register_stub_module(main=main, execute=execute)


@pytest.fixture
def shared(monkeypatch):
    """How many cooks ran on the shared diff stage."""
    cooks = []
    real = skip_cook._shared_diff_stage
    monkeypatch.setattr(skip_cook, "_shared_diff_stage", lambda *args: cooks.append(1) or real(*args))
    return cooks


def _network(scene, reader, gen=None):
    gen = _add(scene, gen or _generator())
    reader = _add(scene, reader)
    reader.add_dependency(gen)
    gen.get_param("size").value = 0.5  # the first cook after wiring records the masks
    return gen


def test_an_unchanged_session_layer_is_walked_once_per_solve(monkeypatch):
    with Scene() as scene:
        with chunk.change_block():
            last = scene.create.cube(prim_path="/World/box")
            for i in range(8):
                last = scene.create.transform(last, name=f"T{i}", primpath_location="/World/box", ty=0.01)
            scene.set_output(last)
        session = scene.get_stage().GetSessionLayer()
        cooks, syncs = [], []
        real_shared = skip_cook._shared_diff_stage
        monkeypatch.setattr(skip_cook, "_shared_diff_stage", lambda *args: cooks.append(1) or real_shared(*args))
        real = layer_reconcile.reconcile

        def counting(src, dst, sublayers=None):
            if src == session:
                syncs.append(1)
            return real(src, dst, sublayers)

        monkeypatch.setattr(layer_reconcile, "reconcile", counting)

        scene.set(scene.get_chunk("T0"), ty=0.5)

        assert len(cooks) > 2, "the chain recooked on the shared diff stage"
        assert len(syncs) == 1, f"the session layer was walked {len(syncs)} times for {len(cooks)} cooks"


def test_a_session_edit_during_a_solve_reaches_the_cooks_after_it(shared) -> None:
    with Scene() as scene:
        gen = _network(scene, _reader(attr_name="bonus"), _generator(into_scene_session=True))
        stage = scene.get_stage()
        shared.clear()

        for size in (1.0, 3.0):
            gen.get_param("size").value = size
            assert stage.GetAttributeAtPath("/Reader.size").Get() == size * 2

        assert shared, "the cooks ran on the shared diff stage"


def test_what_each_cook_authors_into_the_session_layer_reaches_the_scene(shared) -> None:
    with Scene() as scene:
        gen = _network(scene, _reader(into_session=True))
        session = scene.get_stage().GetSessionLayer()
        shared.clear()

        for size in (2.0, 3.0, 3.0, 5.0):
            gen.get_param("size").value = size
            assert session.GetAttributeAtPath("/Note.size").default == size * 2

        assert shared, "the reader cooked on the shared diff stage"
