"""An exported network names its one root prim as the default prim.

A file referenced without a prim path composes nothing unless it names one, so every asset a
network delivered had to be patched by hand before a scene could reference it.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks.chunk_stage import strip_chunks

_CHUNKS = """
def Scope "chunks"
{
    def Chunk "Ref"
    {
    }
}
"""


def _layer(*roots, default=None):
    """A layer holding the chunk scope and these root Xforms."""
    header = f'#usda 1.0\n( defaultPrim = "{default}" )\n' if default else "#usda 1.0\n"
    body = "".join(f'def Xform "{name}"\n{{\n}}\n' for name in roots)
    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.ImportFromString(header + _CHUNKS + body)
    return layer


def test_the_one_root_left_becomes_the_default_prim():
    layer = _layer("Asset")
    strip_chunks(layer)
    assert layer.defaultPrim == "Asset"


def test_a_default_prim_naming_a_stripped_chunk_is_replaced():
    layer = _layer("Asset", default="chunks")
    strip_chunks(layer)
    assert layer.defaultPrim == "Asset"


def test_several_roots_are_left_to_say_which_they_mean():
    layer = _layer("A", "B")
    strip_chunks(layer)
    assert not layer.HasDefaultPrim()


def test_a_default_prim_still_there_is_kept():
    layer = _layer("A", "B", default="B")
    strip_chunks(layer)
    assert layer.defaultPrim == "B"
