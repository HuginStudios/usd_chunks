"""Opening a network cooks what it has to, once, and builds only the UI that shows.

Each test pins one of the fixes from timing car_configurator.usda and solids_kitti.usda,
where a file took 6.3 s to settle and diving into a group 12 s:

* A stale group forces its own inside to cook, not the parent chain its cook_list reaches
  back through -- that re-ran every upstream chunk on the first recook after a load.
* A group has no execute(), so a load left it stale even though it had just cooked
  everything in it; the first recook then forced its whole inside again.
* A Label is display text: a module setting its status line during execute() must not
  recook everything downstream.
* A host that recooks for its active view straight after a load (usdview) can leave a
  root no active view reaches uncooked -- the recook would only mute it.
* The USD listeners are global and hear every stage in the process. Opening a .mtlx builds
  its material on a private stage of the MaterialX plugin's, hundreds of change rounds that
  cost more than the file.
* A collapsed group builds its contents on first expand, not with the node.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore
from pxr import Sdf, Usd

from usd_chunks import Scene, chunk, chunk_cook, chunk_core, chunk_notice, chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import connect_exec, get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.chunk_widget_api import ChunkParamWidgetBase
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.widgets import ChunkWidget, ContainerWidget
from usd_chunks.graph import engine as chunk_graph


class _Scene(SceneSender):
    stage = None

    def get_stage(self):
        return self.stage

    def get_selected_prims(self):
        return []


# --- a Label is display text ---------------------------------------------------------------


def test_a_label_change_is_presentation_only():
    label = chunk.Label("status", default_value="")
    assert label.get_attribute_sender("value").is_presentation_change()


def test_other_params_still_dirty_what_reads_them():
    field = chunk.LineEdit("path", "Path")
    assert not field.get_attribute_sender("value").is_presentation_change()


# --- a network with a group fed from upstream ------------------------------------------------


@pytest.fixture
def network(qapp):
    """up -> group[ inner reads the group's input ] -> view, and a stray root beside them."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene()
    scene.stage = stage
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(600, 600)
    docker.show()
    qapp.processEvents()

    def settle():
        for _ in range(30):
            qapp.processEvents()
            QtCore.QThread.msleep(10)
        qapp.processEvents()

    def mk(key, context=None):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        prev = scene._current_context
        scene._current_context = weakref.ref(context) if context is not None else None
        try:
            for c in cs:
                scene.add_chunk(c)
        finally:
            scene._current_context = prev
        return cs[0]

    def prim(path, context=None):
        c = mk("prim", context=context)
        c.get_param("primpath_location").value = path
        return c

    up = prim("/up")
    group = mk("chunkgroup")
    inner = prim("/inner", context=group)
    stray = prim("/stray")
    settle()

    view = next(c for c in scene.get_chunks() if chunk_stage.is_view_chunk(c))
    connect_exec(stage, scene, get_chunk_prim_path(up), get_chunk_prim_path(group))
    # Inside the group, the inner chunk reads what the group brings in -- which is what
    # puts the parent chain on the group's own cook_list.
    connect_exec(stage, scene, get_chunk_prim_path(group), get_chunk_prim_path(inner))
    connect_exec(stage, scene, get_chunk_prim_path(group), get_chunk_prim_path(view))
    settle()
    root = scene.get_root_context()
    chunk_stage.recook_focus(stage, scene, root, view)
    settle()

    yield dict(stage=stage, scene=scene, root=root, view=view, up=up, group=group,
               inner=inner, stray=stray, settle=settle, docker=docker)
    docker.close()
    docker.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.fixture
def cooks(monkeypatch):
    """The chunks that cook, in order -- by object: prim chunks share a name across groups."""
    seen: list = []
    real = chunk_cook.cook

    def counting(self):
        ran = real(self)
        if ran:
            seen.append(self)
        return ran

    monkeypatch.setattr(chunk_cook, "cook", counting)
    return seen


def test_a_stale_group_forces_its_inside_not_its_upstream(network, cooks):
    up, group, inner = network["up"], network["group"], network["inner"]
    assert not chunk_cook.is_cook_stale(up), "precondition: the upstream chunk has cooked"
    chunk_cook.mark_cook_stale(group)

    chunk_stage.recook_focus(network["stage"], network["scene"], network["root"], network["view"])

    assert any(c is inner for c in cooks), "a stale group still forces its own inside"
    assert not any(c is up for c in cooks), "the parent chain is the parent's to cook, and it is clean"


def test_a_group_whose_inside_has_cooked_is_not_stale(network):
    group, inner = network["group"], network["inner"]
    chunk_cook.mark_cook_stale(group)
    assert not chunk_cook.is_cook_stale(inner)

    chunk_stage._clear_cooked_groups(list(network["scene"].get_chunks()))

    assert not chunk_cook.is_cook_stale(group)


def test_a_group_with_anything_stale_inside_stays_stale(network):
    group, inner = network["group"], network["inner"]
    chunk_cook.mark_cook_stale(group)
    chunk_cook.mark_cook_stale(inner)

    chunk_stage._clear_cooked_groups(list(network["scene"].get_chunks()))

    assert chunk_cook.is_cook_stale(group), "the recook still has to force its inside"


def _make_view_active(network):
    stage, view, root = network["stage"], network["view"], network["root"]
    prim = stage.GetPrimAtPath(get_chunk_prim_path(view))
    slot_attr = prim.GetAttribute(chunk_graph.VIEW_SLOT_ATTR)
    if not (slot_attr and slot_attr.Get()):
        prim.CreateAttribute(chunk_graph.VIEW_SLOT_ATTR, Sdf.ValueTypeNames.Int, custom=True).Set(1)
    chunk_graph.write_active_slot(stage, root.prim_path, prim.GetAttribute(chunk_graph.VIEW_SLOT_ATTR).Get())


def test_a_host_that_opts_in_skips_roots_no_active_view_reaches(network):
    _make_view_active(network)
    scene = network["scene"]
    scene.load_cooks_active_views_only = True

    unseen = chunk_stage._unseen_by_active_views(network["stage"], scene)

    assert unseen(network["stray"])
    for name in ("up", "group", "inner", "view"):
        assert not unseen(network[name]), f"{name} feeds the active view"


def test_without_opting_in_every_root_cooks(network):
    """A script or the CLI never runs the view recook, so it needs the load to cook it all."""
    _make_view_active(network)

    unseen = chunk_stage._unseen_by_active_views(network["stage"], network["scene"])

    assert not unseen(network["stray"])


def test_a_group_recooked_by_a_loaded_callback_still_loads_clean(tmp_path, monkeypatch):
    """The group clear runs last in a load. A CHUNK_LOADED callback can cook again -- Render
    Settings does, once its renderer groups exist -- and that cook solves up the ownership
    chain, which marks the group stale all over again. Cleared any earlier, the first
    recook forced the group's whole inside again (85 cooks on solids_kitti.usda)."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene()
    scene.stage = stage
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)

    def mk(key, context=None):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        prev = scene._current_context
        scene._current_context = weakref.ref(context) if context is not None else None
        try:
            for c in cs:
                scene.add_chunk(c)
        finally:
            scene._current_context = prev
        return cs[0]

    group = mk("chunkgroup")
    inner = mk("prim", context=group)
    inner.get_param("primpath_location").value = "/inner"
    group_path, inner_path = get_chunk_prim_path(group), get_chunk_prim_path(inner)
    out = tmp_path / "net.usda"
    chunk_stage.export_chunks(scene, str(out))

    real_send = chunk_notice.send_notice

    def recook_inner_when_loaded(alias, sender, *a, **k):
        result = real_send(alias, sender, *a, **k)
        if alias is chunk_notice.ChunkNoticeAlias.CHUNK_LOADED and get_chunk_prim_path(sender) == inner_path:
            sender.execute()
        return result

    monkeypatch.setattr(chunk_notice, "send_notice", recook_inner_when_loaded)
    loaded = Scene.open(str(out))
    try:
        by_path = {get_chunk_prim_path(c): c for c in loaded.get_chunks()}
        assert inner_path in by_path and group_path in by_path, "precondition: the network reloaded"
        assert not chunk_cook.is_cook_stale(by_path[inner_path])
        assert not chunk_cook.is_cook_stale(by_path[group_path]), \
            "a group whose inside the load cooked must not be forced again by the first recook"
    finally:
        loaded.close()


# --- the graph's reverse-wire index ------------------------------------------------------------


def _graph_scene(network):
    view = network["docker"].tab_widget.get_current_graph_view()
    return view.scene()


def test_the_fed_outputs_index_follows_the_wires(network):
    """Every node asked "which of my outputs are wired" by reading every other node, on every
    port placement -- 84k scans to dive into an 86-node group. It is read once now, so it
    has to notice a wire being made."""
    scene = _graph_scene(network)
    up_path = get_chunk_prim_path(network["up"])
    stray = network["stage"].GetPrimAtPath(get_chunk_prim_path(network["stray"]))
    assert up_path in scene._nodes and str(stray.GetPath()) in scene._nodes, "precondition"
    assert "bar" not in scene.fed_outputs(up_path)

    stray.CreateAttribute("inputs:foo", Sdf.ValueTypeNames.Float).SetConnections(
        [Sdf.Path(f"{up_path}.outputs:bar")])

    assert "bar" in scene.fed_outputs(up_path)


def test_a_plain_attribute_write_keeps_the_index(network):
    """A cook writes hundreds of attributes, and none of them can move a wire."""
    scene = _graph_scene(network)
    up = network["stage"].GetPrimAtPath(get_chunk_prim_path(network["up"]))
    attr = up.CreateAttribute("plain", Sdf.ValueTypeNames.Float)
    scene.fed_outputs(str(up.GetPath()))
    index = scene._fed_outputs_index
    assert index is not None

    attr.Set(2.0)

    assert scene._fed_outputs_index is index


# --- global USD listeners hear only their own stage ------------------------------------------


def test_a_chunks_global_listener_ignores_other_stages(network):
    heard: list = []

    def listener(owner, notice, sender):
        heard.append(notice.GetStage())

    c = network["up"]
    c.add_callback(listener, listen_globally=True,
                   notice_alias=chunk_notice.ChunkNoticeAlias.OBJECT_CHANGED)

    other = Usd.Stage.CreateInMemory()
    other.DefinePrim("/elsewhere")
    assert heard == [], "an edit on a stage nothing here owns reached the chunk"

    network["stage"].DefinePrim("/here")
    assert heard, "and an edit on its own stage still does"


def test_the_stage_sync_knows_a_foreign_stage(network):
    sync = chunk_stage.ChunkStageSync()
    assert sync._is_foreign_stage(Usd.Stage.CreateInMemory())
    assert not sync._is_foreign_stage(network["stage"])
    assert not sync._is_foreign_stage(None)


# --- a collapsed group builds its contents on first expand -----------------------------------


@pytest.fixture
def folded(qapp):
    scene = _Scene()
    scene.stage = Usd.Stage.CreateInMemory()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")

    c = chunk.Chunk("folded")
    shut = chunk.Group("shut", flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE)
    for i in range(10):
        shut.add_param(chunk.Slider(f"s{i}", default_value=0.0))
    shut.set_state(chunk.ChunkFlags.State.COLLAPSED)
    c.add_param(shut)
    opened = chunk.Group("open", flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE)
    opened.add_param(chunk.Slider("o0", default_value=0.0))
    c.add_param(opened)
    scene.add_chunk(c)

    w = ChunkWidget(c)
    boxes = {b.group.name: b for b in w.findChildren(ContainerWidget)}
    yield dict(qapp=qapp, chunk=c, widget=w, shut=boxes["shut"], open=boxes["open"])
    w.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qapp.processEvents()


def _param_widgets(box):
    return [x for x in box.findChildren(QtCore.QObject) if isinstance(x, ChunkParamWidgetBase)]


def test_a_collapsed_group_builds_nothing_until_it_is_opened(folded):
    assert _param_widgets(folded["shut"]) == []
    assert len(_param_widgets(folded["open"])) == 1, "an expanded group builds as before"


def test_opening_it_builds_its_contents(folded):
    shut = folded["shut"]
    shut.minimize_button.setChecked(True)
    folded["qapp"].processEvents()

    assert len(_param_widgets(shut)) == 10
    assert not shut.group.has_state(chunk.ChunkFlags.State.COLLAPSED)


def test_params_added_while_folded_arrive_with_the_rest(folded):
    shut = folded["shut"]
    shut.group.add_param(chunk.Slider("late", default_value=0.0))

    shut.minimize_button.setChecked(True)
    folded["qapp"].processEvents()

    names = sorted(w.param.name for w in _param_widgets(shut))
    assert names == sorted([f"s{i}" for i in range(10)] + ["late"]), "built once, not twice"


def test_a_chunks_own_body_is_always_built(folded):
    """A collapsed node hides its body whole, and the node graph measures ports off it."""
    c = folded["chunk"]
    c.set_state(chunk.ChunkFlags.State.COLLAPSED)
    w = ChunkWidget(c)
    try:
        assert w._children_built
    finally:
        w.deleteLater()
