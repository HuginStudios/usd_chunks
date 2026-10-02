"""Authoring many prims at once, straight into a layer.

Every Usd API call that authors -- DefinePrim, CreateAttribute, Set -- sends a change
notice, and for each one the stage recomposes and every listener runs: the framework,
usdview's prim tree, the viewport's Hydra scene. A cook that writes thousands of prims
spends nearly all its time there. Measured on 8,000 prims of three attributes: 3.9 s
through the Usd API, 0.2 s through Sdf in one change block -- and the viewport frame after
it 1.6 s against 0.07 s on V-Ray.

Writer puts specs in the edit target's layer at the paths the edit target maps to, so they
land exactly where the Usd API would have put them, a variant context included. Use it
inside one Sdf.ChangeBlock, and do not read back through the stage what was written in the
block: composition catches up when it closes. Reading anything else is fine.

    writer = sdf_authoring.Writer(stage)
    with Sdf.ChangeBlock():
        for i, point in enumerate(points):
            spec = writer.define(root.AppendChild(f"P{i}"), "Cube")
            sdf_authoring.attribute(spec, "size", Sdf.ValueTypeNames.Double, 0.5)
            sdf_authoring.transform(spec, Gf.Matrix4d().SetTranslate(point))
"""

from __future__ import annotations

from typing import Any

from pxr import Gf, Sdf, Usd


class Writer:
    """Specs in a stage's current edit target, at the paths it maps to.

    The edit target is read once, when the writer is made: make one inside the
    author_in_variant_contexts loop, not before it.
    """

    def __init__(self, stage: Usd.Stage):
        self._target = stage.GetEditTarget()
        self.layer: Sdf.Layer = self._target.GetLayer()

    def spec_path(self, path: Sdf.Path | str) -> Sdf.Path:
        """Where path's spec lives in the layer -- inside a variant, under a variant target."""
        return self._target.MapToSpecPath(Sdf.Path(str(path)))

    def define(self, path: Sdf.Path | str, type_name: str = "") -> Sdf.PrimSpec:
        """A `def` at path, typed type_name -- Stage.DefinePrim. Missing parents become `over`s."""
        spec = Sdf.CreatePrimInLayer(self.layer, self.spec_path(path))
        spec.specifier = Sdf.SpecifierDef
        if type_name:
            spec.typeName = type_name
        return spec

    def over(self, path: Sdf.Path | str) -> Sdf.PrimSpec:
        """The spec at path, an `over` if there was none -- what authoring a property on a
        prim this layer does not define yet makes."""
        return Sdf.CreatePrimInLayer(self.layer, self.spec_path(path))

    def target_path(self, path: Sdf.Path | str) -> Sdf.Path:
        """path as a reference or relationship target written from this layer.

        Mapped like a spec path, then with its variant selections stripped, which a target
        may not carry -- what UsdReferences and UsdRelationship do with one.
        """
        return self.spec_path(path).StripAllVariantSelections()

    def reference(self, spec: Sdf.PrimSpec, path: Sdf.Path | str) -> None:
        """An internal reference to path, prepended -- Prim.GetReferences().AddInternalReference."""
        spec.referenceList.Prepend(Sdf.Reference(primPath=self.target_path(path)))


def attribute(spec: Sdf.PrimSpec, name: str, type_name: Sdf.ValueTypeName, value: Any = None, *,
              uniform: bool = False, custom: bool = False,
              interpolation: str | None = None) -> Sdf.AttributeSpec:
    """The attribute name on spec, with value as its default when one is given.

    Not custom by default: that is what a schema's Create*Attr makes. Prim.CreateAttribute
    makes custom ones -- pass custom=True to match it. uniform for the attributes a schema
    declares uniform (subdivisionScheme, xformOpOrder, a GeomSubset's elementType).
    """
    attr = spec.attributes[name] if name in spec.attributes else Sdf.AttributeSpec(
        spec, name, type_name, Sdf.VariabilityUniform if uniform else Sdf.VariabilityVarying, custom)
    if interpolation:
        attr.SetInfo("interpolation", interpolation)
    if value is not None:
        attr.default = value
    return attr


def primvar(spec: Sdf.PrimSpec, name: str, type_name: Sdf.ValueTypeName, value: Any,
            interpolation: str) -> Sdf.AttributeSpec:
    """primvars:<name> -- PrimvarsAPI.CreatePrimvar(name, type, interpolation).Set(value)."""
    return attribute(spec, f"primvars:{name}", type_name, value, interpolation=interpolation)


def transform(spec: Sdf.PrimSpec, matrix: Gf.Matrix4d) -> None:
    """A single xformOp:transform holding matrix -- Xformable.AddTransformOp().Set(matrix)."""
    attribute(spec, "xformOp:transform", Sdf.ValueTypeNames.Matrix4d, matrix)
    attribute(spec, "xformOpOrder", Sdf.ValueTypeNames.TokenArray, ["xformOp:transform"], uniform=True)
