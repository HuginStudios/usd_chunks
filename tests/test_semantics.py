"""Tests that semantics.py authors UsdSemantics labels across every prim matched by its
prim pattern, that re-cooking replaces rather than accumulates labels, and that the
authored data is consumable by the standard UsdSemantics.LabelsQuery inheritance API.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Usd, UsdSemantics

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as init_chunk_usd


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _exec(mod, node):
    """Mirror the real auto-execute chain (chunk_notice.py): run module.execute() inside
    Usd.EditContext(stage, node.get_edit_context()) so each chunk clears only its own
    anonymous layer instead of the ambient root edit target."""
    stage = node.get_stage()
    ctx = node.get_edit_context()
    if stage and ctx:
        with Usd.EditContext(stage, ctx):
            mod.execute(node)
    else:
        mod.execute(node)


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.DefinePrim("/geo/a", "Xform")
        stage.DefinePrim("/geo/b", "Xform")
        stage.DefinePrim("/geo/c", "Xform")

    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    init_chunk_usd(stage, sc)
    yield sc


class TestSemanticLabels:
    def setup_method(self):
        self.mod = importlib.import_module("chunks.usd.semantics")

    def _make(self, name):
        c = self.mod.main()
        c.name = name
        c._config = ChunkConfig(module="semantics")
        return c

    def _set_row(self, c, index, taxonomy, values):
        label_list = c.get_param("labels")
        if label_list.size <= index:
            label_list.size = index + 1
        label_list.get_indexed_param(label_list.get_param("taxonomy"), index).value = taxonomy
        label_list.get_indexed_param(label_list.get_param("label_values"), index).value = values

    def test_labels_applied_to_every_matched_prim(self, scene):
        c = self._make("Sem1")
        scene.add_chunk(c)
        c.get_param("primpath_location").value = "/geo/*"
        self._set_row(c, 0, "category", "chair, furniture")

        _exec(self.mod, c)

        stage = scene.get_stage()
        for p in ("/geo/a", "/geo/b", "/geo/c"):
            prim = stage.GetPrimAtPath(p)
            assert "SemanticsLabelsAPI:category" in prim.GetAppliedSchemas()
            labels = UsdSemantics.LabelsAPI.Get(prim, "category").GetLabelsAttr().Get()
            assert list(labels) == ["chair", "furniture"]

    def test_multiple_taxonomies_on_one_prim(self, scene):
        c = self._make("Sem2")
        scene.add_chunk(c)
        c.get_param("primpath_location").value = "/geo/a"
        self._set_row(c, 0, "category", "chair")
        self._set_row(c, 1, "style", "modern, danish")

        _exec(self.mod, c)

        prim = scene.get_stage().GetPrimAtPath("/geo/a")
        applied = prim.GetAppliedSchemas()
        assert "SemanticsLabelsAPI:category" in applied
        assert "SemanticsLabelsAPI:style" in applied
        assert list(UsdSemantics.LabelsAPI.Get(prim, "category").GetLabelsAttr().Get()) == ["chair"]
        assert list(UsdSemantics.LabelsAPI.Get(prim, "style").GetLabelsAttr().Get()) == [
            "modern",
            "danish",
        ]

    def test_recook_replaces_labels(self, scene):
        c = self._make("Sem3")
        scene.add_chunk(c)
        c.get_param("primpath_location").value = "/geo/a"
        self._set_row(c, 0, "category", "chair, furniture")
        _exec(self.mod, c)

        self._set_row(c, 0, "category", "table")
        _exec(self.mod, c)

        prim = scene.get_stage().GetPrimAtPath("/geo/a")
        assert list(UsdSemantics.LabelsAPI.Get(prim, "category").GetLabelsAttr().Get()) == ["table"]

    def test_invalid_taxonomy_is_skipped(self, scene):
        c = self._make("Sem4")
        scene.add_chunk(c)
        c.get_param("primpath_location").value = "/geo/a"
        self._set_row(c, 0, "2 bad", "nope")
        self._set_row(c, 1, "category", "chair")

        _exec(self.mod, c)

        prim = scene.get_stage().GetPrimAtPath("/geo/a")
        applied = prim.GetAppliedSchemas()
        assert applied == ["SemanticsLabelsAPI:category"]

    def test_labels_inherited_by_descendants(self, scene):
        c = self._make("Sem5")
        scene.add_chunk(c)
        c.get_param("primpath_location").value = "/geo"
        self._set_row(c, 0, "category", "furniture")

        _exec(self.mod, c)

        stage = scene.get_stage()
        query = UsdSemantics.LabelsQuery("category", Usd.TimeCode.Default())
        child = stage.GetPrimAtPath("/geo/a")
        assert list(query.ComputeUniqueDirectLabels(child)) == []
        assert list(query.ComputeUniqueInheritedLabels(child)) == ["furniture"]
