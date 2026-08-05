"""Tests for the topological solve engine in usd_chunks.chunk_scheduler.

These build DAGs out of bare ChunkNoticePropagator subclasses rather than real chunks,
so the ordering, dirty propagation and change-reason behaviour can be asserted without
dragging a USD stage into it.
"""

import pytest

from usd_chunks import chunk_scheduler
from usd_chunks.chunk_notice import ChunkNoticeAlias, ChunkNoticePropagator


class Node(ChunkNoticePropagator):
    """A minimal propagator: just a name and a notice alias."""

    def __init__(self, name, alias=ChunkNoticeAlias.VALUE_CHANGED):
        super().__init__()
        self.name = name
        self._alias = alias

    def get_notice_alias(self):
        return self._alias

    def is_blocked(self):
        return False

    def __repr__(self):
        return f"Node({self.name})"


def solve_order(propagator):
    """Run a solve from propagator and return the names of the nodes it yielded."""
    propagator.get_dag_node().dirty = True
    return [n.propagator.name for n in propagator.get_dag_node().solve_outputs() if n.propagator]


def clean(*propagators):
    """Mark every node clean, as if a solve had just finished."""
    for p in propagators:
        p.get_dag_node()._dirty = False


class TestTopologicalOrder:
    def test_chain_solves_upstream_first(self):
        a, b, c = Node("a"), Node("b"), Node("c")
        b.add_dependency(a)
        c.add_dependency(b)

        assert solve_order(a) == ["a", "b", "c"]

    def test_diamond_joins_after_both_branches(self):
        a, b, c, d = Node("a"), Node("b"), Node("c"), Node("d")
        b.add_dependency(a)
        c.add_dependency(a)
        d.add_dependency(b)
        d.add_dependency(c)

        order = solve_order(a)

        assert order.index("d") > order.index("b")
        assert order.index("d") > order.index("c")
        # The join must run once, not once per incoming branch: the old recursive solve
        # walked every path independently.
        assert order.count("d") == 1

    def test_owner_solves_after_owned(self):
        owner, child = Node("owner"), Node("child")
        child.set_owner(owner)

        order = solve_order(child)

        assert order == ["child", "owner"]

    def test_solving_mid_chain_starts_at_the_dirty_frontier(self):
        a, b, c = Node("a"), Node("b"), Node("c")
        b.add_dependency(a)
        c.add_dependency(b)

        # Everything is dirty from construction; asking c to solve must still run a and b
        # first, otherwise c evaluates against a dependency that has not run.
        assert solve_order(c) == ["a", "b", "c"]

    def test_dirty_dependency_on_a_side_branch_is_pulled_in(self):
        # b depends on both a and c. Solving from a must not run b while c, which is also
        # dirty, has not run: c is on a branch the walk down from a never reaches, so the
        # sort has to climb back up to find it.
        a, b, c = Node("a"), Node("b"), Node("c")
        b.add_dependency(a)
        b.add_dependency(c)

        order = solve_order(a)

        assert order.index("c") < order.index("b")

    def test_clean_side_branch_is_left_alone(self):
        a, b, c = Node("a"), Node("b"), Node("c")
        b.add_dependency(a)
        b.add_dependency(c)
        clean(a, b, c)

        assert solve_order(a) == ["a", "b"]

    def test_clean_dependency_does_not_re_solve(self):
        a, b, c = Node("a"), Node("b"), Node("c")
        b.add_dependency(a)
        c.add_dependency(b)
        clean(a, b, c)

        assert solve_order(b) == ["b", "c"]

    def test_terminal_node_still_solves(self):
        # should_solve() reads as "dirty AND (has outputs OR has owner)", but
        # WeakValueDictionary.items() is a generator and so always truthy, which has
        # always made it plain "dirty". Pinned here because chunk_usd's load path
        # documents the opposite; changing it is a behaviour change, not a cleanup.
        lonely = Node("lonely")

        assert solve_order(lonely) == ["lonely"]

    def test_clean_node_is_never_yielded(self):
        a = Node("a")
        clean(a)

        assert [n for n in a.get_dag_node().solve_outputs()] == []


class TestCycleDetection:
    def test_cycle_raises(self):
        a, b = Node("a"), Node("b")
        b.add_dependency(a)

        with pytest.raises(chunk_scheduler.CircularDependencyError):
            a.add_dependency(b)

    def test_cycle_error_is_a_recursion_error(self):
        # The previous recursive propagation raised RecursionError; existing handlers
        # must keep catching it.
        assert issubclass(chunk_scheduler.CircularDependencyError, RecursionError)


class TestChangeReasons:
    def test_dependant_records_input_changed(self):
        a, b = Node("a"), Node("b")
        b.add_dependency(a)

        a.get_dag_node().dirty = True
        changes = list(b.get_dag_node().iter_changes())

        assert (ChunkNoticeAlias.INPUT_CHANGED, a) in changes

    def test_owner_records_the_childs_own_alias(self):
        owner = Node("owner", ChunkNoticeAlias.CHUNK_CHANGED)
        child = Node("child", ChunkNoticeAlias.PARAM_CHANGED)
        child.set_owner(owner)

        child.get_dag_node().dirty = True
        changes = list(owner.get_dag_node().iter_changes())

        assert (ChunkNoticeAlias.PARAM_CHANGED, child) in changes

    def test_owner_inherits_reasons_from_grandchildren(self):
        # The state_only guard in execute() depends on a Chunk seeing the aliases raised
        # by its params' attribute senders, not just "something below me moved".
        chunk = Node("chunk", ChunkNoticeAlias.CHUNK_CHANGED)
        param = Node("param", ChunkNoticeAlias.PARAM_CHANGED)
        sender = Node("sender", ChunkNoticeAlias.ATTRIBUTE_CHANGED)
        param.set_owner(chunk)
        sender.set_owner(param)

        sender.get_dag_node().dirty = True
        aliases = {alias for alias, _ in chunk.get_dag_node().iter_changes()}

        assert ChunkNoticeAlias.ATTRIBUTE_CHANGED in aliases
        assert ChunkNoticeAlias.PARAM_CHANGED in aliases

    def test_state_change_stays_distinguishable_from_a_data_change(self):
        # A pure STATE_CHANGED must not look like a data change, or execute() re-cooks
        # the module on selection.
        chunk = Node("chunk", ChunkNoticeAlias.CHUNK_CHANGED)
        state = Node("state", ChunkNoticeAlias.STATE_CHANGED)
        state.set_owner(chunk)

        state.get_dag_node().dirty = True
        aliases = [alias for alias, _ in chunk.get_dag_node().iter_changes()]

        assert aliases == [ChunkNoticeAlias.STATE_CHANGED]

    def test_reasons_survive_the_whole_pass(self):
        # A ProxyParam reads iter_changes() off the param it references while handling
        # its own solve, so reasons must not be cleared node-by-node.
        a, b = Node("a"), Node("b")
        b.add_dependency(a)

        seen = []
        a.get_dag_node().dirty = True
        for node in a.get_dag_node().solve_outputs():
            seen.append((node.propagator.name, list(a.get_dag_node().iter_changes())))

        assert seen[-1][0] == "b"
        # a's own reasons are still readable while b is being solved.
        assert a.get_dag_node()._changes == {}

    def test_reasons_cleared_after_the_pass(self):
        a, b = Node("a"), Node("b")
        b.add_dependency(a)

        list(solve_order(a))

        assert list(b.get_dag_node().iter_changes()) == []


class TestSortKey:
    def test_chain_depth(self):
        a, b, c = Node("a"), Node("b"), Node("c")
        b.add_dependency(a)
        c.add_dependency(b)

        assert a.get_dag_node().get_sort_key() == 0
        assert b.get_dag_node().get_sort_key() == 1
        assert c.get_dag_node().get_sort_key() == 2

    def test_diamond_depth_is_the_longest_path(self):
        a, b, c, d = Node("a"), Node("b"), Node("c"), Node("d")
        b.add_dependency(a)
        c.add_dependency(b)
        d.add_dependency(a)
        d.add_dependency(c)

        assert d.get_dag_node().get_sort_key() == 3

    def test_owner_is_at_least_as_deep_as_what_it_owns(self):
        # A chunk wired only through a param connection still has to sort after the
        # chunk feeding it -- the dependency edge lives on the params, not the chunks.
        up_chunk, up_param = Node("up_chunk"), Node("up_param")
        down_chunk, down_param = Node("down_chunk"), Node("down_param")
        up_param.set_owner(up_chunk)
        down_param.set_owner(down_chunk)
        down_param.add_dependency(up_param)

        assert down_chunk.get_dag_node().get_sort_key() > up_chunk.get_dag_node().get_sort_key()

    def test_deep_chain_does_not_exhaust_the_stack(self):
        # Graph depth is the user's to decide, so neither the depth walk nor the solve
        # may be bounded by Python's recursion limit.
        chain = [Node(f"n{i}") for i in range(3000)]
        for upstream, downstream in zip(chain, chain[1:]):
            downstream.add_dependency(upstream)

        assert chain[-1].get_dag_node().get_sort_key() == 2999
        assert len(solve_order(chain[0])) == 3000

    def test_depth_updates_when_the_graph_is_rewired(self):
        a, b = Node("a"), Node("b")

        assert b.get_dag_node().get_sort_key() == 0

        b.add_dependency(a)

        assert b.get_dag_node().get_sort_key() == 1


class TestDependencyRemoval:
    """remove_dependency is the counterpart to add_dependency: it exists so a single wire
    can be detached without disconnect() tearing down every edge on the node."""

    def test_removing_a_dependency_drops_it_from_both_sides(self):
        a, b = Node("a"), Node("b")
        b.add_dependency(a)

        assert b.remove_dependency(a) is True
        assert b.get_dependencies() == []
        assert list(a.get_dag_node().get_outputs()) == []

    def test_removing_a_dependency_dirties_the_dependent(self):
        # Losing an input changes what the node evaluates to, so it must re-solve.
        a, b = Node("a"), Node("b")
        b.add_dependency(a)
        clean(a, b)

        b.remove_dependency(a)

        assert b.get_dag_node().dirty

    def test_removing_a_dependency_that_was_never_added_is_a_no_op(self):
        a, b = Node("a"), Node("b")

        assert b.remove_dependency(a) is False

    def test_removal_reorders_the_solve(self):
        a, b, c = Node("a"), Node("b"), Node("c")
        b.add_dependency(a)
        c.add_dependency(b)
        clean(a, b, c)

        c.remove_dependency(b)

        assert solve_order(a) == ["a", "b"]

    def test_disconnect_clears_the_reverse_links_on_peers(self):
        # Clearing only the node's own dicts left every peer still pointing at it, so a
        # disconnected node stayed reachable as somebody else's input or output.
        a, b, c = Node("a"), Node("b"), Node("c")
        b.add_dependency(a)
        c.add_dependency(b)

        b.get_dag_node().disconnect()

        assert list(a.get_dag_node().get_outputs()) == []
        assert c.get_dependencies() == []
        assert list(b.get_dag_node().get_inputs()) == []
        assert list(b.get_dag_node().get_outputs()) == []
