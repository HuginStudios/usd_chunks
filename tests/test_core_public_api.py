"""The module-level API a chunk author and a host call: create, find, import, reload, save.

Coverage showed none of these ran in any test -- the suite builds chunks with
chunk.Chunk(...) + scene.add_chunk and reaches modules through chunk_stubs, so the entry
points themselves went unchecked.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd

from usd_chunks import chunk, chunk_registry, chunk_stage
from usd_chunks.chunk_flags import ChunkFlags
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


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)
    return scene


def _main():
    c = chunk.Chunk("made")
    c.add_param(chunk.Slider("p", default_value=1.0))
    return c


# -- create_chunks / get_chunk ---------------------------------------------------------------

def test_create_chunks_adds_the_chunk_to_the_scene(scene):
    module = register_stub_module(main=_main)

    (made,) = chunk.create_chunks(module.module_name)

    assert made in scene.get_chunks()
    assert made.get_config().module == module.module_name
    assert scene.get_stage().GetPrimAtPath(chunk_stage.get_chunk_prim_path(made)).IsValid()


def test_create_chunks_anonymous_leaves_the_scene_alone(scene):
    module = register_stub_module(main=_main)

    (made,) = chunk.create_chunks(module.module_name, anonymous=True)

    assert made not in scene.get_chunks()


def test_create_chunks_twice_makes_two_uniquely_named_chunks(scene):
    module = register_stub_module(main=_main)

    (a,) = chunk.create_chunks(module.module_name)
    (b,) = chunk.create_chunks(module.module_name)

    assert a is not b
    assert a.name != b.name


def test_create_chunks_of_an_unknown_module_is_empty(scene):
    assert chunk.create_chunks("no_such_module_anywhere") == []


def test_a_main_that_raises_creates_nothing(scene):
    def broken():
        raise RuntimeError("boom")

    module = register_stub_module(main=broken)

    assert chunk.create_chunks(module.module_name) == []


def test_a_main_returning_a_list_creates_each(scene):
    module = register_stub_module(main=lambda: [chunk.Chunk("one"), chunk.Chunk("two")])

    made = chunk.create_chunks(module.module_name)

    assert [c.name for c in made] == ["one", "two"]
    assert all(c.get_config().module == module.module_name for c in made)


def test_get_chunk_finds_a_chunk_by_name(scene):
    module = register_stub_module(main=_main)
    (made,) = chunk.create_chunks(module.module_name)

    assert chunk.get_chunk(made.name) is made
    assert chunk.get_chunk("nobody") is None


# -- registry: import, register, reload, export ---------------------------------------------

_SOURCE = """from usd_chunks import chunk


def main():
    return chunk.Chunk({name!r})


def config():
    return chunk.DefaultConfig()
"""


def _write_module(tmp_path, name):
    path = tmp_path / f"{name}.py"
    path.write_text(_SOURCE.format(name=name), encoding="utf-8")
    return path


@pytest.fixture()
def drop_imported():
    names = []
    yield names
    registry = chunk_registry.ChunkRegistry()
    for name in names:
        registry.registered_modules.pop(name, None)


def test_import_chunk_registers_and_loads_a_py_file(scene, tmp_path, drop_imported):
    path = _write_module(tmp_path, "imported_chunk_xyz")
    drop_imported.append("imported_chunk_xyz")

    chunk_registry.ChunkRegistry().import_chunk(path)

    module = chunk_registry.ChunkRegistry().get_chunk_module("imported_chunk_xyz")
    assert module is not None and module.loaded


def test_import_chunk_ignores_other_extensions(scene, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("main = None", encoding="utf-8")

    chunk_registry.ChunkRegistry().import_chunk(path)

    assert chunk_registry.ChunkRegistry().get_chunk_module("notes") is None


def test_a_second_file_with_a_taken_name_is_recorded_as_shadowed(scene, tmp_path, drop_imported):
    registry = chunk_registry.ChunkRegistry()
    first = _write_module(tmp_path, "shadow_me_xyz")
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    second = _write_module(other_dir, "shadow_me_xyz")
    drop_imported.append("shadow_me_xyz")

    (a,) = chunk_registry.ChunkModule.from_chunk_definition(first)
    (b,) = chunk_registry.ChunkModule.from_chunk_definition(second)
    assert registry.register_module(a) is a
    assert registry.register_module(b) is None

    assert registry.get_chunk_module("shadow_me_xyz") is a
    assert ("shadow_me_xyz", Path(first), Path(second)) in registry.shadowed_modules


def test_registering_the_same_file_again_is_not_a_shadow(scene, tmp_path, drop_imported):
    registry = chunk_registry.ChunkRegistry()
    path = _write_module(tmp_path, "same_file_xyz")
    drop_imported.append("same_file_xyz")
    (a,) = chunk_registry.ChunkModule.from_chunk_definition(path)
    (again,) = chunk_registry.ChunkModule.from_chunk_definition(path)
    registry.register_module(a)
    shadows = len(registry.shadowed_modules)

    assert registry.register_module(again) is a
    assert len(registry.shadowed_modules) == shadows


def test_reload_modules_recreates_as_many_chunks_as_there_were(scene):
    module = register_stub_module()
    chunk.create_chunks(module.module_name)
    chunk.create_chunks(module.module_name)

    chunk_registry.ChunkRegistry().reload_modules()

    of_module = [c for c in scene.get_chunks()
                 if (cfg := c.get_config()) and cfg.module == module.module_name]
    assert len(of_module) == 2


def test_export_writes_the_module_source(scene, tmp_path):
    module = register_stub_module()
    out = tmp_path / "exported.py"

    module.export(out)

    assert "def main():" in out.read_text(encoding="utf-8")


def test_export_refuses_another_extension(scene, tmp_path):
    module = register_stub_module()
    out = tmp_path / "exported.txt"

    module.export(out)

    assert not out.exists()


# -- params built from USD attributes -------------------------------------------------------

#: The stages behind _attr's attributes: a Usd.Attribute does not keep its stage alive.
_STAGES = []


def _attr(type_name, value, limits=None):
    stage = Usd.Stage.CreateInMemory()
    _STAGES.append(stage)
    attr = stage.DefinePrim("/X").CreateAttribute("thing", type_name)
    attr.Set(value)
    if limits:
        attr.SetMetadata("limits", limits)
    return attr


def test_hard_limits_become_the_sliders_range():
    attr = _attr(Sdf.ValueTypeNames.Float, 0.5, {"hard": {"minimum": 0.0, "maximum": 1.0}})
    if not hasattr(attr, "GetLimits"):
        pytest.skip("this USD build has no attribute limits API")

    param = chunk.Slider.from_usd_attr(attr)

    assert (param.min_value, param.max_value) == (0.0, 1.0)


def test_an_attribute_without_limits_leaves_the_range_alone():
    param = chunk.Slider.from_usd_attr(_attr(Sdf.ValueTypeNames.Float, 0.5))

    assert param.value == pytest.approx(0.5)
    assert param.default_value == pytest.approx(0.5)


def test_a_slider_from_an_attribute_writes_it(scene):
    # On the scene's stage: a param writes through the stage the registry's scene holds.
    attr = scene.get_stage().DefinePrim("/X").CreateAttribute("thing", Sdf.ValueTypeNames.Float)
    attr.Set(0.5)
    param = chunk.Slider.from_usd_attr(attr)

    param.value = 0.75

    assert attr.Get() == pytest.approx(0.75)


@pytest.mark.parametrize("value", [
    [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [5, 6, 7, 1]],
    [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 5, 6, 7, 1],
], ids=["rows", "flat"])
def test_a_matrix_accepts_rows_or_a_flat_list(value):
    param = chunk.Matrix("m")

    param.value = value

    assert isinstance(param.value, Gf.Matrix4d)
    assert param.value.ExtractTranslation() == Gf.Vec3d(5, 6, 7)


def test_is_param_t_filters_by_param_type():
    sliders = chunk.is_param_t(ChunkFlags.ParamType.FLOAT_SLIDER)

    assert sliders(chunk.Slider("s"))
    assert not sliders(chunk.LineEdit("l"))
    assert not sliders(chunk.Group("g"))
