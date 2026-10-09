"""A param built from a relationship reads its targets and writes them back.

Reported via coverage: show_attribute builds a LineEdit for every relationship on a prim
(material:binding, proxyPrim, ...) and the field showed the targets, but typing new ones
changed nothing. The write lived in update_usd_prop behind `isinstance(notice.data,
Usd.Relationship)`, and the notice carries the value's AttributeSender, never the property.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture()
def rel():
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)
    for path in ("/A", "/B"):
        stage.DefinePrim(path)
    rel = stage.DefinePrim("/X").CreateRelationship("material:binding")
    rel.SetTargets([Sdf.Path("/A")])
    return rel


def test_the_param_is_named_after_the_relationship_and_reads_its_targets(rel):
    param = chunk.param_from_rel(rel)

    assert isinstance(param, chunk.LineEdit)
    assert param.name == "binding"
    assert param.value == "/A"


def test_a_new_target_is_written(rel):
    param = chunk.param_from_rel(rel)

    param.value = "/B"

    assert rel.GetTargets() == [Sdf.Path("/B")]


def test_several_targets_are_comma_separated(rel):
    param = chunk.param_from_rel(rel)

    param.value = "/A, /B"

    assert rel.GetTargets() == [Sdf.Path("/A"), Sdf.Path("/B")]


def test_text_that_is_not_a_path_leaves_the_targets_alone(rel):
    param = chunk.param_from_rel(rel)

    param.value = "not a path!!"

    assert rel.GetTargets() == [Sdf.Path("/A")]


def test_emptying_the_field_clears_the_targets(rel):
    param = chunk.param_from_rel(rel)

    param.value = ""

    assert rel.GetTargets() == []


def test_an_explicit_name_is_kept(rel):
    param = chunk.param_from_rel(rel, "material")

    assert param.name == "material"
    assert param.value == "/A"


def test_show_attribute_offers_the_relationship(rel):
    group = chunk.Group("props")

    chunk.show_attribute(rel.GetPrim(), group)

    names = [p.name for p in group.get_params()]
    assert "binding" in names
