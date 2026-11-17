"""Opening a saved network reads each chunk's result back instead of cooking it (open_cache).

Saving keeps each chunk's layer in the cook cache under a key for everything its result can
depend on; opening reads it back while the key still matches, and cooks it otherwise.
"""

from __future__ import annotations

import pytest
from chunk_stubs import register_stub_module
from pxr import Sdf, UsdGeom

from usd_chunks import Scene, chunk, chunk_cook, cook_cache, open_cache


@pytest.fixture(autouse=True)
def _cache(monkeypatch, tmp_path):
    """The open cache on, in a folder of this test's own."""
    monkeypatch.setenv(open_cache.ENV, "1")
    monkeypatch.setenv(cook_cache.ENV, "1")
    monkeypatch.setenv(cook_cache.ENV_DIR, str(tmp_path / "cache"))


@pytest.fixture
def restored(monkeypatch):
    """The names of the chunks whose result was read back from an open-cache entry."""
    names = []
    real = cook_cache.CookEntry.restore

    def counting(self, c, layer):
        ok = real(self, c, layer)
        if ok and open_cache.is_open_entry(self):
            names.append(c.name)
        return ok

    monkeypatch.setattr(cook_cache.CookEntry, "restore", counting)
    return names


def _chain(scene, depth=3):
    with chunk.change_block():
        last = scene.create.cube(name="Box", prim_path="/World/box")
        for i in range(depth):
            last = scene.create.transform(last, name=f"T{i}", primpath_location="/World/box", ty=1.0)
        scene.set_output(last)


def _height(scene) -> float:
    prim = scene.get_stage().GetPrimAtPath("/World/box")
    return UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation()[1]


def _save(scene, path) -> None:
    scene.save(str(path))
    open_cache.wait()


def test_an_opened_network_reads_its_results_back(tmp_path, restored):
    path = tmp_path / "chain.usda"
    with Scene.new() as scene:
        _chain(scene)
        _save(scene, path)

    with Scene.open(str(path)) as scene:
        assert sorted(restored) == ["T0", "T1", "T2"]
        assert _height(scene) == pytest.approx(3.0)
        # And it is a network like any other from there on.
        scene.set(scene.get_chunk("T0"), ty=2.0)
        assert _height(scene) == pytest.approx(4.0)


def test_a_param_changed_in_the_file_cooks_that_chunk_and_those_below(tmp_path, restored):
    path = tmp_path / "chain.usda"
    with Scene.new() as scene:
        _chain(scene)
        _save(scene, path)
    layer = Sdf.Layer.FindOrOpen(str(path))
    attr = next(spec for spec in layer.GetPrimAtPath("/chunks/ChunkGroup/T1").properties
                if spec.name.endswith("ty"))
    attr.default = 5.0
    layer.Save()

    with Scene.open(str(path)) as scene:
        assert restored == ["T0"]
        assert _height(scene) == pytest.approx(7.0)


def test_a_changed_sublayer_file_cooks_everything(tmp_path, restored):
    extra = tmp_path / "extra.usda"
    Sdf.Layer.CreateNew(str(extra)).Save()
    path = tmp_path / "chain.usda"
    with Scene.new() as scene:
        scene.get_stage().GetRootLayer().subLayerPaths.append(str(extra))
        _chain(scene)
        _save(scene, path)
    sub = Sdf.Layer.FindOrOpen(str(extra))
    Sdf.CreatePrimInLayer(sub, "/Elsewhere")
    sub.Save()

    with Scene.open(str(path)):
        assert restored == []


def test_switched_off_it_cooks(tmp_path, restored, monkeypatch):
    path = tmp_path / "chain.usda"
    with Scene.new() as scene:
        _chain(scene)
        _save(scene, path)
    monkeypatch.setenv(open_cache.ENV, "0")

    with Scene.open(str(path)) as scene:
        assert restored == []
        assert _height(scene) == pytest.approx(3.0)


def _note_writer():
    """Writes /Thing in its own layer, and /Note in the session layer as well."""

    def execute(node):
        stage = node.get_stage()
        stage.DefinePrim("/Thing", "Xform")
        Sdf.CreatePrimInLayer(stage.GetSessionLayer(), "/Note")

    return register_stub_module(execute=execute)


def test_a_chunk_that_writes_the_session_layer_is_not_kept(tmp_path, restored):
    module = _note_writer()
    path = tmp_path / "writer.usda"
    with Scene.new() as scene:
        (c,) = module.load()
        scene.add_chunk(c)
        scene.set_output(c)
        _save(scene, path)

    with Scene.open(str(path)) as scene:
        assert restored == []
        assert scene.get_stage().GetSessionLayer().GetPrimAtPath("/Note") is not None


def test_a_module_can_ask_to_cook_on_every_open(tmp_path, restored):
    module = register_stub_module(execute=lambda node: node.get_stage().DefinePrim("/Thing", "Xform"))
    path = tmp_path / "clock.usda"
    with Scene.new() as scene:
        (c,) = module.load()
        # The module's config, which every chunk of it shares, as config() would return it.
        c.get_config().cook_on_open = True
        scene.add_chunk(c)
        scene.set_output(c)
        _save(scene, path)

    with Scene.open(str(path)) as scene:
        assert restored == []
        assert scene.get_stage().GetPrimAtPath("/Thing")


def test_the_first_edit_after_opening_still_skips_what_it_does_not_reach(tmp_path, restored, monkeypatch):
    """What a kept cook read comes back with its result, so skip_cook can skip it again."""
    path = tmp_path / "branches.usda"
    with Scene.new() as scene:
        with chunk.change_block():
            a = scene.create.prim(name="A", primpath_location="/World/a")
            moved = scene.create.transform(a, name="Move", primpath_location="/World/a", ty=1.0)
            b = scene.create.prim(moved, name="B", primpath_location="/World/b")
            scene.set_output(b)
        _save(scene, path)

    with Scene.open(str(path)) as scene:
        assert "B" in restored
        cooked = []
        real = chunk_cook.cook
        monkeypatch.setattr(chunk_cook, "cook", lambda c, *a, **k: cooked.append(c.name) or real(c, *a, **k))

        scene.set(scene.get_chunk("Move"), ty=2.0)

        assert "Move" in cooked
        assert "B" not in cooked, "B reads only /World/b, which the edit did not touch"
