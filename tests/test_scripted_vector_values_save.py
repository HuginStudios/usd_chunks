"""A colour or vector given to scene.create is saved with the network.

Plain numbers and strings given to create() were saved, but a ColorPicker or Vector value
only lived on the param: a Preview Surface's colour, a UV Transform's tiling and a UV
Texture's scale all came back at their defaults when the saved scene was opened again.
"""

from __future__ import annotations

import pytest

from usd_chunks import Scene


def _reopened(path, name, param):
    with Scene.open(str(path)) as scene:
        chunk = next(c for c in scene.get_chunks() if c.name == name)
        return chunk.get_param(param).value


def test_a_colour_given_to_create_is_saved(tmp_path) -> None:
    path = tmp_path / "net.usda"
    with Scene() as scene:
        scene.create.preview_surface(name="Red", mat_path="/m/red", diffuse_color=(0.9, 0.1, 0.1))
        scene.save(str(path))

    assert _reopened(path, "Red", "diffuse_color") == pytest.approx((0.9, 0.1, 0.1))


def test_a_vector_given_to_create_is_saved(tmp_path) -> None:
    path = tmp_path / "net.usda"
    with Scene() as scene:
        scene.create.uv_transform(name="Tile", primpath_location="/m/tile", scale=(0.25, 0.5))
        scene.save(str(path))

    assert _reopened(path, "Tile", "scale") == pytest.approx((0.25, 0.5))


def test_a_vector_left_at_its_default_is_not_written(tmp_path) -> None:
    path = tmp_path / "net.usda"
    with Scene() as scene:
        scene.create.uv_transform(name="Tile", primpath_location="/m/tile")
        scene.save(str(path))

    assert "inputs:scale" not in path.read_text()
