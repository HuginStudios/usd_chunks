"""A chunk's authored prim path is not renamed when a saved scene is reopened.

Reported for Duplicate Prim (#171) and fixed there by removing a collision check. The
same shape reaches a whole family of chunks through chunk_utils.get_unique_path, which
several call on every cook:

    open a scene with a Prim chunk at /prim  ->  the chunk comes back at /prim1

Saving bakes a chunk's authored prims into the file. On reopen the chunk finds its own
past output sitting at the path and cannot tell it from a stranger's prim, so it bumps --
and the path the scene was saved with is gone. Anything aimed at the old one (a Transform,
a lofted parameter, a gizmo anchor) is left pointing at a prim that no longer exists, and
every reopen shifts it again.

A path arriving from a file is a decision already made. Uniqueness is for choosing a free
name when a chunk is first created -- what each chunk's main() does -- and there is
nothing to choose when restoring one.

camera.py already guarded its own call for exactly this reason; the rule now lives in
get_unique_path so every caller gets it.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_usd import save_scene


class _Scene(SceneSender):
    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _scene_for(stage):
    s = _Scene()
    s._stage = stage
    chunk_registry.ChunkRegistry().initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.initialize(stage, s)
    return s


def _mk(scene, key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


# Every chunk that uniques its path inside execute(), i.e. on every cook. The calls in
# each chunk's main() are creation-time and are deliberately left alone.
COOK_TIME_UNIQUERS = [
    "prim", "light", "camera", "render_pass", "render_product", "render_var",
    "render_settings",
]


@pytest.mark.parametrize("chunk_key", COOK_TIME_UNIQUERS)
def test_the_path_comes_back_unchanged(chunk_key, qapp, tmp_path):
    stage = Usd.Stage.CreateInMemory()
    scene = _scene_for(stage)

    c = _mk(scene, chunk_key)
    qapp.processEvents()
    saved_path = c.get_param("primpath_location").value
    assert saved_path, f"{chunk_key} has no path to test"

    out = tmp_path / f"{chunk_key}.usda"
    save_scene(scene, out)

    stage2 = Usd.Stage.Open(str(out))
    # The scene file carries the geometry the chunk authored, as a real one does. Without
    # it there is nothing for the uniquing to trip over and the bug does not reproduce.
    UsdGeom.Xform.Define(stage2, saved_path)
    scene2 = _scene_for(stage2)
    qapp.processEvents()

    reopened = [
        x for x in scene2.get_chunks() if x.get_param("primpath_location") is not None
    ]
    assert reopened, f"{chunk_key} did not come back at all"
    assert reopened[0].get_param("primpath_location").value == saved_path, (
        f"{chunk_key} was renamed on reopen: "
        f"{reopened[0].get_param('primpath_location').value!r} instead of {saved_path!r}. "
        "Anything aimed at the saved path now points at a prim that does not exist."
    )


def test_a_new_chunk_still_gets_a_free_path(qapp):
    """The guard is about restoring, not about creating: uniquing still works normally."""
    stage = Usd.Stage.CreateInMemory()
    scene = _scene_for(stage)

    first = _mk(scene, "prim")
    qapp.processEvents()
    second = _mk(scene, "prim")
    qapp.processEvents()

    a = first.get_param("primpath_location").value
    b = second.get_param("primpath_location").value
    assert a != b, f"two Prim chunks should not both claim {a!r}"


# ---------------------------------------------------------------------------
# A path you set is honoured, including when it points at an occupied prim.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chunk_key", COOK_TIME_UNIQUERS)
def test_a_path_you_set_is_the_path_that_is_used(chunk_key, qapp):
    """Uniquing belongs to creation, not to every cook.

    main() picks a free default so a new chunk does not land on an existing prim. Once a
    path has been set it is an instruction: re-uniquing it meant a path you typed could be
    silently replaced by one you did not.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _scene_for(stage)

    c = _mk(scene, chunk_key)
    qapp.processEvents()

    chosen = "/deliberately/chosen/path"
    c.get_param("primpath_location").value = chosen
    qapp.processEvents()

    assert c.get_param("primpath_location").value == chosen, (
        f"{chunk_key} rewrote the path that was set: "
        f"{c.get_param('primpath_location').value!r} instead of {chosen!r}"
    )


@pytest.mark.parametrize("chunk_key", COOK_TIME_UNIQUERS)
def test_two_chunks_may_target_the_same_prim(chunk_key, qapp):
    """Overriding another chunk's prim is ordinary USD, and has to be expressible.

    Both chunks author on their own layer, so this composes rather than conflicts -- which
    of the two wins is layer strength's business, not something to prevent by renaming one
    of them behind the user's back.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _scene_for(stage)

    first = _mk(scene, chunk_key)
    second = _mk(scene, chunk_key)
    qapp.processEvents()

    shared = first.get_param("primpath_location").value
    second.get_param("primpath_location").value = shared
    qapp.processEvents()

    assert second.get_param("primpath_location").value == shared, (
        f"the second {chunk_key} was moved off {shared!r} to "
        f"{second.get_param('primpath_location').value!r}; an override cannot be expressed"
    )
