"""usd_chunks.sweep: stitching variants into frames, and deciding when a sweep can batch."""

from __future__ import annotations

from pxr import Sdf, Usd

from usd_chunks import sweep


def _variant(tmp_path, index, tx, *, extra_prim=False, binding="/mat_a"):
    layer = Sdf.Layer.CreateNew(str(tmp_path / f"v{index}.usda"))
    stage = Usd.Stage.Open(layer)
    prim = stage.DefinePrim("/World/box", "Xform")
    prim.CreateAttribute("tx", Sdf.ValueTypeNames.Double).Set(tx)
    prim.CreateAttribute("size", Sdf.ValueTypeNames.Double).Set(2.0)
    prim.CreateRelationship("material:binding").SetTargets([binding])
    if extra_prim:
        stage.DefinePrim("/World/extra")
    layer.Save()
    return layer.identifier


def test_what_varies_becomes_one_sample_per_variant(tmp_path):
    paths = {i: _variant(tmp_path, i, float(i) * 10) for i in (3, 5, 9)}
    out = str(tmp_path / "stitched.usda")

    assert sweep.stitch(paths, out) == []

    stage = Usd.Stage.Open(out)
    tx = stage.GetAttributeAtPath("/World/box.tx")
    assert [tx.Get(i) for i in (3, 5, 9)] == [30.0, 50.0, 90.0]
    assert stage.GetAttributeAtPath("/World/box.size").GetNumTimeSamples() == 0, (
        "a value every variant shares stays a plain default"
    )
    assert (stage.GetStartTimeCode(), stage.GetEndTimeCode()) == (3, 9)


def test_what_cannot_vary_over_time_is_refused(tmp_path):
    paths = {
        0: _variant(tmp_path, 0, 1.0),
        1: _variant(tmp_path, 1, 2.0, binding="/mat_b", extra_prim=True),
    }
    out = tmp_path / "stitched.usda"

    problems = sweep.stitch(paths, str(out))

    assert any("material:binding" in p for p in problems), problems
    assert any("/World/extra" in p and "some variants only" in p for p in problems), problems
    assert not out.exists(), "nothing is written when the variants cannot be stitched"


class _Param:
    def __init__(self, raw):
        self.raw_value = raw


class _Module:
    @staticmethod
    def commit(node):
        pass


class _Chunk:
    def __init__(self, name, frames=None, commits=True):
        self.name = name
        self._module = _Module if commits else None
        self._frames = _Param(frames) if frames is not None else None

    def get_param(self, name):
        return self._frames if name == "frames" else None

    def _module_hook(self, name):
        return getattr(self._module, name, None)

    def is_blocked(self):
        return False


def test_commits_that_follow_the_index_are_batched():
    render = _Chunk("Render", "@Wedge.current_index@")
    consumers, reason = sweep.frame_consumers([render, _Chunk("Mesh", commits=False), render], "Wedge")
    assert reason is None
    assert consumers == [render], "a chunk reached twice is still committed once"


def test_a_commit_per_variant_refuses_batching():
    consumers, reason = sweep.frame_consumers([_Chunk("Export USD")], "Wedge")
    assert consumers == []
    assert "Export USD" in reason
