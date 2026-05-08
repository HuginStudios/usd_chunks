"""
Tests for the 'Show Layer Edits' Sdf copy logic.

The fix in graph_widget._show_layer_edits calls Sdf.CreatePrimInLayer before
Sdf.CopySpec so that parent prim specs exist in the anonymous destination layer.
Without this, CopySpec raises when the path is nested (e.g. /Root/Child/Leaf).
"""

from pxr import Sdf, Usd


def test_copy_nested_prim_spec_does_not_raise(tmp_path):
    """CreatePrimInLayer + CopySpec must not raise for deeply nested paths."""
    # Build a source layer with a 3-deep prim hierarchy
    src = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(src, Sdf.Path("/Root/Child/Leaf"))

    dst = Sdf.Layer.CreateAnonymous()
    path = Sdf.Path("/Root/Child/Leaf")

    # This is the fixed pattern from _show_layer_edits
    assert src.GetPrimAtPath(path)
    Sdf.CreatePrimInLayer(dst, path)          # ensures parents exist in dst
    Sdf.CopySpec(src, path, dst, path)        # was raising before the fix

    raw = dst.ExportToString()
    assert "Leaf" in raw


def test_copy_shallow_prim_spec_still_works(tmp_path):
    """The fix does not break single-level paths (no parents needed)."""
    src = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(src, Sdf.Path("/MyPrim"))

    dst = Sdf.Layer.CreateAnonymous()
    path = Sdf.Path("/MyPrim")

    Sdf.CreatePrimInLayer(dst, path)
    Sdf.CopySpec(src, path, dst, path)

    raw = dst.ExportToString()
    assert "MyPrim" in raw


def test_copy_skips_missing_prim_gracefully(tmp_path):
    """Paths absent from the source layer are silently skipped (no crash)."""
    src = Sdf.Layer.CreateAnonymous()          # empty — nothing defined
    dst = Sdf.Layer.CreateAnonymous()
    path = Sdf.Path("/DoesNotExist/Child")

    # Guard matches _show_layer_edits: skip if not in source
    if src.GetPrimAtPath(path):               # False — so block is skipped
        Sdf.CreatePrimInLayer(dst, path)
        Sdf.CopySpec(src, path, dst, path)

    raw = dst.ExportToString()
    assert "DoesNotExist" not in raw


def test_copy_spec_shows_prim_attributes(tmp_path):
    """Copied spec preserves authored attributes from the source layer."""
    src = Sdf.Layer.CreateAnonymous()
    prim_spec = Sdf.CreatePrimInLayer(src, Sdf.Path("/Root/MyMesh"))
    Sdf.AttributeSpec(prim_spec, "myAttr", Sdf.ValueTypeNames.Float)

    dst = Sdf.Layer.CreateAnonymous()
    path = Sdf.Path("/Root/MyMesh")

    Sdf.CreatePrimInLayer(dst, path)
    Sdf.CopySpec(src, path, dst, path)

    raw = dst.ExportToString()
    assert "myAttr" in raw
