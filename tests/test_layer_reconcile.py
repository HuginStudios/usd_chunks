"""layer_reconcile: dst ends up holding what src does, and only what differs is changed."""

from __future__ import annotations

from pxr import Sdf, Tf, Usd

from usd_chunks import layer_reconcile, skip_cook

BASE = """#usda 1.0
(
    defaultPrim = "World"
)

def Xform "World" (
    kind = "assembly"
)
{
    def Xform "A" (
        prepend references = @x.usda@</B>
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        double3 xformOp:translate = (1, 2, 3)
        double3 xformOp:translate.timeSamples = {
            1: (0, 0, 0),
            2: (1, 1, 1),
        }
        uniform token[] xformOpOrder = ["xformOp:translate"]
        rel material:binding = </M>
        float c.connect = </World/A.b>

        def Mesh "C"
        {
            int[] faceVertexCounts = [3]
        }

        def Mesh "D"
        {
        }
    }

    def "V" (
        variants = {
            string look = "red"
        }
        prepend variantSets = "look"
    )
    {
        variantSet "look" = {
            "blue" {
                def "Leaf"
                {
                    color3f c = (0, 0, 1)
                }
            }
            "red" {
                def "Leaf"
                {
                    color3f c = (1, 0, 0)
                }
            }
        }
    }

    def "Last"
    {
    }
}
"""


def _layer(text: str) -> Sdf.Layer:
    layer = Sdf.Layer.CreateAnonymous("reconcile_test")
    layer.ImportFromString(text)
    return layer


class _Changes:
    """The resynced and info-changed paths a stage over the layer hears."""

    def __init__(self, layer: Sdf.Layer):
        self.stage = Usd.Stage.Open(layer)
        self.resynced: list[Sdf.Path] = []
        self.changed: list[Sdf.Path] = []
        self._key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, self._heard, self.stage)

    def _heard(self, notice, _sender) -> None:
        self.resynced += notice.GetResyncedPaths()
        self.changed += notice.GetChangedInfoOnlyPaths()


def _check(old: str, new: str) -> _Changes:
    dst, src = _layer(old), _layer(new)
    before = skip_cook.snapshot(dst)
    changes = _Changes(dst)
    said = layer_reconcile.reconcile(src, dst)
    assert dst.ExportToString() == src.ExportToString()
    # What it says it changed covers what comparing the layers finds: each path at or
    # under one it names, which is how a chunk below reads a hand-down (skip_cook._touches).
    found = skip_cook.changed_paths(before, dst)
    assert all(any(path.HasPrefix(named) for named in said) for path in found), (found, said)
    # More is only an extra cook below (children copied again to reorder them); nothing,
    # when nothing changed, is what lets the chunks below skip.
    if old == new:
        assert said == set()
    return changes


def test_the_same_content_changes_nothing():
    changes = _check(BASE, BASE)
    assert changes.resynced == []
    assert changes.changed == []


def test_a_moved_value_is_an_info_only_change():
    changes = _check(BASE, BASE.replace("(1, 2, 3)", "(4, 5, 6)"))
    assert changes.resynced == []
    assert changes.changed == [Sdf.Path("/World/A.xformOp:translate")]


def test_time_samples_targets_and_connections_change_in_place():
    new = (BASE.replace("2: (1, 1, 1)", "3: (2, 2, 2)")
           .replace("</M>", "</N>")
           .replace("</World/A.b>", "</World/A.q>"))
    changes = _check(BASE, new)
    assert all(not path.IsPrimPath() for path in changes.resynced)


def test_a_new_prim_resyncs_only_itself():
    new = BASE.replace('    def "Last"', '    def "Added"\n    {\n    }\n\n    def "Last"')
    changes = _check(BASE, new)
    assert set(changes.resynced) <= {Sdf.Path("/World/Added"), Sdf.Path("/World/Last"), Sdf.Path("/World")}
    assert Sdf.Path("/World/A") not in changes.resynced


def test_a_connection_and_targets_taken_away():
    _check(BASE, BASE.replace(".connect = </World/A.b>", " = 1").replace(" = </M>", ""))


def test_a_removed_prim_and_property():
    new = BASE.replace('        def Mesh "D"\n        {\n        }\n', "").replace(
        '        rel material:binding = </M>\n', "")
    _check(BASE, new)


def test_type_specifier_and_metadata_changes():
    new = (BASE.replace('def Mesh "C"', 'over Scope "C"')
           .replace('kind = "assembly"', 'kind = "group"')
           .replace('"MaterialBindingAPI"', '"MaterialBindingAPI", "CollectionAPI:x"')
           .replace('defaultPrim = "World"', 'defaultPrim = "Other"'))
    _check(BASE, new)


def test_variants_are_reconciled_and_reshaped():
    _check(BASE, BASE.replace("(0, 0, 1)", "(0, 1, 0)").replace('look = "red"', 'look = "blue"'))
    _check(BASE, BASE.replace('            "blue" {\n                def "Leaf"\n                {\n'
                              '                    color3f c = (0, 0, 1)\n                }\n            }\n', ""))


def test_order_and_sublayers():
    swapped = BASE.replace('def Mesh "C"\n        {\n            int[] faceVertexCounts = [3]\n        }',
                           "TMP").replace('def Mesh "D"\n        {\n        }', 'def Mesh "C"\n        {\n'
                                          '            int[] faceVertexCounts = [3]\n        }').replace(
        "TMP", 'def Mesh "D"\n        {\n        }')
    _check(BASE, swapped)
    subbed = BASE.replace('    defaultPrim = "World"\n', '    defaultPrim = "World"\n    subLayers = [\n'
                          '        @a.usda@ (offset = 2),\n        @b.usda@\n    ]\n')
    _check(BASE, subbed)
    _check(subbed, BASE)


def test_from_and_to_empty():
    _check(BASE, "#usda 1.0\n")
    _check("#usda 1.0\n", BASE)
