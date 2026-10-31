"""The scan for new chunks walks each resynced subtree once, however many roots nest in it.

A cook that authors a scatter's prototypes resyncs hundreds of roots, most inside another.
Walking every root's subtree found the same prims over and over -- 0.36 s of a 7 s load,
all of it thrown away by the de-duplication. Skipping a root its ancestor's walk reached
must find exactly what walking every root did, an `over` on the way included.
"""

from __future__ import annotations

from pxr import Sdf, Usd

from usd_chunks import chunk_stage


def _stage(text):
    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.ImportFromString(text)
    return Usd.Stage.Open(layer)


def _paths(stage, resynced, info=()):
    return [str(p.GetPath()) for p in chunk_stage._iter_scoped_prims(
        stage, {Sdf.Path(p) for p in resynced}, {Sdf.Path(p) for p in info})]


NESTED = """#usda 1.0
def Xform "lib"
{
    def Xform "a"
    {
        def Xform "b"
        {
            def Mesh "m" {}
        }
    }
}
"""


def test_nested_roots_are_walked_once(monkeypatch) -> None:
    stage = _stage(NESTED)
    walks = []
    real = Usd.PrimRange
    monkeypatch.setattr(Usd, "PrimRange", lambda prim, *a: walks.append(prim.GetPath()) or real(prim, *a))
    found = _paths(stage, ["/lib/a/b/m", "/lib/a/b", "/lib", "/lib/a", "/lib/a.xformOpOrder"])
    assert sorted(found) == ["/lib", "/lib/a", "/lib/a/b", "/lib/a/b/m"]
    assert walks == [Sdf.Path("/lib")]


def test_a_root_under_an_over_finds_what_walking_it_alone_finds() -> None:
    stage = _stage("""#usda 1.0
over "a"
{
    def Xform "b"
    {
        def Xform "c" {}
    }
}
def Xform "d"
{
    over "e"
    {
        def Xform "f" {}
    }
}
""")
    alone = sorted({str(p.GetPath()) for root in ("/a", "/a/b", "/d", "/d/e", "/d/e/f")
                    for p in Usd.PrimRange(stage.GetPrimAtPath(root))})
    assert sorted(_paths(stage, ["/a", "/a/b", "/d", "/d/e", "/d/e/f"])) == alone


def test_an_info_change_inside_a_walked_root_is_not_repeated() -> None:
    stage = _stage(NESTED)
    found = _paths(stage, ["/lib/a"], info=["/lib/a/b", "/lib"])
    assert sorted(found) == ["/lib", "/lib/a", "/lib/a/b", "/lib/a/b/m"]
    assert len(found) == len(set(found))
