"""Two Preview Surfaces left at the default Material Path each get a material of their own.

Both authored /materials/surface, so the ground and a second mesh shared one material and
the stronger chunk layer's settings won. Editing the weaker one's colour did nothing on
screen until a reopen happened to cook them in the other order.
"""

from __future__ import annotations

from pxr import Gf, UsdShade

from usd_chunks import Scene


def _colour_on(stage, path):
    material, _rel = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(path)).ComputeBoundMaterial()
    shader = UsdShade.Shader.Get(stage, material.GetPath().AppendChild("surface"))
    return material.GetPath(), shader.GetInput("diffuseColor").Get()


def test_two_default_preview_surfaces_do_not_share_a_material() -> None:
    with Scene() as scene:
        stage = scene.get_stage()
        stage.DefinePrim("/ground", "Mesh")
        stage.DefinePrim("/curves", "Mesh")
        ground = scene.create.preview_surface(primpath_location="/ground", diffuse_color=(1, 0, 0))
        curves = scene.create.preview_surface(primpath_location="/curves", diffuse_color=(0, 0, 1))

        assert ground.get_param("mat_path").value != curves.get_param("mat_path").value
        ground_mat, ground_colour = _colour_on(stage, "/ground")
        curves_mat, curves_colour = _colour_on(stage, "/curves")
        assert ground_mat != curves_mat
        assert ground_colour == Gf.Vec3f(1, 0, 0)
        assert curves_colour == Gf.Vec3f(0, 0, 1)


def test_editing_the_first_one_shows_on_its_prim() -> None:
    with Scene() as scene:
        stage = scene.get_stage()
        stage.DefinePrim("/ground", "Mesh")
        stage.DefinePrim("/curves", "Mesh")
        ground = scene.create.preview_surface(primpath_location="/ground")
        scene.create.preview_surface(primpath_location="/curves")

        ground.get_param("diffuse_color").value = (0.0, 1.0, 0.0)

        assert _colour_on(stage, "/ground")[1] == Gf.Vec3f(0, 1, 0)


def test_a_recook_keeps_its_path() -> None:
    with Scene() as scene:
        ground = scene.create.preview_surface(primpath_location="/ground")
        first = ground.get_param("mat_path").value
        ground.get_param("roughness").value = 0.9
        ground.get_param("roughness").value = 0.1
        assert ground.get_param("mat_path").value == first
