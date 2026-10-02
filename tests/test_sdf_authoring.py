"""sdf_authoring writes the same specs the Usd API would, in a fraction of the notices."""

from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import sdf_authoring


def _by_usd(stage):
    xform = UsdGeom.Xform.Define(stage, "/Root/Part")
    xform.AddTransformOp().Set(Gf.Matrix4d().SetTranslate(Gf.Vec3d(1, 2, 3)))
    prim = xform.GetPrim()
    prim.GetReferences().AddInternalReference("/Root/Proto")
    prim.SetInstanceable(True)
    UsdGeom.PrimvarsAPI(prim).CreatePrimvar(
        "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.constant
    ).Set(Vt.Vec3fArray([Gf.Vec3f(1, 0, 0)]))
    mesh = UsdGeom.Mesh.Define(stage, "/Root/Proto")
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)


def _by_sdf(stage):
    writer = sdf_authoring.Writer(stage)
    with Sdf.ChangeBlock():
        spec = writer.define("/Root/Part", "Xform")
        sdf_authoring.transform(spec, Gf.Matrix4d().SetTranslate(Gf.Vec3d(1, 2, 3)))
        writer.reference(spec, "/Root/Proto")
        spec.instanceable = True
        sdf_authoring.primvar(spec, "displayColor", Sdf.ValueTypeNames.Color3fArray,
                              Vt.Vec3fArray([Gf.Vec3f(1, 0, 0)]), UsdGeom.Tokens.constant)
        mesh = writer.define("/Root/Proto", "Mesh")
        sdf_authoring.attribute(mesh, "subdivisionScheme", Sdf.ValueTypeNames.Token, UsdGeom.Tokens.none,
                                uniform=True)


def _authored(author, variant: bool) -> str:
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    if variant:
        vset = root.GetVariantSets().AddVariantSet("look")
        vset.AddVariant("a")
        vset.SetVariantSelection("a")
        with vset.GetVariantEditContext():
            author(stage)
    else:
        author(stage)
    return stage.GetRootLayer().ExportToString()


def test_the_writer_authors_what_the_usd_api_does():
    assert _authored(_by_sdf, variant=False) == _authored(_by_usd, variant=False)


def test_the_writer_authors_inside_a_variant_edit_target():
    assert _authored(_by_sdf, variant=True) == _authored(_by_usd, variant=True)
