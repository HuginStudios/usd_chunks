"""The escape hatch for handles that are not 1:1 with any param.

Declarative drivers cover the cases where a slot maps onto params directly. Some handles do
not: a lookat point's world position is `camPos + aimDir * focusDistance`, so dragging it has
to decompose back into *rotate* params, and there is no binding to declare.

The property that makes this acceptable at all is that the DCC contract does not change --
the host emits slot values and never learns what a chunk means by them. Anything that made
hosts aware of per-chunk solving would be a design failure, so that is asserted here too.
"""
from __future__ import annotations

import logging
import math
import types

import pytest
from pxr import Gf

from usd_chunks import chunk, chunk_gizmo, chunk_notice, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_notice import ChunkNoticeAlias


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


def _camera_chunk(hook=None, gizmos=None):
    c = chunk.Chunk("Camera")
    c.add_param(chunk.LineEdit("primpath_location", default_value="/World/Cam"))
    c.add_param(chunk.Slider("focusDistance", min_value=0.0, max_value=1000.0, default_value=0.0))
    for axis in ("rx", "ry", "rz"):
        c.add_param(chunk.Slider(axis, min_value=-180.0, max_value=180.0, default_value=0.0))
    c._config = ChunkConfig(module="camera", gizmos=gizmos or [
        chunk.Gizmo("aim", type="cube", anchor="primpath_location", driver="focusDistance"),
    ])
    if hook is not None:
        module = types.ModuleType("scripts.camera_stub")
        module.gizmo_changed = hook
        c._module = module
    return c


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def test_the_hook_runs_instead_of_the_declarative_drivers() -> None:
    calls = []

    def gizmo_changed(node, gizmo, value):
        calls.append((gizmo, value))
        return True

    c = _camera_chunk(gizmo_changed)
    chunk_gizmo.apply_bindings(c, "aim", 12.5)

    assert calls == [("aim", 12.5)]
    assert c.get_param("focusDistance").value == pytest.approx(0.0), "declarative path skipped"


def test_no_hook_means_the_declarative_path() -> None:
    c = _camera_chunk()
    chunk_gizmo.apply_bindings(c, "aim", 12.5)

    assert c.get_param("focusDistance").value == pytest.approx(12.5)


def test_a_falsy_return_falls_through_to_the_declarative_path() -> None:
    """The common shape: a hook that owns one gizmo and declines the rest."""
    def gizmo_changed(node, gizmo, value):
        return gizmo == "somethingelse"

    c = _camera_chunk(gizmo_changed)
    chunk_gizmo.apply_bindings(c, "aim", 12.5)

    assert c.get_param("focusDistance").value == pytest.approx(12.5)


def test_handled_is_signalled_not_inferred() -> None:
    """A hook that writes params but returns nothing has not claimed the gizmo, so the
    declarative path still runs. Inferring 'handled' from side effects would mean guessing
    whether a hook that wrote nothing declined or simply had nothing to do this tick."""
    def gizmo_changed(node, gizmo, value):
        node.get_param("rx").value = 45.0
        # no return

    c = _camera_chunk(gizmo_changed)
    chunk_gizmo.apply_bindings(c, "aim", 12.5)

    assert c.get_param("rx").value == pytest.approx(45.0)
    assert c.get_param("focusDistance").value == pytest.approx(12.5)


def test_a_non_callable_attribute_is_ignored() -> None:
    c = _camera_chunk()
    module = types.ModuleType("scripts.camera_stub")
    module.gizmo_changed = "not a function"
    c._module = module

    chunk_gizmo.apply_bindings(c, "aim", 3.0)

    assert c.get_param("focusDistance").value == pytest.approx(3.0)


def test_the_hook_is_taken_from_the_chunks_own_module_object() -> None:
    """Chunk modules exist twice in sys.modules under different keys, and the other copy is
    not the one the running chunk was built from. Looking the hook up by import would find a
    function belonging to a different object graph."""
    seen = []

    def gizmo_changed(node, gizmo, value):
        seen.append(node)
        return True

    c = _camera_chunk(gizmo_changed)
    chunk_gizmo.apply_bindings(c, "aim", 1.0)

    assert seen == [c], "the hook receives the live chunk, not a re-imported twin"


# ---------------------------------------------------------------------------
# the inverse solve it exists for
# ---------------------------------------------------------------------------

def test_a_lookat_drag_decomposes_into_rotate_params() -> None:
    """The motivating case. The handle's world position carries no rotation, but the chunk
    knows the aim point implies one -- so it solves rather than binds."""
    def gizmo_changed(node, gizmo, value):
        if gizmo != "aim":
            return False
        # The handle gives a point. The chunk turns it into "how far" and "which way",
        # because only the chunk knows the camera looks down -Z.
        x, y, z = value
        distance = math.sqrt(x * x + y * y + z * z)
        node.get_param("focusDistance").value = distance
        node.get_param("ry").value = math.degrees(math.atan2(x, -z))
        node.get_param("rx").value = math.degrees(math.asin(y / distance)) if distance else 0.0
        return True

    c = _camera_chunk(gizmo_changed, gizmos=[
        chunk.Gizmo("aim", type="cube", anchor="primpath_location",
                    driver=("rx", "ry", "rz")),
    ])

    # Straight ahead: ten units down -Z, no rotation needed.
    chunk_gizmo.apply_bindings(c, "aim", Gf.Vec3d(0.0, 0.0, -10.0))
    assert c.get_param("focusDistance").value == pytest.approx(10.0)
    assert c.get_param("rx").value == pytest.approx(0.0)
    assert c.get_param("ry").value == pytest.approx(0.0)

    # Directly to the right: a quarter turn, same distance -- and note the drag carried no
    # rotation at all, which is exactly why this cannot be a declarative binding.
    chunk_gizmo.apply_bindings(c, "aim", Gf.Vec3d(10.0, 0.0, 0.0))
    assert c.get_param("focusDistance").value == pytest.approx(10.0)
    assert c.get_param("ry").value == pytest.approx(90.0)


def test_the_host_contract_is_unchanged_by_the_hook() -> None:
    """The point: apply_bindings takes the same slot dict either way, and nothing about the
    call tells a host whether a chunk solves or binds."""
    handled = _camera_chunk(lambda node, gizmo, value: True)
    plain = _camera_chunk()
    payload = 4.0

    chunk_gizmo.apply_bindings(handled, "aim", payload)
    chunk_gizmo.apply_bindings(plain, "aim", payload)  # identical call shape


# ---------------------------------------------------------------------------
# failure
# ---------------------------------------------------------------------------

def test_a_raising_hook_does_not_break_the_drag(caplog) -> None:
    def gizmo_changed(node, gizmo, value):
        raise RuntimeError("solver blew up")

    c = _camera_chunk(gizmo_changed)

    with caplog.at_level(logging.ERROR):
        chunk_gizmo.apply_bindings(c, "aim", 12.5)  # must not raise

    assert "gizmo_changed hook failed" in caplog.text


def test_a_raising_hook_does_not_then_also_apply_the_declarative_path() -> None:
    """Falling through after a half-finished custom solve would combine two different intents
    into one frame, which is worse than dropping a tick -- the next tick retries anyway."""
    def gizmo_changed(node, gizmo, value):
        node.get_param("rx").value = 10.0
        raise RuntimeError("halfway through")

    c = _camera_chunk(gizmo_changed)
    chunk_gizmo.apply_bindings(c, "aim", 12.5)

    assert c.get_param("focusDistance").value == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# notices
# ---------------------------------------------------------------------------

def test_a_handled_drag_still_announces_itself() -> None:
    """A listener watching the drag must not be able to tell solved from bound."""
    seen = []

    def callback(notice, sender):
        seen.append(notice)

    connection = chunk_notice.NoticeConnection(callback, ChunkNoticeAlias.GIZMO_CHANGED, globally=True)
    c = _camera_chunk(lambda node, gizmo, value: True)

    chunk_gizmo.apply_bindings(c, "aim", 1.0)

    assert len(seen) == 1
    del connection


def test_an_unknown_gizmo_name_is_still_loud_with_a_hook_present() -> None:
    """The hook is an escape hatch for solving, not for inventing handles the chunk never
    declared -- a host asking for one is a contract mismatch either way."""
    c = _camera_chunk(lambda node, gizmo, value: True)

    with pytest.raises(ValueError, match="declares no gizmo"):
        chunk_gizmo.apply_bindings(c, "nope", 1.0)
