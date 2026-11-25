"""A param takes a numpy scalar as the plain number it holds.

Values a script works out come out of numpy arrays (`points[:, 0].max()` is an np.float64),
and setting one raised `ValueError: Value of 4.43 is not supported ... numpy.float64`, so
every value had to be cast to float first.
"""
from __future__ import annotations

import numpy as np
import pytest
from pxr import Usd

from usd_chunks.chunk_script import Scene


@pytest.fixture
def scene():
    s = Scene(Usd.Stage.CreateInMemory(), "pytest")
    yield s
    s.close()


@pytest.mark.parametrize("value", [np.float64(4.43), np.float32(2.5), np.int64(3), np.int32(-2)])
def test_a_float_param_takes_a_numpy_scalar(scene, value):
    node = scene.add("transform", tx=value)
    assert node.get_param("tx").value == pytest.approx(float(value))
    assert type(node.get_param("tx").value) in (int, float)
    node.get_param("tx").value = value * 2
    assert node.get_param("tx").value == pytest.approx(float(value) * 2)


def test_an_int_and_a_bool_param_take_numpy_scalars(scene):
    node = scene.add("mesh", mesh_type="cube", prim_path="/cube", dimx=np.int64(4), caps=np.bool_(True))
    assert node.get_param("dimx").value == 4 and type(node.get_param("dimx").value) is int
    assert node.get_param("caps").value is True


def test_a_numpy_array_is_still_refused(scene):
    node = scene.add("transform")
    with pytest.raises(ValueError):
        node.get_param("tx").value = np.array([1.0, 2.0])
