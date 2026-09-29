"""A param's first write after a load keeps the value written, not the one saved.

A param that loads without being bound to its storage attribute -- one a chunk builds
during its own cook, like a Wedge's ranges -- is bound by the first write to it. That
binding read the attribute's saved value back over the one just written, so the first
change after opening a network went nowhere: everything the Wedge drove kept the saved
variant until the next change. `usdchunks run` papered over it by cooking every root a
second time after its overrides.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene
from usd_chunks.chunk_stage import _iter_chunk_params, get_chunk_prim_path


def _saved_but_unbound(scene):
    """A transform whose tx has a value saved on its chunk prim, but no binding yet."""
    node = scene.create.transform()
    tx = node.get_param("tx")
    assert not tx._param_attr_path, "tx should start unbound, at its default"
    prim = scene.get_stage().GetPrimAtPath(Sdf.Path(get_chunk_prim_path(node)))
    # Named the way the stage sync names it, group path included (inputs:translate_grp:tx).
    param_path = next(path for path, param in _iter_chunk_params(node) if param is tx)
    name = "inputs:" + param_path.replace(".", ":")
    attr = prim.CreateAttribute(name, Sdf.ValueTypeNames.Float, custom=False)
    attr.Set(5.0)
    return tx, attr


def test_the_first_write_is_not_undone_by_the_saved_value():
    with Scene() as scene:
        tx, attr = _saved_but_unbound(scene)

        tx.value = 7.25

        assert tx.value == 7.25
        assert attr.Get() == 7.25, "the saved attribute should now hold the new value"


def test_a_double_is_not_rounded_through_the_float_attribute():
    """Reading the attr back after writing it only rounds a double to float32."""
    with Scene() as scene:
        tx, _ = _saved_but_unbound(scene)

        tx.value = 0.1

        assert tx.value == 0.1
