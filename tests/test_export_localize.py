"""Tests for Export USD gathering an exported asset's dependencies beside it.

FlattenLayerStack composes the layer *stack*, but a reference or payload is a composition
arc that survives flattening untouched — so an exported file still points at whatever it
referenced, by a path anchored to the network rather than to the output. That resolves to
nothing as soon as the output lives somewhere else, which is exactly what converting an
asset library produces. These cover the gather-and-rewrite that fixes it.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from pxr import Sdf, Usd

# scripts/ only reaches sys.path once the registry has initialized, which these tests have
# no reason to do — they exercise the export helper directly, with no chunks involved.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import export_usd  # noqa: E402  (needs the path set up above)


def _asset(path, prim_name, geom="Sphere", reference=None):
    """Write a tiny standalone asset, optionally referencing another one.

    A reference goes onto its own child prim, the way the Reference chunk authors it —
    putting it on the prim that already owns "geo" would have the local opinion win over
    the referenced one and hide whether the reference resolved at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    nested = (
        f'\n    def "imported" (\n        prepend references = @{reference}@\n    )\n    {{\n    }}\n'
        if reference
        else ""
    )
    path.write_text(
        f'#usda 1.0\n(\n    defaultPrim = "{prim_name}"\n)\n\n'
        f'def Xform "{prim_name}"\n{{\n    def {geom} "geo" {{}}\n{nested}}}\n'
    )
    return path


def _flat_referencing(target: str) -> Sdf.Layer:
    """An in-memory layer standing in for the flattened stage about to be exported."""
    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.ImportFromString(
        '#usda 1.0\n\n'
        f'def Xform "Asset" (\n    prepend references = @{target}@\n)\n{{\n}}\n'
    )
    return layer


def test_dependency_is_copied_and_the_path_rewritten(tmp_path):
    _asset(tmp_path / "assets" / "crate.usda", "crate", "Cube")
    flat = _flat_referencing("./assets/crate.usda")

    gathered = export_usd._localize_dependencies(flat, str(tmp_path), str(tmp_path / "out" / "crate.usda"))

    assert len(gathered) == 1
    assert (tmp_path / "out" / "deps" / "crate.usda").is_file()
    assert "@./deps/crate.usda@" in flat.ExportToString()
    # The source library is left exactly as it was — this gathers copies, it does not move.
    assert (tmp_path / "assets" / "crate.usda").is_file()


def test_exported_asset_resolves_from_an_unrelated_directory(tmp_path):
    """The point of the whole exercise: hand the output folder to someone else and it works."""
    _asset(tmp_path / "assets" / "crate.usda", "crate", "Cube")
    flat = _flat_referencing("./assets/crate.usda")
    out = tmp_path / "out" / "crate.usda"

    export_usd._localize_dependencies(flat, str(tmp_path), str(out))
    flat.Export(str(out))

    shipped = tmp_path / "elsewhere" / "shipped"
    shipped.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(out.parent, shipped)

    stage = Usd.Stage.Open(str(shipped / "crate.usda"))
    assert "Cube" in [p.GetTypeName() for p in stage.Traverse()]


def test_recursion_reaches_a_dependency_of_a_dependency(tmp_path):
    """A copied layer's own references would dangle one level down without this."""
    _asset(tmp_path / "lib" / "parts" / "bolt.usda", "bolt", "Capsule")
    _asset(tmp_path / "lib" / "machine.usda", "machine", "Cube", reference="./parts/bolt.usda")
    flat = _flat_referencing("./lib/machine.usda")
    out = tmp_path / "out" / "machine.usda"

    gathered = export_usd._localize_dependencies(flat, str(tmp_path), str(out))
    flat.Export(str(out))

    assert len(gathered) == 2
    assert (tmp_path / "out" / "deps" / "bolt.usda").is_file()
    # Flat deps folder, so a copied layer refers to its siblings as ./name, not ./deps/name.
    copied = Sdf.Layer.FindOrOpen(str(tmp_path / "out" / "deps" / "machine.usda"))
    assert "@./bolt.usda@" in copied.ExportToString()

    stage = Usd.Stage.Open(str(out))
    assert "Capsule" in [p.GetTypeName() for p in stage.Traverse()]


def test_same_basename_from_different_folders_does_not_collide(tmp_path):
    _asset(tmp_path / "a" / "part.usda", "part_a", "Cube")
    _asset(tmp_path / "b" / "part.usda", "part_b", "Sphere")

    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.ImportFromString(
        '#usda 1.0\n\n'
        'def Xform "A" (\n    prepend references = @./a/part.usda@\n)\n{\n}\n\n'
        'def Xform "B" (\n    prepend references = @./b/part.usda@\n)\n{\n}\n'
    )
    out = tmp_path / "out" / "both.usda"

    gathered = export_usd._localize_dependencies(layer, str(tmp_path), str(out))
    layer.Export(str(out))

    assert len(gathered) == 2
    assert (tmp_path / "out" / "deps" / "part.usda").is_file()
    assert (tmp_path / "out" / "deps" / "part_2.usda").is_file()

    stage = Usd.Stage.Open(str(out))
    kinds = sorted(p.GetTypeName() for p in stage.Traverse())
    assert "Cube" in kinds and "Sphere" in kinds


def test_one_source_referenced_twice_is_copied_once(tmp_path):
    _asset(tmp_path / "assets" / "shared.usda", "shared", "Cube")
    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.ImportFromString(
        '#usda 1.0\n\n'
        'def Xform "A" (\n    prepend references = @./assets/shared.usda@\n)\n{\n}\n\n'
        'def Xform "B" (\n    prepend references = @./assets/shared.usda@\n)\n{\n}\n'
    )

    gathered = export_usd._localize_dependencies(layer, str(tmp_path), str(tmp_path / "out" / "x.usda"))

    assert len(gathered) == 1
    assert not (tmp_path / "out" / "deps" / "shared_2.usda").exists()


def test_unresolvable_path_is_left_exactly_as_authored(tmp_path):
    """Rewriting a path that resolves to nothing would only move the breakage somewhere
    harder to spot."""
    flat = _flat_referencing("./missing/ghost.usda")

    gathered = export_usd._localize_dependencies(flat, str(tmp_path), str(tmp_path / "out" / "x.usda"))

    assert gathered == {}
    assert "@./missing/ghost.usda@" in flat.ExportToString()
    assert not (tmp_path / "out" / "deps").exists()


def test_no_dependencies_creates_no_deps_folder(tmp_path):
    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.ImportFromString('#usda 1.0\n\ndef Xform "Asset"\n{\n}\n')

    assert export_usd._localize_dependencies(layer, str(tmp_path), str(tmp_path / "out" / "x.usda")) == {}
    assert not (tmp_path / "out" / "deps").exists()
