"""Undo has to move the sliders too, not just the prim.

Reported live in usdview: Ctrl+Z moves the object back, but the chunk's translate values
stay where they were -- and only catch up once you touch the gizmo again.

ChunkStageSync mirrors USD back into params in _on_objects_changed, but used to read only
GetChangedInfoOnlyPaths(). A real Ctrl+Z in usdview was instrumented and reports the param
as a **resync** instead:

    resynced   ['/Tab1/Transform.inputs:translate_grp:tz', ...]
    info-only  ['/sopcreate1', '/sopcreate1/mesh_0']

so the mirror never saw it. Precisely the edits a user cannot make by hand -- undo, redo, a
layer reload -- were the ones ignored.

Note the layer-content-restore tests below pass either way: re-importing a layer's text is
reported as info-only, so it does NOT model a real undo. They are kept as a regression
guard, and as a warning to anyone who reaches for that as a stand-in for undo again.

The real engine cannot be driven from here -- hugin is a binary built for Python 3.11
against the bundle's USD and does not import under this interpreter -- so the resync case
is provoked directly through Sdf instead.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import _values_agree, initialize as _chunk_usd_init
import usd_chunks.chunk_notice as cn


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _session(stage):
    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return reg, scene


def _snapshot(stage):
    return {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetLayerStack(includeSessionLayers=True)
    }


def _restore(stage, snap):
    """Undo, as usd-undo does it: put layer contents back wholesale.

    This is the crux -- a wholesale content restore is reported by USD as a resync of the
    affected paths, not as an info-only change, which is why the param mirror missed it.
    """
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        text = snap.get(layer.identifier)
        if text is not None and layer.ExportToString() != text:
            layer.ImportFromString(text)


def _transform_chunk(reg, scene):
    mod = reg.get_chunk_module("transform")
    chunk = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(chunk)
    return chunk


def test_undo_restores_the_param_not_just_the_prim() -> None:
    stage = Usd.Stage.CreateInMemory()
    reg, scene = _session(stage)
    chunk = _transform_chunk(reg, scene)
    chunk.get_param("primpath_location").value = "/World/Cube"

    chunk.get_param("tx").value = 5.0
    before = _snapshot(stage)

    chunk.get_param("tx").value = 11.0
    assert chunk.get_param("tx").value == 11.0

    _restore(stage, before)

    assert chunk.get_param("tx").value == 5.0, (
        "the slider kept the undone value -- the object moves back but the chunk disagrees"
    )


def test_redo_moves_the_param_forward_again() -> None:
    stage = Usd.Stage.CreateInMemory()
    reg, scene = _session(stage)
    chunk = _transform_chunk(reg, scene)
    chunk.get_param("primpath_location").value = "/World/Cube"

    chunk.get_param("tx").value = 5.0
    before = _snapshot(stage)
    chunk.get_param("tx").value = 11.0
    after = _snapshot(stage)

    _restore(stage, before)
    _restore(stage, after)

    assert chunk.get_param("tx").value == 11.0


def _resync_param(stage, attr_path, value):
    """Change a param attribute in a way USD reports as a RESYNC, not an info-only change.

    Removing the property spec and re-adding it is a structural edit, which is the notice
    shape a real undo produces. Setting the attribute normally would be info-only and would
    exercise the path that already worked.
    """
    path = Sdf.Path(attr_path)
    # Where the chunk's prim was authored: the stage's edit target.
    layer = stage.GetEditTarget().GetLayer()
    with Usd.EditContext(stage, layer):
        prim_spec = layer.GetPrimAtPath(path.GetPrimPath())
        prop = prim_spec.properties.get(path.name) if prim_spec else None
        assert prop is not None, f"no spec at {attr_path} to resync"
        type_name = prop.typeName
        # Inside a change block, so the whole edit lands as ONE resync. Without it the
        # trailing `.default = value` is its own info-only notice, which the mirror already
        # handled -- the test would then pass with or without the fix, proving nothing.
        with Sdf.ChangeBlock():
            del prim_spec.properties[path.name]
            Sdf.AttributeSpec(prim_spec, path.name, type_name).default = value


def test_a_resynced_param_is_mirrored_back() -> None:
    """The reported bug. A real Ctrl+Z reports the param as resynced, and the mirror used to
    read only the info-only set -- so the prim moved back and the slider did not."""
    stage = Usd.Stage.CreateInMemory()
    reg, scene = _session(stage)
    chunk = _transform_chunk(reg, scene)

    chunk.get_param("tx").value = 5.0
    attr_path = getattr(chunk.get_param("tx"), "_param_attr_path", None)
    assert attr_path, "param never got an attribute to mirror"

    _resync_param(stage, attr_path, 2.0)

    assert chunk.get_param("tx").value == 2.0, (
        "the slider kept the undone value -- the prim moves back but the chunk disagrees"
    )


def test_a_change_arriving_mid_write_is_still_dropped() -> None:
    """Documents a known gap rather than a fix, so the next attempt does not repeat mine.

    _on_objects_changed bails while _usd_write_active is set, and the paths it saw are
    discarded. A change landing mid-write is therefore lost -- a candidate for the reported
    "undo moved the prim every step but the sliders followed only about half the time".

    Keeping those paths and replaying them on the next notice does NOT work. A suppressed
    notice means we were mid-write, and a multi-component write leaves USD transiently
    holding partial values: the replay pushed a stale (1, 0, 0) over a param that already
    correctly held (1, 2, 3), breaking test_vector_math_ops_expression. Any real fix has to
    drain at a point where USD is known consistent -- as the write block closes -- not on
    the next unrelated notice.
    """
    stage = Usd.Stage.CreateInMemory()
    reg, scene = _session(stage)
    chunk = _transform_chunk(reg, scene)

    chunk.get_param("tx").value = 5.0
    chunk.get_param("ty").value = 4.0
    attr_path = chunk.get_param("tx")._param_attr_path

    previous = cn._usd_write_active
    cn._usd_write_active = True
    try:
        _resync_param(stage, attr_path, 2.0)
    finally:
        cn._usd_write_active = previous

    # An unrelated notice does not catch it up. Asserted as-is so that a future fix has to
    # come here and change it deliberately.
    _resync_param(stage, chunk.get_param("ty")._param_attr_path, 1.0)
    assert chunk.get_param("tx").value == 5.0


def test_float32_rounding_is_not_treated_as_a_change() -> None:
    """A param can hold a 64-bit float while USD stores 32, so the two differ in the 8th
    significant digit. These are the exact values a live session reported:

        Transform.tx   param=-3.3492076146840537   USD=-3.349207639694214

    Compared exactly they are permanently unequal -- so now that resyncs reach the mirror,
    every notice mentioning the attribute would rewrite the param, dirtying the chunk and
    cooking it. Harmless once; resyncs are frequent enough that it is not once.

    Tested at the comparison rather than end-to-end because assigning through the param
    setter rounds it too, which makes the drifted state impossible to reach that way.
    """
    assert _values_agree(-3.349207639694214, -3.3492076146840537)
    assert _values_agree(-7.938231468200684, -7.938231637851604)
    assert _values_agree(-2.4296252727508545, -2.4296252931861098)

    # A real edit still reads as one. An undo moves things by far more than this.
    assert not _values_agree(-3.349, -3.35)
    assert not _values_agree(0.0, 1e-3)


def test_agreement_stays_exact_where_it_has_to() -> None:
    """The tolerance is for float32 round-tripping only -- it must not blur anything else."""
    assert not _values_agree(True, False)
    assert not _values_agree("/World/Cube", "/World/Sphere")
    assert _values_agree((1.0, 2.0), (1.0, 2.0))
    assert not _values_agree((1.0, 2.0), (1.0, 9.0))
    # Different lengths are not "equal as far as they go".
    assert not _values_agree((1.0, 2.0), (1.0, 2.0, 3.0))


def test_equal_length_strings_do_not_recurse() -> None:
    """A str is iterable and yields 1-character strs, which are iterable in turn -- so
    treating strings as sequences recurses until the stack dies, taking down whatever is
    holding the notice.

    Its own length is what hides it: unequal lengths raise out of zip(strict=True) and land
    on plain equality, so "/World/Cube" vs "/World/Sphere" passes happily while any two prim
    paths of the SAME length blow up. Which is how it reached a full test run before being
    caught, in three unrelated loft tests.
    """
    assert not _values_agree("/World/Cube", "/World/Cyl0")
    assert _values_agree("/World/Cube", "/World/Cube")
    assert not _values_agree("abc", "abd")


def test_an_ordinary_edit_still_syncs() -> None:
    """The info-only path is the common one, and the control for the two above."""
    stage = Usd.Stage.CreateInMemory()
    reg, scene = _session(stage)
    chunk = _transform_chunk(reg, scene)

    chunk.get_param("tx").value = 3.0
    attr_path = getattr(chunk.get_param("tx"), "_param_attr_path", None)
    assert attr_path, "param never got an attribute to mirror"

    # Into the SESSION layer. Chunk inputs are authored there, and it outranks the root
    # layer -- writing through the stage's ambient edit target instead leaves the resolved
    # value untouched, so the mirror correctly does nothing and the test fails blaming it.
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.GetAttributeAtPath(Sdf.Path(attr_path)).Set(7.5)

    assert chunk.get_param("tx").value == 7.5


def test_a_vector_expression_survives_a_resync() -> None:
    """The scalar branch of the mirror has always skipped expression-driven params; the
    vector branch did not. Latent while the mirror only ran for info-only paths, and a live
    hazard now that resyncs reach it: writing the resolved value over the expression swaps a
    live link for a frozen number, and the vector silently stops tracking its source.

    Found while chasing the undo bugs, not reported -- so it gets its own test rather than
    riding along untested in the fix that exposed it.
    """
    stage = Usd.Stage.CreateInMemory()
    reg, scene = _session(stage)
    mod = reg.get_chunk_module("vector_math_ops")
    source = (mod.create() if mod.loaded else mod.load())[0]
    source.name = "VecSource"
    scene.add_chunk(source)
    target = (mod.create() if mod.loaded else mod.load())[0]
    target.name = "VecTarget"
    scene.add_chunk(target)

    source.get_param("a").value = (1.0, 2.0, 3.0)
    expression = f"@{source.name}.a@"
    target.get_param("a").value = expression
    assert target.get_param("a").value == pytest.approx((1.0, 2.0, 3.0)), "expression never resolved"

    _resync_param(stage, target.get_param("a")._param_attr_path, (9.0, 9.0, 9.0))

    assert target.get_param("a").raw_value == expression, "the expression was frozen to a value"
