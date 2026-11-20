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


@pytest.mark.parametrize("prim_type", ["PhysicsScene", "PhysicsFixedJoint", "RenderSettings"])
def test_physics_and_render_types_are_offered(scene, prim_type):
    """A PhysicsScene, a joint or RenderSettings from a Prim. The menu listed only Xform,
    Scope and Class, so a script asking for a PhysicsScene got an Xform."""
    node = scene.add("prim", primpath_location="/World/thing", prim_type=prim_type)

    assert scene.get_stage().GetPrimAtPath("/World/thing").GetTypeName() == prim_type
    assert node.get_param("prim_type").value == prim_type
