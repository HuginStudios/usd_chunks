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


def test_a_list_row_given_to_create_is_set_before_the_first_cook(monkeypatch) -> None:
    """`list.row.param` names one row's param, so a Scatter Points' prototype is in place
    when it first cooks instead of costing a second cook after."""
    with Scene() as scene:
        scene.create.transform(primpath_location="/first")
        cooks = _cooks_of(monkeypatch)
        s = scene.create.scatter_points(name="scatter", inst_path="/S",
                                        **{"prototypes.0.proto_path": "/assets/rock"})
        assert s.get_param("prototypes").get_indexed_param("proto_path", 0).value == "/assets/rock"
        assert cooks.count("scatter") == 1, cooks


def test_a_row_that_does_not_exist_still_raises() -> None:
    import pytest
    with Scene() as scene:
        with pytest.raises(KeyError, match="prototypes.7.proto_path"):
            scene.create.scatter_points(**{"prototypes.7.proto_path": "/x"})
