"""A masked cook's stage holds its own chunk prim and those above it, not every chunk's.

Every chunk prim was in every mask: in a set of 300 props, a cook of one Transform
populated 900 of them, a third of opening its stage.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, UsdGeom

from usd_chunks import Scene, skip_cook


@pytest.fixture
def masks(monkeypatch):
    """The mask of each cook stage opened."""
    seen = []
    real = skip_cook._open_cook_stage

    def recording(main, mask, hidden=(), opened=None):
        seen.append(set(mask))
        return real(main, mask, hidden, opened)

    monkeypatch.setattr(skip_cook, "_open_cook_stage", recording)
    return seen


def test_a_cook_stage_leaves_out_the_chunks_it_is_not_wired_to(masks) -> None:
    with Scene() as scene:
        a = scene.create.prim(primpath_location="/World/a")
        b = scene.create.prim(primpath_location="/World/b")
        moved = scene.create.transform(a, primpath_location="/World/a", tx=1.0)
        moved.get_param("tx").value = 2.0  # the first cook is on the whole stage
        masks.clear()
        moved.get_param("tx").value = 3.0
        assert masks, "the edit cooked on a masked stage"
        mask = masks[-1]

        def populated(c) -> bool:
            path = Sdf.Path(c._exec_prim_path)
            return any(path.HasPrefix(p) for p in mask)

        assert populated(moved) and populated(a)
        assert not populated(b), (sorted(map(str, mask)), b._exec_prim_path)
        prim = scene.get_stage().GetPrimAtPath("/World/a")
        assert UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation()[0] == pytest.approx(3.0)
