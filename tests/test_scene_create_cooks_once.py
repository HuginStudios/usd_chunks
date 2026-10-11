"""scene.create cooks a chunk once, however many float params it is given.

Joining the scene round-trips values through USD's 32-bit floats, so 0.16 came back
0.1599999964. Compared exactly, that read as a change twice over -- place() set each float
again because it "did not stick", and wiring each param to its attribute assigned the
read-back value as an edit -- and every one cooked the chunk once more: ten cooks of a
45k-instance Scatter Points where one would do.
"""

from __future__ import annotations

from usd_chunks import Scene, chunk_cook


def _cooks_of(monkeypatch):
    counted = []
    real = chunk_cook.cook

    def counting(chunk, *args, **kwargs):
        counted.append(chunk.name)
        return real(chunk, *args, **kwargs)

    monkeypatch.setattr(chunk_cook, "cook", counting)
    return counted


def test_float_params_do_not_cook_the_chunk_again(monkeypatch) -> None:
    with Scene() as scene:
        # The scene's first chunk also makes its root context, which cooks once more; a
        # network's every later chunk is what this is about.
        scene.create.transform(primpath_location="/first")
        cooks = _cooks_of(monkeypatch)
        scene.create.preview_surface(mat_path="/look/a", roughness=0.16, metallic=0.8,
                                     opacity=0.15, ior=1.4, diffuse_color=(0.45, 0.01, 0.8),
                                     name="look")
        assert cooks.count("look") == 1, cooks


def test_the_values_are_still_the_ones_given() -> None:
    with Scene() as scene:
        c = scene.create.preview_surface(mat_path="/look/a", roughness=0.16, diffuse_color=(0.45, 0.01, 0.8))
        assert abs(c.get_param("roughness").value - 0.16) < 1e-6
        assert all(abs(a - b) < 1e-6 for a, b in zip(c.get_param("diffuse_color").value, (0.45, 0.01, 0.8)))
