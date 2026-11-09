"""Prim with type Class makes an abstract `class` prim, not a `def` typed "Class".

DefinePrim(path, "Class") wrote `def Class "base"`: a concrete prim of an unknown type,
rendered and traversed like any other, where a class prim for Inherit and Specialize to
point at is a `class` spec with no type.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks.chunk_script import Scene


@pytest.fixture
def scene():
    s = Scene(Usd.Stage.CreateInMemory(), "pytest")
    yield s
    s.close()


def test_class_makes_an_abstract_class_prim(scene):
    node = scene.add("prim", primpath_location="/_base", prim_type="Class")

    prim = scene.get_stage().GetPrimAtPath("/_base")
    assert prim.IsAbstract()
    assert prim.GetSpecifier() == Sdf.SpecifierClass
    assert not prim.GetTypeName()
    spec = node.get_edit_context().GetPrimAtPath("/_base")  # in the chunk's own layer
    assert (spec.specifier, spec.typeName) == (Sdf.SpecifierClass, "")


def test_xform_is_still_a_def(scene):
    scene.add("prim", primpath_location="/geo", prim_type="Xform")

    prim = scene.get_stage().GetPrimAtPath("/geo")
    assert (prim.GetSpecifier(), prim.GetTypeName()) == (Sdf.SpecifierDef, "Xform")
