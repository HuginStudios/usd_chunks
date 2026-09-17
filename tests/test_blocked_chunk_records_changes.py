"""A blocked chunk still records what is done to it; it only stops reacting.

View-driven execution blocks every chunk outside the active view's tree, and a scene with
no view blocks them all -- which is where a chunk built in the interpreter lands:

    c = chunk.get_scene().add_chunk(chunk.Chunk("myChunk"))
    p = c.add_param(chunk.Slider("Slider"))
    p.name = "NewName"

Blocking used to drop the change itself, not just the reaction to it. The UI kept showing
"Slider", the prim kept inputs:Slider, and a value typed in meanwhile never reached the
prim -- so the next save wrote back the old name and the old value. Unblocking did not
catch either up. Now the change is recorded (widget, attribute name, attribute value) and
only the reaction is withheld: the param's callbacks, and the value's push onto the real
prims the param drives.

Also here: a rename at any depth renames the attribute and moves the param onto it, so
later edits land on the renamed attribute rather than nowhere.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd
from PySide6 import QtWidgets

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


@pytest.fixture
def env(qapp):
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    def settle():
        for _ in range(10):
            qapp.processEvents()

    c = chunk.Chunk("myChunk")
    scene.add_chunk(c)
    slider = c.add_param(chunk.Slider("Slider"))
    group = c.add_param(chunk.Group("myGroup"))
    inner = group.add_param(chunk.Slider("inner"))
    settle()
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
    return dict(stage=stage, scene=scene, chunk=c, slider=slider, group=group, inner=inner,
                prim=prim, settle=settle)


def _inputs(prim):
    return {a.GetName(): a.Get() for a in prim.GetAttributes() if a.GetName().startswith("inputs:")}


def _block_everything(env):
    """What a context with no active view does: block and mute every chunk in it."""
    chunk_stage.recook_for_view(env["stage"], env["scene"], None, None)
    assert env["chunk"].is_blocked(), "precondition: the chunk is blocked"


def test_a_rename_while_blocked_renames_the_attribute(env):
    _block_everything(env)
    env["slider"].name = "NewName"
    env["settle"]()
    names = set(_inputs(env["prim"]))
    assert "inputs:NewName" in names and "inputs:Slider" not in names


def test_a_rename_while_blocked_reaches_the_widget(env, qapp):
    widget = ChunkWidget(env["chunk"])
    _block_everything(env)
    env["slider"].name = "NewName"
    env["settle"]()
    labels = {label.text() for label in widget.findChildren(QtWidgets.QLabel)}
    assert "NewName" in labels and "Slider" not in labels
    widget.deleteLater()


def test_a_value_set_while_blocked_is_recorded(env):
    _block_everything(env)
    env["slider"].value = 0.9
    env["settle"]()
    assert _inputs(env["prim"])["inputs:Slider"] == pytest.approx(0.9)


def test_a_blocked_chunk_still_does_not_react(env):
    """Blocking still means what it meant: nothing runs."""
    calls = []
    env["slider"].add_callback(lambda owner, notice, sender: calls.append(owner.value))
    _block_everything(env)
    env["slider"].value = 0.9
    env["settle"]()
    assert calls == []


def test_an_unblocked_chunk_still_reacts(env):
    calls = []
    env["slider"].add_callback(lambda owner, notice, sender: calls.append(owner.value))
    env["slider"].value = 0.4
    env["settle"]()
    assert calls == [pytest.approx(0.4)]


def test_edits_after_a_rename_land_on_the_renamed_attribute(env):
    env["slider"].value = 0.3
    env["settle"]()
    env["slider"].name = "NewName"
    env["settle"]()
    env["slider"].value = 0.6
    env["settle"]()
    values = _inputs(env["prim"])
    assert "inputs:Slider" not in values
    assert values["inputs:NewName"] == pytest.approx(0.6)


def test_a_param_inside_a_group_is_renamed(env):
    env["inner"].value = 0.2
    env["settle"]()
    env["inner"].name = "renamed"
    env["settle"]()
    env["inner"].value = 0.7
    env["settle"]()
    values = _inputs(env["prim"])
    assert "inputs:myGroup:inner" not in values
    assert values["inputs:myGroup:renamed"] == pytest.approx(0.7)


def test_renaming_a_group_renames_everything_under_it(env):
    env["inner"].value = 0.2
    env["settle"]()
    env["group"].name = "otherGroup"
    env["settle"]()
    env["inner"].value = 0.8
    env["settle"]()
    values = _inputs(env["prim"])
    assert not [n for n in values if n.startswith("inputs:myGroup:")]
    assert values["inputs:otherGroup:inner"] == pytest.approx(0.8)


def test_a_connection_to_a_renamed_attribute_follows_it(env):
    """A namespace edit leaves connections to the old name dangling."""
    env["slider"].value = 0.3
    env["settle"]()
    stage, prim = env["stage"], env["prim"]
    other = stage.DefinePrim("/listener")
    attr = other.CreateAttribute("reads", Sdf.ValueTypeNames.Float)
    attr.SetConnections([prim.GetPath().AppendProperty("inputs:Slider")])
    other.SetTypeName("Chunk")  # only chunk prims are patched

    env["slider"].name = "NewName"
    env["settle"]()

    assert attr.GetConnections() == [prim.GetPath().AppendProperty("inputs:NewName")]
