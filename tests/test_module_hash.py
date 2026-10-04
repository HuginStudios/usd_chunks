"""A cooked chunk's prim records the module source it cooked with; a load compares it.

info:moduleHash sits beside info:chunkType. It is written by the cook (not by a save, which
would relabel output that was never re-made), travels through save and reopen as ordinary
prim data, and load_chunks warns once per chunk type whose saved code is not the code loaded.
"""

from __future__ import annotations

import logging

from pxr import Sdf, Usd

from usd_chunks import Scene, chunk_cook, chunk_naming, chunk_registry, chunk_utils
from usd_chunks.chunk_topology import get_chunk_prim_path
from usd_chunks.store import pin

ATTR = chunk_naming.MODULE_HASH_ATTR
OLD = "sha256:" + "0" * 64
OLDER = "sha256:" + "1" * 64
WARNING = "were saved with different code"


def _prim(scene, chunk):
    return scene.get_stage().GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))


def _in_home_layer(prim, edit):
    # The stamp lives in the chunk's bookkeeping layer, stronger than the default edit target:
    # a test that writes or removes it elsewhere is shadowed and changes nothing.
    home = chunk_utils.get_attr_home_layer(prim.GetAttribute(ATTR))
    with Usd.EditContext(prim.GetStage(), home):
        edit()


def _set_hash(prim, value):
    _in_home_layer(prim, lambda: prim.GetAttribute(ATTR).Set(value))


def _clear_hash(prim):
    _in_home_layer(prim, lambda: prim.RemoveProperty(ATTR))


def _module(name="transform"):
    return chunk_registry.ChunkRegistry().get_chunk_module(name)


def _save_transforms(path, count=2):
    with Scene() as scene:
        for _ in range(count):
            scene.create.transform()
        scene.save(str(path))


def _open_and_collect_warnings(path, caplog):
    with caplog.at_level(logging.WARNING):
        with Scene.open(str(path)):
            pass
    return [r.getMessage() for r in caplog.records if WARNING in r.getMessage()]


def test_a_cook_stamps_the_hash_of_the_source_it_ran():
    with Scene() as scene:
        node = scene.create.transform()
        stamped = _prim(scene, node).GetAttribute(ATTR).Get()

        assert stamped == _module().source_hash
        assert stamped.startswith("sha256:")


def test_a_recook_restamps_a_prim_whose_recorded_code_is_out_of_date():
    with Scene() as scene:
        node = scene.create.transform()
        prim = _prim(scene, node)
        _set_hash(prim, OLD)

        assert chunk_cook.cook(node) is True

        assert prim.GetAttribute(ATTR).Get() == _module().source_hash


def test_the_module_hash_is_pins_hash_of_the_module_file():
    module = _module()

    assert module.source_hash == pin.hash_file(module.module_file)


def test_a_comment_changes_the_hash(tmp_path):
    source = tmp_path / "chunk.py"
    source.write_text("x = 1\n", encoding="utf-8")
    before = pin.hash_file(source)
    source.write_text("x = 1  # note\n", encoding="utf-8")

    assert pin.hash_file(source) != before


def test_a_save_writes_the_stamp_into_the_file(tmp_path):
    path = tmp_path / "shot.usda"
    _save_transforms(path, count=1)

    assert f'{ATTR} = "{_module().source_hash}"' in path.read_text(encoding="utf-8")


def test_a_load_with_unchanged_code_is_quiet(tmp_path, caplog):
    path = tmp_path / "shot.usda"
    _save_transforms(path)

    assert _open_and_collect_warnings(path, caplog) == []


def test_a_load_warns_once_per_type_and_names_both_hashes(tmp_path, caplog):
    path = tmp_path / "shot.usda"
    _save_transforms(path, count=2)
    text = path.read_text(encoding="utf-8").replace(_module().source_hash, OLD)
    path.write_text(text, encoding="utf-8")

    warnings = _open_and_collect_warnings(path, caplog)

    assert len(warnings) == 1
    assert "2 'transform' chunk(s)" in warnings[0]
    assert OLD[:19] in warnings[0]
    assert _module().source_hash[:19] in warnings[0]


def test_the_warning_names_the_module_file_to_diff(tmp_path, caplog):
    path = tmp_path / "shot.usda"
    _save_transforms(path, count=1)
    text = path.read_text(encoding="utf-8").replace(_module().source_hash, OLD)
    path.write_text(text, encoding="utf-8")

    warnings = _open_and_collect_warnings(path, caplog)

    assert str(_module().module_file) in warnings[0]


def test_prims_saved_with_different_old_hashes_list_each_once(tmp_path, caplog):
    path = tmp_path / "shot.usda"
    _save_transforms(path, count=2)
    current = _module().source_hash
    text = path.read_text(encoding="utf-8")
    text = text.replace(current, OLD, 1).replace(current, OLDER, 1)
    path.write_text(text, encoding="utf-8")

    warnings = _open_and_collect_warnings(path, caplog)

    assert len(warnings) == 1
    assert OLD[:19] in warnings[0] and OLDER[:19] in warnings[0]


def test_a_file_from_before_the_stamp_existed_is_quiet(tmp_path, caplog):
    path = tmp_path / "shot.usda"
    _save_transforms(path)
    kept = [line for line in path.read_text(encoding="utf-8").splitlines() if ATTR not in line]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    assert _open_and_collect_warnings(path, caplog) == []


def test_a_recorded_hash_is_read_back_from_the_prim():
    with Scene() as scene:
        node = scene.create.transform()
        prim = _prim(scene, node)

        assert chunk_utils.recorded_module_hash(prim) == _module().source_hash

        _clear_hash(prim)

        assert chunk_utils.recorded_module_hash(prim) is None


def test_no_recorded_hash_is_not_a_change():
    with Scene() as scene:
        node = scene.create.transform()
        prim = _prim(scene, node)
        _clear_hash(prim)

        assert chunk_utils.module_changed_since_cook(prim, _module()) is False


def test_a_differing_recorded_hash_is_a_change():
    with Scene() as scene:
        node = scene.create.transform()
        prim = _prim(scene, node)
        _set_hash(prim, OLD)

        assert chunk_utils.module_changed_since_cook(prim, _module()) is True
