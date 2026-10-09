"""scene.create(name=...) sets the name without cooking the chunk on its defaults.

The name was set before the guard that holds back the cook for the other params, so a
named Preview Surface cooked once with its default Material Path and left an `over` at
/materials/surface in the session layer and its own layer.
"""

from __future__ import annotations

from usd_chunks import Scene


def _layers_with(scene, path):
    stage = scene.get_stage()
    return [layer.GetDisplayName() for layer in stage.GetLayerStack(includeSessionLayers=True)
            if layer.GetPrimAtPath(path)]


def test_named_chunk_never_cooks_on_its_defaults() -> None:
    with Scene() as scene:
        scene.create.preview_surface(mat_path="/look/glass", roughness=0.4, name="glass")
        assert _layers_with(scene, "/materials") == []
        assert scene.get_chunk("glass") is not None
