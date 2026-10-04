"""Tests for the topological solve engine in usd_chunks.chunk_scheduler.

These build DAGs out of bare ChunkNoticePropagator subclasses rather than real chunks,
so the ordering, dirty propagation and change-reason behaviour can be asserted without
dragging a USD stage into it.
"""
from __future__ import annotations

import random

import pytest

from usd_chunks import chunk_notice, chunk_scheduler
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


class PresentationNode(Node):
    """A node whose change nothing computes from, like a hidden/collapsed/selected bit."""

    def is_presentation_change(self):
        return True


def solve_order(propagator):
    """Run a solve from propagator and return the names of the nodes it yielded."""
    propagator.get_dag_node().dirty = True
    return [n.propagator.name for n in propagator.get_dag_node().solve_outputs() if n.propagator]


def clean(*propagators):
    """Mark every node clean and drop its change reasons, as if a solve had just finished."""
    for p in propagators:
        node = p.get_dag_node()
        node._dirty = False
        node._changes.clear()
        node._index = 0


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
        # always made it plain "dirty". Pinned here because chunk_stage's load path
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

    def test_owner_records_the_seeds_alias_not_the_middlemans(self):
        # A state change reaches a chunk through the param or group holding it, and that
        # middleman used to stamp its own PARAM_CHANGED/GROUP_CHANGED on the way up --
        # which made a collapsed group read as a data change and re-cooked the module.
        chunk = Node("chunk", ChunkNoticeAlias.CHUNK_CHANGED)
        group = Node("group", ChunkNoticeAlias.GROUP_CHANGED)
        state = PresentationNode("state", ChunkNoticeAlias.STATE_CHANGED)
        group.set_owner(chunk)
        state.set_owner(group)
        clean(chunk, group, state)

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


class TestPresentationVerdict:
    """only_presentation_changed() is what execute() gates the module re-run on."""

    def test_a_data_change_is_not_presentation(self):
        chunk = Node("chunk", ChunkNoticeAlias.CHUNK_CHANGED)
        param = Node("param", ChunkNoticeAlias.PARAM_CHANGED)
        param.set_owner(chunk)
        clean(chunk, param)

        param.get_dag_node().dirty = True

        assert chunk.get_dag_node().only_presentation_changed() is False

    def test_a_state_change_is_presentation(self):
        chunk = Node("chunk", ChunkNoticeAlias.CHUNK_CHANGED)
        state = PresentationNode("state", ChunkNoticeAlias.STATE_CHANGED)
        state.set_owner(chunk)
        clean(chunk, state)

        state.get_dag_node().dirty = True

        assert chunk.get_dag_node().only_presentation_changed() is True

    def test_presentation_is_recognised_without_a_state_changed_alias(self):
        # The regression this replaced: execute() used to test the *alias*, so a
        # presentation_only param -- which raises PARAM_CHANGED, not STATE_CHANGED --
        # read as a data change at dispatch even though propagate_dirty had already
        # routed it as presentation. The Scatter Brush's radius is this case.
        chunk = Node("chunk", ChunkNoticeAlias.CHUNK_CHANGED)
        param = PresentationNode("param", ChunkNoticeAlias.PARAM_CHANGED)
        param.set_owner(chunk)
        clean(chunk, param)

        param.get_dag_node().dirty = True

        aliases = [alias for alias, _ in chunk.get_dag_node().iter_changes()]
        assert aliases == [ChunkNoticeAlias.PARAM_CHANGED], "setup no longer models the bug"
        assert chunk.get_dag_node().only_presentation_changed() is True

    def test_one_data_change_among_presentation_ones_still_cooks(self):
        chunk = Node("chunk", ChunkNoticeAlias.CHUNK_CHANGED)
        state = PresentationNode("state", ChunkNoticeAlias.STATE_CHANGED)
        param = Node("param", ChunkNoticeAlias.PARAM_CHANGED)
        state.set_owner(chunk)
        param.set_owner(chunk)
        clean(chunk, state, param)

        state.get_dag_node().dirty = True
        param.get_dag_node().dirty = True

        assert chunk.get_dag_node().only_presentation_changed() is False

    def test_nothing_recorded_is_not_presentation(self):
        # A direct dirty solve -- execute-on-creation -- carries no propagated change and
        # must still cook.
        chunk = Node("chunk", ChunkNoticeAlias.CHUNK_CHANGED)
        clean(chunk)

        assert chunk.get_dag_node().only_presentation_changed() is False


class TestPresentationChanges:
    def test_dependants_are_left_clean(self):
        # Selecting a node used to dirty every chunk downstream of it along the exec chain.
        owner = Node("owner")
        state = PresentationNode("state", ChunkNoticeAlias.STATE_CHANGED)
        state.set_owner(owner)
        dependant = Node("dependant")
        dependant.add_dependency(owner)
        clean(owner, state, dependant)

        state.get_dag_node().dirty = True

        assert owner.get_dag_node().dirty
        assert not dependant.get_dag_node().dirty

    def test_the_whole_ownership_chain_is_dirtied(self):
        # The UI hangs off the owners: a group's data_changed spreads DISABLED to nested
        # chunks, the SceneSender's keeps the selection set. They have to solve.
        scene, chunk, param = Node("scene"), Node("chunk"), Node("param")
        state = PresentationNode("state", ChunkNoticeAlias.STATE_CHANGED)
        chunk.set_owner(scene)
        param.set_owner(chunk)
        state.set_owner(param)
        clean(scene, chunk, param, state)

        state.get_dag_node().dirty = True

        assert all(n.get_dag_node().dirty for n in (param, chunk, scene))

    def test_a_data_change_still_dirties_dependants(self):
        owner, dependant = Node("owner"), Node("dependant")
        value = Node("value", ChunkNoticeAlias.ATTRIBUTE_CHANGED)
        value.set_owner(owner)
        dependant.add_dependency(owner)
        clean(owner, value, dependant)

        value.get_dag_node().dirty = True

        assert dependant.get_dag_node().dirty


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

    def test_cached_depths_survive_any_sequence_of_rewiring(self):
        # Invalidation stops walking at the first node with no cached depth, which is only
        # sound while nothing downstream of an uncached node is cached. Checked against an
        # independent depth after every edit, over a few hundred random ones -- edges and
        # owners both -- asking only a few nodes each time, so the cache stays part-filled
        # the way a real load leaves it.
        rng = random.Random(7)
        nodes = [Node(f"n{i}") for i in range(40)]
        edges = []

        def expected(dag, memo):
            if dag not in memo:
                memo[dag] = max(
                    [1 + expected(d, memo) for d in dag._connections.inputs.values()]
                    + [expected(o, memo) for o in (dag._owned or ())],
                    default=0,
                )
            return memo[dag]

        for _ in range(300):
            lo, hi = sorted(rng.sample(range(len(nodes)), 2))
            action = rng.random()
            if action < 0.45:
                nodes[hi].add_dependency(nodes[lo])  # lower index upstream: stays acyclic
                edges.append((hi, lo))
            elif action < 0.65 and edges:
                down, up = edges.pop(rng.randrange(len(edges)))
                nodes[down].remove_dependency(nodes[up])
            else:
                nodes[lo].set_owner(nodes[hi])

            memo = {}
            for i in rng.sample(range(len(nodes)), 5):
                dag = nodes[i].get_dag_node()
                assert dag.get_sort_key() == expected(dag, memo), f"n{i}"


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


def _reasons(*propagators):
    """Each node's recorded change reasons, by name, for comparing two graphs."""
    return {p.name: sorted((alias.name, n.propagator.name) for alias, n in p.get_dag_node()._changes)
            for p in propagators}


class TestDeferredDirty:
    """chunk_scheduler.deferred_dirty: one downstream walk for many dirtied nodes."""

    def test_nothing_below_is_dirtied_until_the_block_ends(self):
        a, b, c = Node("a"), Node("b"), Node("c")
        b.add_dependency(a)
        c.add_dependency(b)
        clean(a, b, c)

        with chunk_scheduler.deferred_dirty():
            a.get_dag_node().dirty = True
            assert a.get_dag_node().dirty
            assert not b.get_dag_node().dirty and not c.get_dag_node().dirty

        assert b.get_dag_node().dirty and c.get_dag_node().dirty

    def test_one_walk_records_what_a_walk_per_seed_would(self):
        def graph():
            a, b, c, d = Node("a"), Node("b"), Node("c"), Node("d")
            c.add_dependency(a)
            c.add_dependency(b)
            d.add_dependency(c)
            clean(a, b, c, d)
            return a, b, c, d

        walked = graph()
        for seed in walked[:2]:
            seed.get_dag_node().dirty = True

        deferred = graph()
        with chunk_scheduler.deferred_dirty():
            for seed in deferred[:2]:
                seed.get_dag_node().dirty = True

        assert _reasons(*deferred) == _reasons(*walked)
        assert [p.get_dag_node().dirty for p in deferred] == [p.get_dag_node().dirty for p in walked]

    def test_wiring_a_graph_inside_the_block_leaves_it_ready_to_solve(self):
        # What loading a network does: every edge added inside the block, one solve after.
        with chunk_scheduler.deferred_dirty():
            a, b, c = Node("a"), Node("b"), Node("c")
            b.add_dependency(a)
            c.add_dependency(b)

        assert solve_order(a) == ["a", "b", "c"]

    def test_presentation_changes_are_not_held_back(self):
        owner, child = Node("owner"), PresentationNode("child")
        child.set_owner(owner)
        clean(owner, child)

        with chunk_scheduler.deferred_dirty():
            child.get_dag_node().dirty = True
            assert owner.get_dag_node().dirty


class Counting(Node):
    """A node that records every time a solve reaches it."""

    log: list = []

    def send_self(self):
        Counting.log.append(self.name)


class TestBatchedSolve:
    """chunk_notice.batched_solve: many changes, one solve of what they feed."""

    def setup_method(self):
        Counting.log = []

    def graph(self):
        a, b, c = Counting("a"), Counting("b"), Counting("c")
        c.add_dependency(a)
        c.add_dependency(b)
        clean(a, b, c)
        Counting.log = []
        return a, b, c

    def test_without_a_batch_each_change_solves_what_it_feeds(self):
        a, b, _c = self.graph()
        a.recook()
        b.recook()
        assert Counting.log.count("c") == 2

    def test_a_batch_solves_what_its_changes_feed_once(self):
        a, b, _c = self.graph()
        with chunk_notice.batched_solve():
            a.recook()
            b.recook()
            assert Counting.log == []  # nothing solves inside the block
        assert Counting.log.count("c") == 1
        assert sorted(Counting.log) == ["a", "b", "c"]

    def test_nested_batches_join_the_outermost(self):
        a, b, _c = self.graph()
        with chunk_notice.batched_solve():
            a.recook()
            with chunk_notice.batched_solve():
                b.recook()
            assert Counting.log == []
        assert Counting.log.count("c") == 1

    def test_a_solve_asked_for_while_draining_is_queued_not_nested(self):
        # A param binding its storage attr sets its value mid-solve, which asks for a solve.
        # Run inside the current one, dozens of those nested deep enough to blow the stack.
        depth = {"now": 0, "max": 0}
        a, b, _c = self.graph()

        class Reentrant(Counting):
            def send_self(self):
                depth["now"] += 1
                depth["max"] = max(depth["max"], depth["now"])
                super().send_self()
                if self.name == "a":
                    b.recook()  # asked for while a is being solved
                depth["now"] -= 1

        a.__class__ = Reentrant
        b.__class__ = Reentrant
        with chunk_notice.batched_solve():
            a.recook()

        assert depth["max"] == 1
        assert Counting.log.count("c") == 1
