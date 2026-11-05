"""Authoring mistakes that used to pass without a word.

- USD's own variant-path spelling, `/geo{look=fancy}box`, read as a prim called `geobox`.
- A mistyped target or material: the chunk authored nothing and said nothing.
- `scene.save()` over the file a scene was opened on deleted that file's own prims.
- A param another setting hides (a tube's `radius` on a sphere Mesh) did nothing, silently.

The warning on a missing target must not fire for a target that is only switched off for
the moment -- in a muted layer, or in a variant not selected -- which is the case the
silence was there for.
"""
from __future__ import annotations

import logging

import pytest
from pxr import Sdf, Usd

from usd_chunks import Scene, chunk_utils

ASSET = """#usda 1.0
(
    defaultPrim = "World"
    upAxis = "Y"
)

def Xform "World"
{
    def Scope "geo" (
        variants = {
            string look = "plain"
        }
        prepend variantSets = "look"
    )
    {
        def Cube "box"
        {
        }
        variantSet "look" = {
            "fancy" {
                def Sphere "extra"
                {
                }
            }
            "plain" {
            }
        }
    }
}
"""

SHOT = """#usda 1.0
(
    subLayers = [@./asset.usda@]
)

over "World"
{
    def Camera "cam"
    {
    }
}
"""


@pytest.fixture
def shot(tmp_path):
    (tmp_path / "asset.usda").write_text(ASSET)
    (tmp_path / "shot.usda").write_text(SHOT)
    return tmp_path / "shot.usda"


def _warnings(caplog, text):
    return [r for r in caplog.records if r.levelno == logging.WARNING and text in r.getMessage()]


# --------------------------------------------------------------------------------------
# Variant paths


@pytest.mark.parametrize("path", ["/World/geo{look=fancy}box", "/World/geo{look=fancy}/box"])
def test_both_variant_spellings_name_the_same_prim(path):
    assert chunk_utils.split_variant_path(path) == (
        "/World/geo", "/World/geo/box", [("/World/geo", "look", "fancy")])


def test_sdf_spells_variant_paths_without_the_slash():
    # The spelling the parser has to read, straight from USD.
    path = Sdf.Path("/World/geo").AppendVariantSelection("look", "fancy").AppendChild("box")
    assert chunk_utils.split_variant_path(str(path))[1] == "/World/geo/box"


def test_variant_names_may_hold_dashes():
    assert chunk_utils.split_variant_path("/a{lod=hi-res}/b")[2] == [("/a", "lod", "hi-res")]


def test_an_unreadable_selection_raises_rather_than_hanging():
    with pytest.raises(ValueError):
        chunk_utils.split_variant_path("/a{oops}/b")


def test_a_transform_on_the_sdf_spelling_moves_the_prim_in_the_variant(shot):
    stage = Usd.Stage.Open(str(shot))
    with Scene(stage) as scene:
        xf = scene.create.transform(primpath_location="/World/geo{look=fancy}box", ty=2.0)
        scene.set_output(xf)
        vset = stage.GetPrimAtPath("/World/geo").GetVariantSets().GetVariantSet("look")
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            vset.SetVariantSelection("fancy")
        box = stage.GetPrimAtPath("/World/geo/box")
        assert box.GetAttribute("xformOpOrder").Get()


# --------------------------------------------------------------------------------------
# Missing targets


def test_a_mistyped_target_warns_once(caplog):
    with Scene(host_name="pytest") as scene, caplog.at_level(logging.WARNING):
        box = scene.create.cube(prim_path="/World/box")
        xf = scene.create.transform(box, primpath_location="/World/bx", ty=1.0)
        scene.set_output(xf)
        scene.set(xf, ty=2.0)
    assert len(_warnings(caplog, "/World/bx")) == 1


def test_an_untouched_default_target_does_not_warn(caplog):
    with Scene(host_name="pytest") as scene, caplog.at_level(logging.WARNING):
        scene.set_output(scene.create.transform())
    assert not _warnings(caplog, "no prim at")


def test_a_mistyped_material_warns(caplog):
    with Scene(host_name="pytest") as scene, caplog.at_level(logging.WARNING):
        box = scene.create.cube(prim_path="/World/box")
        surf = scene.create.preview_surface(box, mat_path="/World/materials/look")
        bind = scene.create.bind_material(surf, material_path="/World/material/look",
                                          primpath_location="/World/box")
        scene.set_output(bind)
    assert _warnings(caplog, "/World/material/look")


def test_a_mistyped_binding_target_warns(caplog):
    with Scene(host_name="pytest") as scene, caplog.at_level(logging.WARNING):
        box = scene.create.cube(prim_path="/World/box")
        surf = scene.create.preview_surface(box, mat_path="/World/materials/look",
                                            primpath_location="/World/bx")
        scene.set_output(surf)
    assert _warnings(caplog, "/World/bx")


def test_a_prim_in_a_muted_layer_is_not_missing_for_good(tmp_path):
    layer = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(layer, "/World/held")
    stage = Usd.Stage.CreateInMemory()
    stage.GetRootLayer().subLayerPaths.append(layer.identifier)
    stage.MuteLayer(layer.identifier)
    assert not stage.GetPrimAtPath("/World/held")
    assert not chunk_utils.missing_for_good(stage, "/World/held")
    assert chunk_utils.missing_for_good(stage, "/World/hled")


def test_a_reference_in_a_muted_layer_can_bring_its_prims_back(tmp_path):
    (tmp_path / "asset.usda").write_text(
        '#usda 1.0\n(\n    defaultPrim = "A"\n)\n\ndef Xform "A"\n{\n    def Cube "c"\n    {\n    }\n}\n')
    arcs = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(arcs, "/World/prop").referenceList.Append(Sdf.Reference(str(tmp_path / "asset.usda")))
    stage = Usd.Stage.CreateInMemory()
    stage.GetRootLayer().subLayerPaths.append(arcs.identifier)
    assert stage.GetPrimAtPath("/World/prop/c")
    stage.MuteLayer(arcs.identifier)
    assert not chunk_utils.missing_for_good(stage, "/World/prop/c")


def test_a_prim_in_another_variant_is_not_missing_for_good(shot):
    stage = Usd.Stage.Open(str(shot))
    assert not stage.GetPrimAtPath("/World/geo/extra")
    assert not chunk_utils.missing_for_good(stage, "/World/geo/extra")


def test_a_typo_under_a_live_prim_is_missing_for_good(shot):
    stage = Usd.Stage.Open(str(shot))
    assert chunk_utils.missing_for_good(stage, "/World/cma")


# --------------------------------------------------------------------------------------
# Saving a network built on an opened file


def test_saving_over_the_opened_file_refuses(shot):
    stage = Usd.Stage.Open(str(shot))
    with Scene(stage) as scene:
        scene.set_output(scene.create.cube(prim_path="/World/box2"))
        with pytest.raises(ValueError, match="/World"):
            scene.save(str(shot))
    assert "Camera" in shot.read_text()


def test_saving_elsewhere_warns_what_is_left_out(shot, tmp_path, caplog):
    stage = Usd.Stage.Open(str(shot))
    with Scene(stage) as scene, caplog.at_level(logging.WARNING):
        scene.set_output(scene.create.cube(prim_path="/World/box2"))
        scene.save(str(tmp_path / "network.usda"))
    assert _warnings(caplog, "leaves out")


def test_saving_a_network_of_its_own_says_nothing(tmp_path, caplog):
    path = tmp_path / "network.usda"
    with Scene(host_name="pytest") as scene, caplog.at_level(logging.WARNING):
        scene.set_output(scene.create.cube(prim_path="/World/box"))
        scene.save(str(path))
    with Scene.open(str(path)) as scene, caplog.at_level(logging.WARNING):
        scene.save(str(path))
    assert not _warnings(caplog, "leaves out")


@pytest.mark.parametrize("lazy", [False, True])
def test_a_param_the_picked_shape_hides_warns(caplog, lazy):
    """`radius` is the tube's; a sphere's is `radius1`. Set on a sphere, it did nothing, silently."""
    with Scene(host_name="pytest", lazy=lazy) as scene, caplog.at_level(logging.WARNING):
        mesh = scene.create.mesh(mesh_type="sphere", prim_path="/World/ball", radius=0.5)
        scene.set(mesh, dimx=4)
    assert len(_warnings(caplog, "does not use 'radius'")) == 1
    assert len(_warnings(caplog, "does not use 'dimx'")) == 1


def test_the_picked_shapes_own_params_do_not_warn(caplog):
    with Scene(host_name="pytest") as scene, caplog.at_level(logging.WARNING):
        mesh = scene.create.mesh(radius=0.5, mesh_type="tube", prim_path="/World/pipe")
        scene.set(mesh, rows=3)
    assert not _warnings(caplog, "does not use")
