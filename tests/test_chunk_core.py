from __future__ import annotations

# import usd_chunks.chunk_registry  # noqa: F401  # import-order workaround for chunk_core/chunk_registry circular init
from pxr import Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ProxyParam, SceneSender
from usd_chunks.chunk_notice import ChunkNoticeAlias


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


reg = chunk_registry.ChunkRegistry()
reg.initialize(_DummyScene(), "pytest")


class _LayerContext:
    def __init__(self, mgr):
        self._mgr = mgr

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc, tb):
        for g_layer in self._mgr.graph_layers.values():
            g_layer.Clear()
        self._mgr.graph_layers.clear()
        self._mgr.chunk_layers.clear()
        self._mgr.initialized = False


def _double_first_dependency(owner: chunk.Slider, sender):
    dep = owner.get_dependency(0, chunk.is_param)
    return dep.value * 2


def _scene_callback(owner, notice, sender):
    # Intentionally no-op; used for verifying callback connection persistence.
    return None


def test_group_add_param_enforces_unique_names():
    group = chunk.Group("root")
    slider_a = chunk.Slider("value")
    slider_b = chunk.Slider("value")

    group.add_param(slider_a)
    group.add_param(slider_b)

    assert group.get_param("value") is slider_a
    assert group.get_param("value1") is slider_b


def test_group_get_param_supports_nested_dot_path():
    root = chunk.Group("root")
    nested = chunk.Group("nested")
    leaf = chunk.Slider("leaf")

    nested.add_param(leaf)
    root.add_param(nested)

    assert root.get_param("nested.leaf") is leaf


def test_clone_does_not_copy_callbacks():
    calls: list[str] = []

    def on_value_change(owner, notice, sender):
        calls.append(owner.name)

    slider = chunk.Slider("src")
    slider.add_callback(on_value_change, notice_alias=ChunkNoticeAlias.ATTRIBUTE_CHANGED)
    slider_copy = slider.clone()

    slider.value = 1
    slider_copy.value = 2

    assert calls == ["src"]


def test_set_function_evaluates_from_dependencies():
    driver = chunk.Slider("driver")
    target = chunk.Slider("target")
    target.add_dependency(driver)

    def evaluate_value(owner: chunk.Slider, sender):
        dep = owner.get_dependency(0, chunk.is_param)
        return dep.value * 2

    target.set_function("value", evaluate_value)
    driver.value = 4

    assert target.value == 8


def test_list_group_creates_proxy_params():
    list_group = chunk.ListGroup("items")
    source_param = chunk.Slider("item_value")
    list_group.add_param(source_param)
    list_group.size = 2

    p0 = list_group.get_indexed_param("item_value", 0)
    p1 = list_group.get_indexed_param("item_value", 1)

    assert isinstance(p0, ProxyParam)
    assert isinstance(p1, ProxyParam)
    assert p0.name == "item_value"
    assert p1.name == "item_value"


def test_list_group_resize_does_not_reassign_the_shared_graph_id():
    """Regression (issue #81): growing a ListGroup after it's already sharing a graph_id
    with siblings used to silently reassign that whole graph's id. ProxyParam.__init__'s
    add_dependency(param) call merges graph_id like any other dependency -- and since the
    proxy is brand new, the merge ran backwards, making the *established* root (the tab
    and everything in it) subordinate to the fresh proxy's root instead of the other way
    around. Anything created afterward then computed a different graph_key and landed in
    a brand-new graph_XXXX layer instead of the tab's existing one."""
    tab = chunk.Group("tab")
    sibling = chunk.Slider("sibling")
    list_group = chunk.ListGroup("items")
    list_group.add_param(chunk.Slider("item_value"))

    tab.add_param(sibling)
    tab.add_param(list_group)

    before = tab.get_dag_node().get_graph_id()
    assert sibling.get_dag_node().get_graph_id() == before
    assert list_group.get_dag_node().get_graph_id() == before

    list_group.size = 2  # creates ProxyParams for item_value, as the UI's "+" row does

    assert tab.get_dag_node().get_graph_id() == before
    assert sibling.get_dag_node().get_graph_id() == before
    assert list_group.get_dag_node().get_graph_id() == before


def test_vector_get_set_item_and_tuple_value():
    vec = chunk.Vector("vec", param=chunk.Slider("axis"), length=3, suffixes="XYZ")

    assert tuple(p.name for p in vec.get_my_params()) == ("axisX", "axisY", "axisZ")

    vec[0] = 1
    vec["axisY"] = 2
    vec.value = (3, 4, 5)

    assert vec.value == (3, 4, 5)


def test_complex_dependency_graph_with_four_inputs():
    # Build a small DAG:
    #   in_a,in_b -> left
    #   in_c,in_d -> right
    #   left,right -> total
    in_a = chunk.Slider("in_a")
    in_b = chunk.Slider("in_b")
    in_c = chunk.Slider("in_c")
    in_d = chunk.Slider("in_d")

    left = chunk.Slider("left")
    right = chunk.Slider("right")
    total = chunk.Slider("total")

    left.add_dependency(in_a)
    left.add_dependency(in_b)
    right.add_dependency(in_c)
    right.add_dependency(in_d)
    total.add_dependency(left)
    total.add_dependency(right)

    left.set_function(
        "value",
        lambda owner, sender: (
            owner.get_dependency(0, chunk.is_param).value + owner.get_dependency(1, chunk.is_param).value
        ),
    )
    right.set_function(
        "value",
        lambda owner, sender: (
            owner.get_dependency(0, chunk.is_param).value + owner.get_dependency(1, chunk.is_param).value
        ),
    )
    total.set_function(
        "value",
        lambda owner, sender: (
            owner.get_dependency(0, chunk.is_param).value + owner.get_dependency(1, chunk.is_param).value
        ),
    )

    in_a.value = 1
    in_b.value = 2
    in_c.value = 3
    in_d.value = 4

    assert left.value == 3
    assert right.value == 7
    assert total.value == 10

    # Changing one root node should propagate through both intermediate and final nodes.
    in_d.value = 10

    assert right.value == 13
    assert total.value == 16


def test_remove_param_or_group_removes_child_and_owner_link():
    group = chunk.Group("root")
    slider = chunk.Slider("to_remove")
    group.add_param(slider)

    assert group.get_param("to_remove") is slider
    assert slider.get_owner() is not None

    group.remove_param_or_group(slider)

    assert group.get_param("to_remove") is None
    assert slider.get_owner() is None


def test_request_delete_param_removes_it_from_group():
    group = chunk.Group("root")
    slider = chunk.Slider("victim")
    group.add_param(slider)

    slider.request_delete()

    assert group.get_param("victim") is None
    assert slider.get_owner() is None


def test_list_group_remove_index_updates_size_and_proxies():
    list_group = chunk.ListGroup("items")
    list_group.add_param(chunk.Slider("v"))
    list_group.size = 3

    assert len(list_group.proxy_params) == 3

    list_group.remove_index(1)

    assert list_group.size == 2
    assert len(list_group.proxy_params) == 2
    assert tuple(g.name for g in list_group.proxy_params) == ("0", "1")


def test_remove_chunk_with_layers():
    from pxr import Usd

    scene = _DummyScene()
    stage = Usd.Stage.CreateInMemory()

    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")

    lreg = chunk_registry.ChunkLayerManager()
    lreg.initialize(stage)

    with _LayerContext(lreg):
        c = chunk.Chunk("test")
        ctx = scene.add_context("acontext")

        if not ctx:
            raise ValueError
        ctx.add_param(c)

        assert lreg.get_or_create_layer(c)

        ctx.request_delete()

        h = c.get_hash()

        del ctx
        del c
        assert lreg.get_layer(h) is None


def test_loft_connection_not_reconstructed_as_expression():
    """A loft connection points UP to the param's own container (the group's interface attr).
    _connect_param_attr must NOT rebuild it as a sibling expression @Container.attr@ -- that made
    an unresolvable ref ('Expression ref not found' spam) and briefly overwrote the forwarded
    value with the expression text. Sibling connections must still reconstruct as expressions."""
    from pxr import Usd, Sdf

    scene = _DummyScene()
    stage = Usd.Stage.CreateInMemory()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")

    # /a/b (container) with inputs:tx ; /a/b/c (inner) whose inputs:tx lofts UP to /a/b.inputs:tx.
    stage.DefinePrim("/a/b").CreateAttribute("inputs:tx", Sdf.ValueTypeNames.Float, custom=True).Set(1.0)
    c_attr = stage.DefinePrim("/a/b/c").CreateAttribute("inputs:tx", Sdf.ValueTypeNames.Float, custom=True)
    c_attr.Set(3.0)
    c_attr.SetConnections([Sdf.Path("/a/b.inputs:tx")])

    p = chunk.Slider("tx", default_value=0.0)
    p._connect_param_attr(c_attr)
    # A loft (upward) connection must not turn the value into an expression string.
    assert not isinstance(p.raw_value, str)

    # A sibling connection SHOULD still reconstruct as an expression.
    stage.DefinePrim("/a/b/e").CreateAttribute("outputs:tx", Sdf.ValueTypeNames.Float, custom=True).Set(7.0)
    d_attr = stage.DefinePrim("/a/b/d").CreateAttribute("inputs:tx", Sdf.ValueTypeNames.Float, custom=True)
    d_attr.Set(2.0)
    d_attr.SetConnections([Sdf.Path("/a/b/e.outputs:tx")])
    p2 = chunk.Slider("tx", default_value=0.0)
    p2._connect_param_attr(d_attr)
    assert p2.raw_value == "@e.tx@"


def test_sender_scoped_callback_triggers_only_for_matching_sender():
    received: list[str] = []
    root = chunk.Chunk("root")
    btn_a = chunk.Button("a")
    btn_b = chunk.Button("b")
    root.add_params([btn_a, btn_b])

    def on_button(owner, notice, sender):
        received.append(sender.name)

    root.add_callback(on_button, sender=btn_a)

    btn_b.execute()
    btn_a.execute()

    assert received == ["a"]


def test_menu_invalid_value_reverts_to_first_item():
    menu = chunk.Menu("mode", menu_items=["hello0", "hello1"])

    menu.value = "hello1"
    assert menu.value == "hello1"

    menu.value = "not-in-menu"
    assert menu.value == "hello0"




class _AChunk(chunk.Chunk):
    def __init__(self):
        super().__init__(name="_TEST")

        grp = self.add_param(chunk.Group("grp"))
        self.slider = grp.add_param(chunk.Slider("slider"))






def test_get_param_by_path_with_duplicate_names():
    ctx = chunk.ChunkBase("ctx", flags=chunk.ChunkFlags.GroupFlags.CONTEXT)

    A = chunk.Chunk("A")
    P = chunk.Slider("P")
    A.add_param(P)
    A1 = A.clone()

    ctx.add_params([A, A1])

    assert ctx.get_param("A") == A
    assert ctx.get_param("A1") == A1
    assert ctx.get_param("A.P") == P
    assert ctx.get_param("A1.P") == A1.get_param("P")

def test_select_deselect_chunk():
    scene = _DummyScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")

    A = chunk.Chunk("A")
    B = chunk.Chunk("B")

    reg.scene.add_chunks([A, B])

    A.set_state(chunk.ChunkFlags.State.SELECTED)
    sel = scene.get_selected_chunks()
    assert len(sel) == 1 and sel[0] == A

    scene.set_selected_chunks([B])
    sel = scene.get_selected_chunks()
    assert len(sel) == 1 and sel[0] == B

    B.unset_state(chunk.ChunkFlags.State.SELECTED)
    sel = scene.get_selected_chunks()
    assert len(sel) == 0


def test_state_change_does_not_reexecute_module():
    """A pure state change (e.g. selecting a node) must not re-run the chunk's module -- only
    data changes (a param value) should. Selection/collapse used to cascade into module.execute,
    so selecting a node re-authored its output."""
    stage = Usd.Stage.CreateInMemory()

    class _Scene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _Scene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")

    calls = []

    class _Mod:
        def execute(self, prop):
            calls.append(1)

    c = chunk.Chunk("c")
    slider = chunk.Slider("s", default_value=0.0)
    c.add_param(slider)
    scene.add_chunk(c)
    c._module = _Mod()

    calls.clear()
    c.set_state(chunk.ChunkFlags.State.SELECTED)
    c.unset_state(chunk.ChunkFlags.State.SELECTED)
    assert calls == []  # selection/deselection does not execute the module

    slider.value = 5.0
    assert len(calls) >= 1  # a data change does execute the module


def _module_call_counter(chunk_obj):
    """Attach a counting stub module to chunk_obj and return the call list."""
    calls = []

    class _Mod:
        def execute(self, prop):
            calls.append(1)

    chunk_obj._module = _Mod()
    return calls


def _stage_scene():
    stage = Usd.Stage.CreateInMemory()

    class _Scene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _Scene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    return scene


def test_collapsing_a_group_does_not_reexecute_module():
    """Folding a group open/closed is a UI preference, not an input to what the chunk
    computes. The state change reaches the chunk through the group, which used to stamp its
    own GROUP_CHANGED on the way up and make the solve look like a data change."""
    scene = _stage_scene()

    c = chunk.Chunk("collapse_me")
    g = chunk.Group("g", flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE)
    g.add_param(chunk.Slider("s", default_value=0.0))
    c.add_param(g)
    scene.add_chunk(c)
    calls = _module_call_counter(c)

    g.set_state(chunk.ChunkFlags.State.COLLAPSED)
    g.unset_state(chunk.ChunkFlags.State.COLLAPSED)

    assert calls == []


def test_hiding_a_param_does_not_reexecute_module():
    """Hiding is a consequence of a chunk reconfiguring, never an input to it: the change
    that hid the param is what should dirty the chunk, not the hiding itself."""
    scene = _stage_scene()

    c = chunk.Chunk("hide_me")
    slider = chunk.Slider("s", default_value=0.0)
    c.add_param(slider)
    scene.add_chunk(c)
    calls = _module_call_counter(c)

    slider.set_state(chunk.ChunkFlags.State.HIDDEN)
    slider.unset_state(chunk.ChunkFlags.State.HIDDEN)

    assert calls == []


def test_selecting_a_chunk_does_not_recook_downstream():
    """A presentation state change must not travel along the exec chain either -- selecting
    one node used to dirty every chunk downstream of it and re-cook the lot."""
    scene = _stage_scene()

    upstream = chunk.Chunk("upstream")
    downstream = chunk.Chunk("downstream")
    scene.add_chunks([upstream, downstream])
    downstream.add_dependency(upstream)

    calls = _module_call_counter(downstream)
    # Cook once so the node is clean, as it is in a live graph. A node that is already
    # dirty gets swept into whatever solve passes it next, which would mask the point.
    downstream.execute()
    calls.clear()

    upstream.set_state(chunk.ChunkFlags.State.SELECTED)
    upstream.unset_state(chunk.ChunkFlags.State.SELECTED)

    assert calls == []
    assert not downstream.get_dag_node().dirty


def test_disabling_a_chunk_recooks_downstream():
    """DISABLED is the one state bit that is not presentation: it decides whether the chunk
    cooks at all, so everything reading its output has to run again."""
    scene = _stage_scene()

    upstream = chunk.Chunk("disable_up")
    downstream = chunk.Chunk("disable_down")
    scene.add_chunks([upstream, downstream])
    downstream.add_dependency(upstream)

    calls = _module_call_counter(downstream)
    downstream.execute()
    calls.clear()

    upstream.set_state(chunk.ChunkFlags.State.DISABLED)

    assert len(calls) >= 1


def test_module_does_not_execute_without_a_stage():
    """Every chunk module works through get_stage(), so a stage-less solve must not run one.

    Hosts do go stageless for a window -- usdview clears its stage before opening the next
    file while still pumping the Qt event loop, clearing the prim selection in there -- and a
    param write from that churn used to reach the module and crash it on its first stage
    call ('NoneType' object has no attribute 'DefinePrim')."""
    scene = _DummyScene()  # get_stage() -> None
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")

    calls = []

    class _Mod:
        def execute(self, prop):
            calls.append(1)

    c = chunk.Chunk("stageless")
    slider = chunk.Slider("s", default_value=0.0)
    c.add_param(slider)
    scene.add_chunk(c)
    c._module = _Mod()

    calls.clear()
    slider.value = 5.0
    assert calls == []