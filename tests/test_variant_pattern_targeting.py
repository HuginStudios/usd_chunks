"""Tests that set_variant.py and variant.py -- the two variant-authoring chunks that edit
EXISTING/overridable prims rather than define a single new one -- apply across every prim
matched by a Houdini-style glob or %name collection reference, not just one literal path.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender


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
    from usd_chunks.chunk_stage import initialize as init_chunk_usd
    init_chunk_usd(stage, sc)
    yield sc


class TestVariantPatternTargeting:
    def setup_method(self):
        self.mod = importlib.import_module("scripts.usd.variant")

    def _make(self, name):
        c = self.mod.main()
        c.name = name
        c._config = ChunkConfig(module="variant")
        return c

    def test_add_variants_applies_to_every_matched_prim(self, scene):
        c = self._make("Vars1")
        scene.add_chunk(c)
        c.get_param("primpath_location").value = "/geo/*"

        vs_list = c.get_param("variant_entries")
        vs_list.get_indexed_param(vs_list.get_param("vs_name"), 0).value = "LOD"
        vs_list.get_indexed_param(vs_list.get_param("v_names"), 0).value = "high, low"
        vs_list.get_indexed_param(vs_list.get_param("v_default"), 0).value = "high"

        _exec(self.mod, c)

        stage = scene.get_stage()
        for p in ("/geo/a", "/geo/b", "/geo/c"):
            prim = stage.GetPrimAtPath(p)
            vset = prim.GetVariantSets().GetVariantSet("LOD")
            assert set(vset.GetVariantNames()) == {"high", "low"}
            assert vset.GetVariantSelection() == "high"


class TestSetVariantPatternTargeting:
    def setup_method(self):
        self.mod = importlib.import_module("scripts.usd.set_variant")

    def _make(self, name):
        c = self.mod.main()
        c.name = name
        c._config = ChunkConfig(module="set_variant")
        return c

    def test_set_variant_applies_selection_to_every_matched_prim(self, scene):
        # Author the LOD variant set (no selection yet) directly in the session layer --
        # deliberately not via the Add Variants chunk, so this test isn't entangled with
        # cross-chunk layer-strength ordering (unrelated to what's being verified here:
        # that Set Variant loops over every prim matched by its own pattern). Leaving the
        # selection unset avoids relying on the chunk layer out-competing a stronger,
        # pre-existing selection opinion elsewhere in the layer stack -- the session layer
        # is always stronger than a chunk's own anonymous layer, same as the root layer
        # outranks its sublayers, so this test only asserts what the chunk itself authors.
        stage = scene.get_stage()
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            for p in ("/geo/a", "/geo/b", "/geo/c"):
                vset = stage.GetPrimAtPath(p).GetVariantSets().AddVariantSet("LOD")
                vset.AddVariant("high")
                vset.AddVariant("low")

        # Now select "low" on every matched prim in one Set Variant chunk.
        set_var = self._make("SetVar1")
        scene.add_chunk(set_var)
        set_var.get_param("primpath_location").value = "/geo/*"
        set_var.get_param("variant_set").value = "LOD"
        set_var.get_param("variant_selection").value = "low"
        _exec(self.mod, set_var)

        stage = scene.get_stage()
        for p in ("/geo/a", "/geo/b", "/geo/c"):
            prim = stage.GetPrimAtPath(p)
            vset = prim.GetVariantSets().GetVariantSet("LOD")
            assert vset.GetVariantSelection() == "low"
