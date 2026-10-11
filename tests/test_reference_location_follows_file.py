"""A Reference's location follows its file only until it is chosen.

Picking a file names the prim after it -- a UI convenience. But once a location has been
typed, scripted or loaded from a saved network, a new file swaps the asset, not where the
network put it: `usdchunks run net.usda Reference.file_path=other.usd`, and every run of a
`usdchunks batch`, otherwise moved the prim the rest of the network targets.
"""

from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks import Scene


def _asset(path):
    stage = Usd.Stage.CreateNew(str(path))
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/root").GetPrim())
    stage.GetRootLayer().Save()
    return str(path)


def test_an_untouched_location_follows_each_new_file(tmp_path) -> None:
    first, second = _asset(tmp_path / "crate.usda"), _asset(tmp_path / "barrel.usda")
    with Scene() as scene:
        ref = scene.create.reference(file_path=first)
        location = ref.get_param("primpath_location")
        assert location.value == "/crate"
        ref.get_param("file_path").value = second
        assert location.value == "/barrel"


def test_a_chosen_location_stays_when_the_file_changes(tmp_path) -> None:
    first, second = _asset(tmp_path / "crate.usda"), _asset(tmp_path / "barrel.usda")
    with Scene() as scene:
        ref = scene.create.reference(file_path=first)
        location = ref.get_param("primpath_location")
        location.value = "/Asset"
        ref.get_param("file_path").value = second
        assert location.value == "/Asset"


def test_a_glob_suggests_no_location(tmp_path) -> None:
    """A pattern names many files (usdchunks batch fills it per run), so no one name."""
    with Scene() as scene:
        ref = scene.create.reference()
        ref.get_param("file_path").value = "./assets/*.usda"
        assert not ref.get_param("primpath_location").value
