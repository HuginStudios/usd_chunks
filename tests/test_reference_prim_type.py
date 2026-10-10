"""A Reference or Payload chunk's Prim Type wins even when something weaker already defines the prim.

It only called DefinePrim when the prim was not yet defined, so with an upstream chunk
(or the file) defining the location its layer got a typeless `over` and the chosen type
was silently dropped.
"""

from __future__ import annotations

import pytest

from usd_chunks import Scene

ASSET = '#usda 1.0\n(\n    defaultPrim = "A"\n)\n\ndef "A"\n{\n    def Sphere "s"\n    {\n    }\n}\n'


@pytest.mark.parametrize("kind", ["reference", "payload"])
def test_prim_type_overrides_an_upstream_def(tmp_path, kind) -> None:
    asset = tmp_path / "asset.usda"
    asset.write_text(ASSET)
    with Scene() as scene:
        up = scene.create.prim(primpath_location="/asset", prim_type="Scope")
        ref = getattr(scene.create, kind)(up, file_path=asset.as_posix(), primpath_location="/asset",
                                          prim_type="Xform")
        scene.commit()
        prim = ref.get_stage().GetPrimAtPath("/asset")
        assert prim.GetTypeName() == "Xform"
        assert prim.GetChild("s").IsValid()
