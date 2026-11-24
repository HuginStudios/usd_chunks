"""autosave: quick saves of an edited network, and telling an edit from a cook."""
from __future__ import annotations

import logging
import os
import time
import types
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdUI

from usd_chunks import Scene, asset_paths, autosave, chunk_cache, chunk_stage, chunk_utils, open_cache
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks.graph import backdrops

GROUP = "/chunks/ChunkGroup"
DIR_ENV = "USD_CHUNKS_AUTOSAVE_DIR"


@pytest.fixture(autouse=True)
def quick_save_dir(monkeypatch, tmp_path):
    """Quick saves in a folder of the test's own, never the user's real one."""
    root = tmp_path / "quick_saves"
    monkeypatch.setenv(DIR_ENV, str(root))
    return root


def _build(path):
    """Save a small network to path: a cube, a transform of it, the output, a backdrop."""
    with Scene.new(path=str(path)) as scene:
        box = scene.create.cube(prim_path="/geo/box")
        lift = scene.create.transform(box)
        scene.set_output(lift)
        scene.add_backdrop([box, lift], title="Geo")
        scene.save()


@pytest.fixture
def net(tmp_path):
    path = tmp_path / "shot.usda"
    _build(path)
    return path


@pytest.fixture
def scene(net):
    scene = Scene.open(str(net))
    yield scene
    scene.close()


@pytest.fixture
def tracker(scene):
    tracker = autosave.ChangeTracker(scene.get_stage())
    yield tracker
    tracker.close()


def _chunk(scene, name):
    return next(c for c in scene.get_chunks() if c.name == name)


def _move_node(scene, name, x, y):
    """Author a node position the way the Qt node graph's _write_pos_to_usd does."""
    stage = scene.get_stage()
    chunk = _chunk(scene, name)
    prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(chunk))
    api = UsdUI.NodeGraphNodeAPI(prim)
    pos_attr = api.GetPosAttr() or api.CreatePosAttr()
    home = chunk_utils.get_attr_home_layer(pos_attr)
    target = home or chunk_utils.get_bookkeeping_layer(stage, chunk)
    with Usd.EditContext(stage, target):
        pos_attr.Set(Gf.Vec2f(x, -y))


def _layers_state(stage):
    return [(layer.identifier, layer.dirty, layer.ExportToString())
            for layer in stage.GetLayerStack(includeSessionLayers=True)]


# -- paths ------------------------------------------------------------------------------


def _touch(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#usda 1.0\n")
    os.utime(path, (mtime, mtime))
    return path


def test_quick_saves_go_in_the_users_own_folder(tmp_path, monkeypatch, quick_save_dir):
    """Never beside the network: in USD_CHUNKS_AUTOSAVE_DIR, else the user's usd_chunks folder."""
    net = tmp_path / "shot.usda"
    assert autosave.autosave_root() == quick_save_dir
    assert autosave.network_folder(net).parent == quick_save_dir
    assert autosave.network_folder(net).name.startswith("shot-")
    monkeypatch.delenv(DIR_ENV)
    assert autosave.autosave_root() == Path(chunk_cache.user_cache_dir("autosave"))


def test_each_network_file_has_its_own_folder(tmp_path):
    one, other = tmp_path / "a" / "shot.usda", tmp_path / "b" / "shot.usda"
    assert autosave.network_folder(one) != autosave.network_folder(other)
    spelled = os.path.join(str(tmp_path), "a", ".", "shot.usda")
    assert autosave.network_folder(spelled) == autosave.network_folder(one)


def test_a_new_quick_save_is_named_by_date_and_time(tmp_path):
    net = tmp_path / "shot.usda"
    when = time.mktime((2026, 11, 18, 14, 30, 5, 0, 0, -1))
    first = autosave.new_target(net, when)
    assert Path(first) == autosave.network_folder(net) / "shot_2026-11-18_14-30-05.usda"
    _touch(Path(first), when)
    second = autosave.new_target(net, when)  # same second: never the same file
    assert Path(second).name == "shot_2026-11-18_14-30-05_2.usda"


def test_is_quick_save(tmp_path, quick_save_dir):
    net = tmp_path / "shot.usda"
    assert autosave.is_quick_save(autosave.new_target(net))
    assert not autosave.is_quick_save(net)
    # A folder that only starts with the same name is not the quick-save folder.
    assert not autosave.is_quick_save(str(quick_save_dir) + "_other" + os.sep + "shot.usda")


def test_pending_recovery_lists_newer_files_newest_first(tmp_path):
    net = tmp_path / "shot.usda"
    now = time.time()
    _touch(net, now)
    folder = autosave.network_folder(net)
    newer = _touch(folder / "shot_b.usda", now + 20)
    new = _touch(folder / "shot_a.usda", now + 10)
    old = _touch(folder / "shot_c.usda", now - 20)  # older than the save
    _touch(folder / "shot_d.usda.tmp", now + 30)  # a write that never finished
    _touch(autosave.network_folder(tmp_path / "other.usda") / "other_a.usda", now + 30)

    assert autosave.pending_recovery(net) == [str(newer), str(new)]
    assert autosave.quick_save_paths(net) == [str(newer), str(new), str(old)]
    net.unlink()
    assert autosave.pending_recovery(net) == autosave.quick_save_paths(net)


def test_discard_removes_every_quick_save_and_the_folder(tmp_path):
    net = tmp_path / "shot.usda"
    _touch(net, time.time())
    for name in ("shot_a.usda", "shot_b.usda"):
        _touch(autosave.network_folder(net) / name, time.time())
    autosave.discard(net)
    assert autosave.quick_save_paths(net) == []
    assert not autosave.network_folder(net).exists()
    assert net.exists()
    autosave.discard(net)  # nothing left: no error


def test_prune_keeps_the_newest(tmp_path):
    net = tmp_path / "shot.usda"
    now = time.time()
    files = [_touch(autosave.network_folder(net) / f"shot_{i}.usda", now - i) for i in range(5)]
    autosave.prune(net, keep=2)
    assert autosave.quick_save_paths(net) == [str(files[0]), str(files[1])]


def test_sweep_deletes_month_old_quick_saves_of_any_network(tmp_path):
    now = time.time()
    old_net = tmp_path / "old.usda"
    live_net = tmp_path / "live.usda"
    _touch(autosave.network_folder(old_net) / "old_a.usda", now - 40 * 86400)
    kept = _touch(autosave.network_folder(live_net) / "live_a.usda", now - 2 * 86400)
    gone = _touch(autosave.network_folder(live_net) / "live_b.usda", now - 31 * 86400)
    autosave.sweep(now=now)
    assert not autosave.network_folder(old_net).exists()
    assert kept.exists() and not gone.exists()
    autosave.sweep(now=now)  # again: nothing to do, no error


def test_autosave_minutes(monkeypatch, caplog):
    key = "USD_CHUNKS_AUTOSAVE_MINUTES"
    monkeypatch.delenv(key, raising=False)
    assert autosave.autosave_minutes() == 2
    monkeypatch.setenv(key, "")
    assert autosave.autosave_minutes() == 2
    monkeypatch.setenv(key, "0")
    assert autosave.autosave_minutes() == 0
    monkeypatch.setenv(key, "-5")
    assert autosave.autosave_minutes() == 0
    monkeypatch.setenv(key, "3")
    assert autosave.autosave_minutes() == 3
    monkeypatch.setenv(key, "often")
    with caplog.at_level(logging.WARNING, logger="usd_chunks.autosave"):
        assert autosave.autosave_minutes() == 2
        assert autosave.autosave_minutes() == 2
    assert len([r for r in caplog.records if "often" in r.getMessage()]) == 1


# -- change tracking ----------------------------------------------------------------------


def test_nothing_done_is_no_change(scene, tracker):
    assert not tracker.changed


def test_a_param_edit_is_a_change(scene, tracker):
    scene.set(_chunk(scene, "Transform"), tx=4.0)
    assert tracker.changed


def test_creating_a_chunk_is_a_change(scene, tracker):
    scene.create.sphere(prim_path="/geo/ball")
    assert tracker.changed


def test_deleting_a_chunk_is_a_change(scene, tracker):
    ball = scene.create.sphere(prim_path="/geo/ball")
    tracker.mark_clean()
    ball.request_delete()
    assert tracker.changed


def test_a_wire_is_a_change(scene, tracker):
    ball = scene.create.sphere(prim_path="/geo/ball")
    tracker.mark_clean()
    scene.connect(_chunk(scene, "Mesh"), ball)
    assert tracker.changed


def test_moving_a_node_is_a_change(scene, tracker):
    _move_node(scene, "Mesh", 400.0, 300.0)
    assert tracker.changed


def test_a_backdrop_edit_is_a_change(scene, tracker):
    backdrops.set_rect(scene.get_backdrops()[0], (0.0, 0.0, 900.0, 700.0))
    assert tracker.changed


def test_mark_clean_and_mark_changed(scene, tracker):
    tracker.mark_changed()
    assert tracker.changed
    tracker.mark_clean()
    assert not tracker.changed


def test_a_cook_is_no_change(scene, tracker, monkeypatch):
    stage = scene.get_stage()
    mesh = _chunk(scene, "Mesh")
    mesh.execute()
    _chunk(scene, "Transform").execute()
    # A cook's output, in the chunk's own layer -- also an opinion on a chunk prim, the way
    # an Override Chunk Prim cooks one.
    cook_layer = ChunkLayerManager().get_layer(mesh)
    with Usd.EditContext(stage, cook_layer):
        stage.DefinePrim("/geo/cooked", "Xform")
        stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(mesh)).CreateAttribute(
            "inputs:cooked", Sdf.ValueTypeNames.Int).Set(1)
    # The module hash a cook stamps on its prim, in the prim's bookkeeping layer.
    module = mesh.get_chunk_module()
    monkeypatch.setattr(module, "source_hash", "sha256:" + "0" * 64)
    chunk_utils.stamp_module_hash(stage, mesh)
    prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(mesh))
    assert prim.GetAttribute("info:moduleHash").Get() == "sha256:" + "0" * 64
    assert not tracker.changed


def test_writing_a_value_a_chunk_already_has_is_no_change(scene, tracker):
    stage = scene.get_stage()
    prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(_chunk(scene, "Mesh")))
    attr = prim.GetAttribute("inputs:mesh_type")
    attr.Set(attr.Get())
    assert not tracker.changed


def test_stage_reload_is_no_change(scene, tracker, net):
    stage = scene.get_stage()
    scene.set(_chunk(scene, "Transform"), tx=4.0)
    assert tracker.changed
    stage.Reload()  # throws the unsaved edit away: back to the file
    assert not tracker.changed
    scene.set(_chunk(scene, "Transform"), ty=5.0)
    assert tracker.changed


def test_layer_reload_of_a_file_changed_on_disk_is_no_change(scene, tracker, net):
    stage = scene.get_stage()
    # Another process edits the file: a chunk is added and the backdrop retitled.
    layer = Sdf.Layer.OpenAsAnonymous(str(net))
    spec = Sdf.CreatePrimInLayer(layer, GROUP + "/Extra")
    spec.specifier, spec.typeName = Sdf.SpecifierDef, "Chunk"
    layer.GetAttributeAtPath(GROUP + "/backdrop1.ui:description").default = "Renamed"
    net.write_text(layer.ExportToString())
    later = time.time() + 5
    os.utime(net, (later, later))
    stage.GetRootLayer().Reload()
    assert stage.GetPrimAtPath(GROUP + "/Extra")
    assert not tracker.changed
    _move_node(scene, "Mesh", 10.0, 10.0)
    assert tracker.changed


def test_close_stops_tracking(scene):
    tracker = autosave.ChangeTracker(scene.get_stage())
    tracker.close()
    tracker.close()
    scene.set(_chunk(scene, "Transform"), tx=4.0)
    assert not tracker.changed


# -- writing --------------------------------------------------------------------------------


def _without_network_key(layer):
    data = dict(layer.customLayerData)
    data.pop(autosave.NETWORK_KEY, None)
    if data:
        layer.customLayerData = data
    else:
        layer.ClearCustomLayerData()
    return layer.ExportToString()


def test_quick_save_is_what_save_scene_writes(scene, net):
    scene.set(_chunk(scene, "Transform"), tx=4.0)
    target = autosave.new_target(net)
    text = autosave.write_quick_save(scene, target, net)
    assert text is not None
    with open(target, encoding="utf-8") as f:
        assert f.read() == text
    quick = Sdf.Layer.OpenAsAnonymous(target)
    assert quick.customLayerData[autosave.NETWORK_KEY] == os.path.realpath(net)
    scene.save(str(net))
    assert _without_network_key(quick) == Sdf.Layer.OpenAsAnonymous(str(net)).ExportToString()


def test_relative_paths_in_a_quick_save_are_from_the_networks_folder(tmp_path, monkeypatch):
    """Kept far from the network, a quick save still holds its paths as the network's file
    does: recovered into it, they name the same files."""
    inputs = tmp_path / "proj" / "inputs"
    inputs.mkdir(parents=True)
    layer = Sdf.Layer.CreateNew(str(inputs / "set.usda"))
    Sdf.CreatePrimInLayer(layer, "/World/chair").specifier = Sdf.SpecifierDef
    layer.GetPrimAtPath("/World").specifier = Sdf.SpecifierDef
    layer.defaultPrim = "World"
    layer.Save()
    net = tmp_path / "proj" / "shot" / "net.usda"
    net.parent.mkdir()
    monkeypatch.chdir(tmp_path)
    asset_paths._warned.clear()
    with Scene.new(path=str(net)) as built:
        built.set_output(built.create.reference(file_path="../inputs/set.usda", primpath_location="/ref"))
        built.save()

    with Scene.open(str(net)) as scene:
        scene.set(next(c for c in scene.get_chunks() if c.get_param("file_path") is not None),
                  primpath_location="/moved")
        target = autosave.new_target(net)
        autosave.write_quick_save(scene, target, net)
    values = []
    quick = Sdf.Layer.OpenAsAnonymous(target)
    quick.Traverse(Sdf.Path.absoluteRootPath, lambda p: values.append(quick.GetAttributeAtPath(p).default)
                   if p.IsPropertyPath() and p.name == "inputs:file_path" else None)
    assert values == ["../inputs/set.usda"]

    root = Sdf.Layer.FindOrOpen(str(net))
    root.Reload(force=True)
    autosave.recover_into(root, target)
    root.Save()
    with Scene.open(str(net)) as recovered:
        assert recovered.get_stage().GetPrimAtPath("/moved/chair")


def test_quick_save_reopens_to_the_same_network(net, tmp_path):
    scene = Scene.open(str(net))
    try:
        scene.set(_chunk(scene, "Transform"), tx=4.0)
        ball = scene.create.sphere(prim_path="/geo/ball")
        scene.connect(_chunk(scene, "Mesh"), ball)
        target = autosave.new_target(net)
        autosave.write_quick_save(scene, target, net)
        live = {c.name: (chunk_stage.get_chunk_prim_path(c), [d.name for d in scene.get_inputs(c)])
                for c in scene.get_chunks()}
    finally:
        scene.close()
    reopened = Scene.open(target)
    try:
        assert {c.name: (chunk_stage.get_chunk_prim_path(c), [d.name for d in reopened.get_inputs(c)])
                for c in reopened.get_chunks()} == live
        assert _chunk(reopened, "Transform").get_param("tx").value == pytest.approx(4.0)
        assert reopened.get_stage().GetPrimAtPath("/geo/ball")
    finally:
        reopened.close()


def test_writing_leaves_the_stage_alone(scene, net, monkeypatch):
    scene.set(_chunk(scene, "Transform"), tx=4.0)
    stage = scene.get_stage()
    before = _layers_state(stage)

    def refuse(*_args, **_kwargs):
        raise AssertionError("a quick save must not do this")

    monkeypatch.setattr(open_cache, "store", refuse)
    monkeypatch.setattr(chunk_stage, "_reload_cached_layer", refuse)
    monkeypatch.setattr(type(scene), "lay_out_scripted", refuse)
    tracker = autosave.ChangeTracker(stage)
    try:
        assert autosave.write_quick_save(scene, autosave.new_target(net), net) is not None
        assert not tracker.changed
    finally:
        tracker.close()
    assert _layers_state(stage) == before


def test_unchanged_text_is_not_written_again(scene, net):
    target = autosave.new_target(net)
    text = autosave.write_quick_save(scene, target, net)
    old = time.time() - 100
    os.utime(target, (old, old))
    assert autosave.write_quick_save(scene, target, net, last_text=text) is None
    assert os.path.getmtime(target) == pytest.approx(old)
    scene.set(_chunk(scene, "Transform"), tx=4.0)
    assert autosave.write_quick_save(scene, target, net, last_text=text) not in (None, text)


def test_a_quick_save_is_usda_whatever_format_the_layer_has(scene, net, monkeypatch):
    """USD 25.05 makes the anonymous layer Save Scene builds in its "sdf" text format, whose
    text starts `#sdf 1.4.32`: written to a .usda file as it was, the quick save could not
    be opened and recovering it failed. usd-core has no "sdf" format, so the layer is made
    to export and report itself the way 25.05's does."""
    real_saved, real_export, real_format = chunk_stage.saved_layer, Sdf.Layer.ExportToString, Sdf.Layer.GetFileFormat
    sdf_like = []

    def saved_layer(*args, **kwargs):
        layer = real_saved(*args, **kwargs)
        sdf_like.append(layer)
        return layer

    def export(layer):
        text = real_export(layer)
        return "#sdf 1.4.32" + text[len("#usda 1.0"):] if layer in sdf_like else text

    def file_format(layer):
        return types.SimpleNamespace(formatId="sdf") if layer in sdf_like else real_format(layer)

    monkeypatch.setattr(chunk_stage, "saved_layer", saved_layer)
    monkeypatch.setattr(Sdf.Layer, "ExportToString", export)
    monkeypatch.setattr(Sdf.Layer, "GetFileFormat", file_format)
    scene.set(_chunk(scene, "Transform"), tx=4.0)
    target = autosave.new_target(net)
    autosave.write_quick_save(scene, target, net)
    assert sdf_like and export(sdf_like[0]).startswith("#sdf")  # the stand-in took
    monkeypatch.undo()

    with open(target, "rb") as f:
        assert f.read().startswith(b"#usda 1.0")
    root = Sdf.Layer.CreateAnonymous(".usda")
    root.TransferContent(Sdf.Layer.OpenAsAnonymous(str(net)))
    autosave.recover_into(root, target)
    assert root.GetAttributeAtPath(GROUP + "/Transform.inputs:translate_grp:tx").default == pytest.approx(4.0)


def test_a_failed_write_raises_and_leaves_no_temp_file(scene, net, tmp_path, monkeypatch):
    def broken(*_args):
        raise PermissionError("no")

    monkeypatch.setattr(os, "replace", broken)
    target = autosave.new_target(net)
    with pytest.raises(PermissionError):
        autosave.write_quick_save(scene, target, net)
    assert list(autosave.network_folder(net).iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["quick_saves", "shot.usda"]


# -- recovering -----------------------------------------------------------------------------


def test_recover_into_replaces_the_network_and_keeps_the_rest(net, tmp_path):
    # The shot also holds a camera no chunk made.
    layer = Sdf.Layer.FindOrOpen(str(net))
    Sdf.CreatePrimInLayer(layer, "/World/cam").specifier = Sdf.SpecifierDef
    layer.GetPrimAtPath("/World/cam").typeName = "Camera"
    layer.Save()

    scene = Scene.open(str(net))
    try:
        scene.set(_chunk(scene, "Transform"), tx=4.0)
        scene.create.sphere(prim_path="/geo/ball")
        scene.remove_backdrop(scene.get_backdrops()[0])
        target = autosave.new_target(net)
        autosave.write_quick_save(scene, target, net)
    finally:
        scene.close()
    # The session's edits never reached the file: it is as saved.
    root = Sdf.Layer.FindOrOpen(str(net))
    root.Reload(force=True)
    on_disk = net.read_bytes()
    assert root.GetPrimAtPath(GROUP + "/backdrop1")

    autosave.recover_into(root, target)

    assert root.dirty
    assert net.read_bytes() == on_disk
    assert root.GetPrimAtPath("/World/cam").typeName == "Camera"
    assert root.GetPrimAtPath("/chunks").typeName == "Scope"
    assert root.GetAttributeAtPath(GROUP + "/Transform.inputs:translate_grp:tx").default == pytest.approx(4.0)
    assert not root.GetPrimAtPath(GROUP + "/backdrop1")
    names = {spec.name for spec in root.GetPrimAtPath(GROUP).nameChildren}
    assert names == {"Mesh", "Transform", "Output", "Mesh1"}  # Mesh1: the sphere
    quick = Sdf.Layer.OpenAsAnonymous(target)
    assert {s.name for s in quick.GetPrimAtPath(GROUP).nameChildren} == names
    root.Reload(force=True)  # leave the shared layer as the file says
