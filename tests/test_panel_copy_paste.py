"""The chunk panel's copy/paste (chunk_stage.copy_selected_chunks / paste_chunks).

The node graph has its own clipboard (engine.node_clipboard); this is the one the Qt panel's
Ctrl+C / Ctrl+V uses. Reported via coverage: a pasted chunk came back with every value at
its default. A fresh chunk has an inputs: attribute only for values off their default, and
paste skipped every source attribute the copy did not already have -- i.e. every edit.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender

from chunk_stubs import register_stub_module


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _main():
    c = chunk.Chunk("thing")
    c.add_param(chunk.Slider("p", default_value=1.0))
    c.add_param(chunk.Slider("n", default_value=2, is_int=True))
    c.add_param(chunk.LineEdit("s", default_value="d"))
    c.add_param(chunk.Checkbox("b", default_value=False))
    return c


@pytest.fixture()
def scene(monkeypatch):
    monkeypatch.setattr(chunk_stage, "_chunk_clipboard", [])
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)
    return scene


@pytest.fixture()
def module():
    return register_stub_module(main=_main).module_name


def _copy_paste(scene, *chunks):
    for c in chunks:
        c.set_selected(True)
    before = set(map(id, scene.get_chunks()))
    chunk_stage.copy_selected_chunks()
    chunk_stage.paste_chunks()
    return [c for c in scene.get_chunks() if id(c) not in before]


def test_a_pasted_chunk_keeps_every_edited_value(scene, module):
    (src,) = chunk.create_chunks(module)
    src.get_param("p").value = 5.0
    src.get_param("n").value = 7
    src.get_param("s").value = "edited"
    src.get_param("b").value = True

    (copy,) = _copy_paste(scene, src)

    assert copy is not src and copy.name != src.name
    assert copy.get_param("p").value == pytest.approx(5.0)
    assert copy.get_param("n").value == 7
    assert copy.get_param("s").value == "edited"
    assert copy.get_param("b").value is True


def test_a_value_left_at_its_default_stays_at_it(scene, module):
    (src,) = chunk.create_chunks(module)
    src.get_param("p").value = 5.0

    (copy,) = _copy_paste(scene, src)

    assert copy.get_param("s").value == "d"


def test_the_copy_is_independent_of_the_original(scene, module):
    (src,) = chunk.create_chunks(module)
    src.get_param("p").value = 5.0
    (copy,) = _copy_paste(scene, src)

    src.get_param("p").value = 9.0

    assert copy.get_param("p").value == pytest.approx(5.0)


def test_a_wired_param_pastes_still_wired(scene, module):
    (driver,) = chunk.create_chunks(module)
    driver.name = "driver"
    driver.get_param("p").value = 3.0
    (src,) = chunk.create_chunks(module)
    src.get_param("p").value = "@driver.p@ * 2"

    (copy,) = _copy_paste(scene, src)

    assert copy.get_param("p").raw_value == "@driver.p@ * 2"
    assert copy.get_param("p").value == pytest.approx(6.0)
    driver.get_param("p").value = 4.0
    assert copy.get_param("p").value == pytest.approx(8.0)


def test_several_chunks_paste_together(scene, module):
    a, b = chunk.create_chunks(module)[0], chunk.create_chunks(module)[0]
    a.get_param("p").value = 10.0
    b.get_param("p").value = 20.0

    copies = _copy_paste(scene, a, b)

    assert sorted(c.get_param("p").value for c in copies) == [10.0, 20.0]


def test_nothing_selected_pastes_nothing(scene, module):
    chunk.create_chunks(module)

    assert _copy_paste(scene) == []
