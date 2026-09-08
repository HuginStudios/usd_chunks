"""The chunk prim schema: where persisted chunk state lives, and which layer it lands in.

These build bare chunks on an in-memory stage — no Qt, no widgets. The schema is pure USD
bookkeeping, so if a test here needs a shown widget the seam is in the wrong place.
"""

from __future__ import annotations

from pxr import Sdf, Usd

from usd_chunks import chunk as chunk_mod
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks import chunk_schema
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _make_scene(stage: Usd.Stage) -> SceneSender:
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)
    return scene


def _chunk(scene, name: str, module: str = "prim") -> chunk_mod.Chunk:
    c = chunk_mod.Chunk(name)
    c._config = ChunkConfig(module=module)
    scene.add_chunk(c)
    return c


# ---------------------------------------------------------------------------
# Defaults — absent always means the pre-feature behaviour
# ---------------------------------------------------------------------------

def test_absent_attribute_reads_back_as_its_declared_default() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    assert chunk_schema.read(ctx, chunk_schema.ATTR_PIN_FILTER) is True
    assert chunk_schema.read(ctx, chunk_schema.ATTR_SEARCH_QUERY) == ""
    assert chunk_schema.read(ctx, chunk_schema.ATTR_SEARCH_SCOPE) == "both"


def test_a_stored_value_outside_the_accepted_set_falls_back_to_the_default() -> None:
    """A scene saved by a future build, or hand-edited, can hold a scope this build has
    never heard of. It must not reach the caller — the filter bar would have no widget
    state to show for it."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    chunk_schema.write(ctx, chunk_schema.ATTR_SEARCH_SCOPE, "category")

    assert chunk_schema.read(ctx, chunk_schema.ATTR_SEARCH_SCOPE) == "both"


def test_reading_a_context_with_no_prim_yet_does_not_raise() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    assert not stage.GetPrimAtPath("/Tab1").IsValid()

    assert chunk_schema.read(ctx, chunk_schema.ATTR_PIN_FILTER) is True


# ---------------------------------------------------------------------------
# Where the state lives
# ---------------------------------------------------------------------------

def test_context_state_lands_on_the_contexts_own_prim_not_a_child_of_it() -> None:
    """Regression: resolving a context's path the way a *child* resolves its own doubles
    the context's name (/Tab1/Tab1), because get_context() returns self for a chunk that
    already carries the CONTEXT flag. Declaring Where.CONTEXT on the attribute is what
    stops a caller making that choice by accident."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    chunk_schema.write(ctx, chunk_schema.ATTR_PIN_FILTER, False)

    assert stage.GetPrimAtPath("/Tab1").GetAttribute("filter:pinEnabled").Get() is False
    assert not stage.GetPrimAtPath("/Tab1/Tab1").IsValid()


def test_chunk_state_lands_on_the_chunks_own_prim_under_its_context() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    c = _chunk(scene, "Foo")

    chunk_schema.write(c, chunk_schema.ATTR_PINNED, False)

    path = chunk_usd.get_chunk_prim_path(c)
    assert stage.GetPrimAtPath(Sdf.Path(path)).GetAttribute("chunk:pinned").Get() is False


def test_writing_defines_a_context_prim_that_does_not_exist_yet() -> None:
    """A plain tab context's prim is never authored by _define_prim (add_chunk passes
    send_added=False for a CONTEXT chunk with no module), so an empty just-created tab has
    no prim at all. Silently doing nothing would drop the setting."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    assert not stage.GetPrimAtPath("/Tab1").IsValid()

    assert chunk_schema.write(ctx, chunk_schema.ATTR_SEARCH_QUERY, "apple") is True
    assert stage.GetPrimAtPath("/Tab1").IsValid()
    assert chunk_schema.read(ctx, chunk_schema.ATTR_SEARCH_QUERY) == "apple"


# ---------------------------------------------------------------------------
# The write policy — one layer per attribute, for the life of that attribute
# ---------------------------------------------------------------------------

def _layers_holding(stage: Usd.Stage, attr_name: str) -> list:
    return [
        layer.identifier
        for layer in (stage.GetSessionLayer(), stage.GetRootLayer())
        if attr_name in layer.ExportToString()
    ]


def test_a_second_write_under_a_moved_edit_target_still_takes_effect() -> None:
    """The bug this module exists to kill.

    An Edit Target chunk moves the stage's ambient edit target while the graph is live. A
    setter that writes through that target puts its first opinion in one layer and its
    second in another; the stronger layer keeps winning and the toggle silently stops
    responding. Writing to the attribute's existing home instead keeps one opinion in one
    place for the life of the attribute.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    chunk_schema.write(ctx, chunk_schema.ATTR_PIN_FILTER, False)
    assert chunk_schema.read(ctx, chunk_schema.ATTR_PIN_FILTER) is False

    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    chunk_schema.write(ctx, chunk_schema.ATTR_PIN_FILTER, True)

    assert chunk_schema.read(ctx, chunk_schema.ATTR_PIN_FILTER) is True
    assert len(_layers_holding(stage, "filter:pinEnabled")) == 1


def test_repeated_writes_never_split_one_attribute_across_layers() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    for i, target in enumerate((stage.GetSessionLayer(), stage.GetRootLayer(), stage.GetSessionLayer())):
        stage.SetEditTarget(Usd.EditTarget(target))
        chunk_schema.write(ctx, chunk_schema.ATTR_SEARCH_QUERY, f"q{i}")

    assert chunk_schema.read(ctx, chunk_schema.ATTR_SEARCH_QUERY) == "q2"
    assert len(_layers_holding(stage, "filter:searchQuery")) == 1


# ---------------------------------------------------------------------------
# chunk_type
# ---------------------------------------------------------------------------

def test_chunk_type_is_safe_on_an_invalid_or_bare_prim() -> None:
    stage = Usd.Stage.CreateInMemory()

    assert chunk_schema.chunk_type(stage.GetPrimAtPath("/nope")) is None
    assert chunk_schema.chunk_type(stage.DefinePrim("/bare", "Scope")) is None


def test_chunk_type_reads_the_authored_module_name() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    c = _chunk(scene, "Foo", module="transform")

    prim = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(c)))
    assert chunk_schema.chunk_type(prim) == "transform"
