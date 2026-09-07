"""
The chunk_scheduler module owns the *solve* of the chunk DAG.

`chunk_notice.DagNode` stores the topology and nothing else: inputs (dependencies),
outputs (dependants) and a single owner. This module answers the questions that used
to be answered by state maintained incrementally on the node itself:

* which nodes are dirty (`propagate_dirty`)
* in what order must they run (`solve`, via a Kahn topological sort)
* how deep in the graph does a node sit (`get_depth`, the sort key used to order
  chunk sublayers)

Every answer is derived from the topology at the moment it is asked, so nothing has to
be kept in sync while the graph is being built or rewired.

The one piece of per-node state the scheduler does maintain is the *change reason*: when
a node is dirtied, each node it dirties records which ChunkNoticeAlias caused it, which
node it came from, and whether that change was presentation only. That triple is what
`DagNode.iter_changes()` and `DagNode.only_presentation_changed()` replay, and what lets
`ChunkNoticePropagator.execute()` tell a pure state change (selection, collapse, disable)
apart from a data change and skip re-cooking the module. A scheduler that only tracked
"dirty" would re-cook chunks on selection.

The presentation verdict is decided once, here, from `AttributeSender
.is_presentation_change()`, and carried unchanged through both downstream propagation and
ownership inheritance. Consumers read it back rather than re-deriving it: the aliases
alone cannot express it (a `presentation_only` param raises PARAM_CHANGED, not
STATE_CHANGED), and re-asking a sender later reads an old_value/value pair that has since
moved on.

A change nothing computes from -- hiding a param, folding a group open, selecting a node
-- takes `propagate_presentation` instead, which dirties only the ownership chain the UI
hangs off. DISABLED is the one state bit that is not presentation, since it decides
whether the chunk cooks at all.

Ownership is treated as just another downstream edge: an owner solves after everything
it owns, exactly like a dependant solves after its dependency.

After the solve has settled there is a second, much simpler phase: `commit_all`, which
runs each chunk module's reserved `commit(node)` hook once, in dependency order. Where
`execute` is the *cook* -- cheap, idempotent, re-run constantly while the DAG is being
solved -- `commit` is where a chunk writes to disk, which must happen once and only
once the numbers it is writing have stopped moving.
"""

import atexit
import weakref
from typing import TYPE_CHECKING, Callable, Dict, Generator, Iterable, List, Tuple

# Imported as a module rather than by name: chunk_notice imports this module, so at the
# time this line runs chunk_notice may still be halfway through its own import and have
# no attributes yet. Binding the module and reaching into it at call time is what makes
# the cycle work.
from usd_chunks import chunk_notice

if TYPE_CHECKING:
    from usd_chunks.chunk_core import ChunkBase
    from usd_chunks.chunk_notice import DagNode


class CircularDependencyError(RecursionError):
    """
    Raised when the DAG is not acyclic.

    Subclasses RecursionError because that is what the previous recursive dirty
    propagation raised, so existing ``except RecursionError`` handlers keep working.
    """


# Bumped whenever the shape of the graph changes (a connection is made or broken, an
# owner is set). Depths are cached against this value and recomputed after any change.
_topology_epoch: int = 0
_depth_epoch: int = -1
_depth_cache: "weakref.WeakKeyDictionary[DagNode, int]" = weakref.WeakKeyDictionary()

# Dropped before the interpreter starts tearing modules down. This is the only global
# holding weakrefs to graph nodes for an unbounded time, and letting those references die
# in the middle of finalisation puts weakref callbacks in among USD's own C++ teardown.
atexit.register(_depth_cache.clear)


def topology_changed(*nodes: "DagNode"):
    """
    Invalidate everything derived from the graph shape.

    Called by DagNode whenever an input, output or owner connection changes.

    Given the nodes whose edges moved, only the depths that can actually have changed are
    dropped: a node's depth is a function of its dependencies and the things it owns, so
    the nodes affected by an edge change are exactly the ones downstream of it -- the same
    closure a dirty propagation walks. Everything else keeps the depth it already had.

    Passing nothing means "shape changed somewhere", and throws the lot away. That is what
    the old unconditional behaviour did on every single edge, which made building or
    loading a graph quadratic: every add_dependency wiped the cache, so the next depth
    query re-walked the whole chain from scratch.

    Args:
        nodes (DagNode): The nodes whose connections changed, if known
    """
    global _topology_epoch

    if not nodes:
        _topology_epoch += 1
        return

    for node in nodes:
        for stale in downstream_closure(node):
            _depth_cache.pop(stale, None)


def downstream(node: "DagNode") -> List["DagNode"]:
    """
    The nodes that must solve *after* node: its dependants, then its owner.

    Reaches into DagNode._connections rather than going through get_outputs() /
    get_owner_node(): this is the innermost loop of every dirty propagation, and those
    accessors each build a generator wrapped in a weakref iteration guard.

    Args:
        node (DagNode): The node to walk from

    Returns:
        List[DagNode]: The downstream nodes, dependants first
    """
    conns = node._connections
    nodes = list(conns.outputs.values())

    if (owner_ref := conns.owner) is not None and (owner := owner_ref()) is not None:
        nodes.append(owner.get_dag_node())

    return nodes


def downstream_closure(seed: "DagNode") -> Dict["DagNode", None]:
    """
    Every node reachable downstream of seed, seed included.

    A dict rather than a set so the order is stable: the sort below seeds its queue from
    this collection, and a set would make the result depend on object addresses.

    Args:
        seed (DagNode): The node to walk from

    Returns:
        Dict[DagNode, None]: The closure, in discovery order
    """
    found: Dict["DagNode", None] = {seed: None}
    pending = [seed]

    head = 0
    while head < len(pending):
        for nxt in downstream(pending[head]):
            if nxt not in found:
                found[nxt] = None
                pending.append(nxt)
        head += 1

    return found


def dirty_component(seed: "DagNode") -> Dict["DagNode", None]:
    """
    Every dirty node connected to seed along a path of dirty nodes, walking dependants
    and owners downstream *and* dependencies upstream.

    The upstream half is what stops a node running ahead of a dependency that has not
    solved yet: a node downstream of seed may have a second, dirty dependency on a branch
    seed knows nothing about, and that branch has to be pulled into the same sort rather
    than left behind. Clean nodes end the walk, which is what confines a solve to the
    part of the graph that actually changed.

    Args:
        seed (DagNode): The node the solve was requested for

    Returns:
        Dict[DagNode, None]: The dirty component, in discovery order
    """
    found: Dict["DagNode", None] = {seed: None}
    pending = [seed]

    head = 0
    while head < len(pending):
        node = pending[head]
        head += 1

        for nxt in downstream(node):
            if nxt._dirty and nxt not in found:
                found[nxt] = None
                pending.append(nxt)

        for dep in node._connections.inputs.values():
            if dep._dirty and dep not in found:
                found[dep] = None
                pending.append(dep)

    return found


def topo_order(nodes: Dict["DagNode", None]) -> List["DagNode"]:
    """
    Kahn topological sort of the subgraph induced by nodes.

    Only edges with both ends inside nodes are counted, so the result is a valid order
    for solving that subgraph on its own. Cycle detection is free: a cyclic subgraph
    never drains, leaving nodes whose in-degree never reaches zero.

    Args:
        nodes (Dict[DagNode, None]): The induced subgraph, in discovery order

    Raises:
        CircularDependencyError: If the subgraph contains a cycle

    Returns:
        List[DagNode]: The nodes, every node preceded by all of its dependencies
    """
    in_degree: Dict["DagNode", int] = dict.fromkeys(nodes, 0)

    for node in nodes:
        for nxt in downstream(node):
            if (degree := in_degree.get(nxt)) is not None:
                in_degree[nxt] = degree + 1

    order = [node for node, degree in in_degree.items() if not degree]
    head = 0
    while head < len(order):
        for nxt in downstream(order[head]):
            if (degree := in_degree.get(nxt)) is None:
                continue
            in_degree[nxt] = degree = degree - 1
            if not degree:
                order.append(nxt)
        head += 1

    if len(order) != len(in_degree):
        stuck = next(node for node, degree in in_degree.items() if degree)
        raise CircularDependencyError("DAG has circular dependencies at node:", stuck.propagator)

    return order


def propagate_presentation(seed: "DagNode"):
    """
    Mark only the ownership chain above seed dirty, for a change nothing computes from.

    A presentation change (hiding a param, folding a group open, selecting a node) still
    has to reach the UI, and the UI hangs off the owners: a group's data_changed spreads
    DISABLED to nested chunks, the SceneSender's keeps the selection set. So the owners
    solve, and each records the *seed's* reason -- not the alias of the node it inherited
    it through. Stamping GROUP_CHANGED / PARAM_CHANGED on the way up is what used to make a
    collapsed group read as a data change by the time it reached the chunk, and
    ChunkNoticePropagator.execute() re-cooked the module on it.

    Dependants are left clean, so nothing travels along the exec chain: selecting one node
    used to dirty every chunk downstream of it and re-cook the lot.

    Args:
        seed (DagNode): The node that changed
    """
    reasons: Dict[Tuple["chunk_notice.ChunkNoticeAlias", "DagNode"], bool] = dict(seed._changes)
    if (prop := seed.propagator) is not None and (alias := prop.get_notice_alias()):
        # True is the verdict, not a placeholder: reaching here *is* the decision that this
        # change is presentation only, and DagNode.only_presentation_changed reads it back
        # rather than guessing from the alias -- which a presentation_only param, raising
        # PARAM_CHANGED, would fail.
        reasons[(alias, seed)] = True

    node = seed
    seen = {id(seed)}

    while True:
        conns = node._connections
        if (owner_ref := conns.owner) is None or (owner := owner_ref()) is None:
            return

        owner_node = owner.get_dag_node()
        # Ownership is asserted acyclic, but a solve is not the place to find out otherwise.
        if id(owner_node) in seen:
            return
        seen.add(id(owner_node))

        owner_node._dirty = True
        owner_node._index += 1
        owner_node._changes.update(reasons)
        node = owner_node


def propagate_dirty(seed: "DagNode"):
    """
    Mark seed and everything downstream of it dirty, recording why.

    Each dirtied node is told which of its dependencies changed (INPUT_CHANGED, plus the
    node it came from). An owner additionally inherits the change reasons already
    collected by everything it owns, so a Chunk sees the aliases raised by its params and
    their attribute senders, not just "something below me moved".

    The walk runs in topological order, which is what makes the owner inheritance
    correct: a node has collected every reason that applies to it before it hands them
    upward.

    A change nothing computes from goes to propagate_presentation instead.

    Args:
        seed (DagNode): The node that changed

    Raises:
        CircularDependencyError: If the graph below seed contains a cycle
    """
    seed._dirty = True

    # By far the most common shape: a node with nothing hanging off it. Worth taking
    # before any of the bookkeeping below, since dirtying is on the hot path of every
    # param write.
    conns = seed._connections
    if not conns.outputs and conns.owner is None:
        return

    if (prop := seed.propagator) is not None and prop.is_presentation_change():
        propagate_presentation(seed)
        return

    input_changed = chunk_notice.ChunkNoticeAlias.INPUT_CHANGED

    for node in topo_order(downstream_closure(seed)):
        node._dirty = True

        conns = node._connections
        for out in conns.outputs.values():
            out._index += 1
            # False: propagate_dirty is the data-change path. A presentation change was
            # routed to propagate_presentation above and never reaches here.
            out._changes[(input_changed, node)] = False

        if (owner_ref := conns.owner) is None or (owner := owner_ref()) is None:
            continue

        owner_node = owner.get_dag_node()
        owner_node._index += 1
        owner_node._changes.update(node._changes)
        if node.propagator and (alias := node.propagator.get_notice_alias()):
            owner_node._changes[(alias, node)] = False


def solve(seed: "DagNode") -> Generator["DagNode", None, None]:
    """
    Solve seed and everything that depends on it, in dependency order.

    Yields each node as it is marked clean so the caller can run its side effects
    (data_changed / send_self / the chunk module) before the next node runs. Nodes are
    only yielded once per pass, and a node that a re-entrant solve already cleaned is
    skipped rather than run twice.

    Change reasons are held until the whole pass is over, not cleared per node: a
    ProxyParam reads iter_changes() off the param it references while that param is
    already solved, and clearing eagerly would leave it with nothing to forward.

    Args:
        seed (DagNode): The node to solve from

    Raises:
        CircularDependencyError: If the dirty subgraph contains a cycle

    Yields:
        DagNode: Each solved node, seed's frontier first
    """
    if not seed.should_solve():
        return

    solved: List["DagNode"] = []

    try:
        for node in topo_order(dirty_component(seed)):
            # Re-checked per node rather than trusted from the snapshot above: running a
            # node's side effects can re-enter the solve and clean nodes further down
            # this same order, and those must not run a second time.
            if not node.should_solve():
                continue

            node._dirty = False
            solved.append(node)
            yield node
    finally:
        for node in solved:
            node._changes.clear()
            node._index = 0


def get_depth(node: "DagNode") -> int:
    """
    The length of the longest dependency chain ending at node.

    This is the sort key the layer manager orders chunk sublayers by: a chunk that
    depends on another must end up with the stronger layer. Ownership counts without
    adding depth, so a Chunk is at least as deep as the deepest of its params -- which
    is how a chunk wired only through a param connection still sorts after the chunk
    feeding it.

    Computed on demand and cached until the graph changes shape, rather than maintained
    on every connect.

    Args:
        node (DagNode): The node to measure

    Returns:
        int: The depth, 0 for a node with no dependencies
    """
    global _depth_epoch

    if _depth_epoch != _topology_epoch:
        _depth_cache.clear()
        _depth_epoch = _topology_epoch

    if (cached := _depth_cache.get(node)) is not None:
        return cached

    return _depth(node)


def _upstream(node: "DagNode") -> List[Tuple[int, "DagNode"]]:
    """
    What node's depth is measured against, each paired with what it adds.

    A dependency adds one level. Ownership adds none: an owner is only ever pulled down
    to the level of the deepest thing it owns, so that a chunk wired to another chunk
    only through its params still sorts after it.

    Args:
        node (DagNode): The node to walk from

    Returns:
        List[Tuple[int, DagNode]]: (cost, node) pairs
    """
    nodes = [(1, dep) for dep in node._connections.inputs.values()]

    if (owned := node._owned) is not None:
        nodes.extend((0, child) for child in owned)

    return nodes


def _depth(root: "DagNode") -> int:
    """
    Body of get_depth(), as an explicit post-order walk.

    Iterative rather than recursive because the walk is only bounded by how deep the
    user's graph goes, and a param chain in a large scene is free to be deeper than
    Python's stack.

    Args:
        root (DagNode): The node to measure

    Returns:
        int: The depth of root
    """
    # Each entry is a node and whether its dependencies have been expanded yet: a node is
    # pushed once to schedule its dependencies and popped a second time to fold their
    # depths together.
    stack: List[Tuple["DagNode", bool]] = [(root, False)]
    # Guards against a cycle, which contributes no depth rather than looping forever.
    on_path: set = set()

    while stack:
        node, expanded = stack.pop()

        if expanded:
            on_path.discard(id(node))
            _depth_cache[node] = max(
                (cost + _depth_cache.get(dep, 0) for cost, dep in _upstream(node)),
                default=0,
            )
            continue

        if node in _depth_cache or id(node) in on_path:
            continue

        on_path.add(id(node))
        stack.append((node, True))
        for _, dep in _upstream(node):
            if dep not in _depth_cache and id(dep) not in on_path:
                stack.append((dep, False))

    return _depth_cache.get(root, 0)


def commit_order(chunks: Iterable["ChunkBase"]) -> List["ChunkBase"]:
    """
    Order chunks so that every chunk comes after everything it depends on.

    Sorted by get_depth rather than run through topo_order, because a chunk can depend on
    another without there being any edge between the two *chunks*: a param-level
    add_dependency puts no edge on the chunk nodes, so a subgraph induced over those alone
    would miss it entirely. Depth sees it, since ownership costs nothing and therefore
    pulls a chunk at least as deep as its deepest param -- the same reason it is already
    the key the layer manager sorts chunk sublayers by.

    Depth only sees edges someone created with add_dependency, which is NOT how the node
    graph wires a param port: drag-to-connect authors an @Node.param@ expression, whose
    live link is a NoticeConnection rather than a DAG edge, so two chunks joined only by a
    param wire both land at depth 0 here. Pass chunk_usd.commit_order_from_stage as the
    `order` argument to commit_all to order from the stage's own connections instead --
    which is what the CLI does.

    Depth is a valid topological order: an edge from u to v makes v strictly deeper than
    u, so sorting by it can never place a dependant first. Independent chunks tie, and the
    sort being stable leaves those in the order they were handed over.

    Args:
        chunks (Iterable[ChunkBase]): The chunks to order

    Returns:
        List[ChunkBase]: The chunks, every chunk preceded by everything it depends on
    """
    return sorted(chunks, key=lambda c: get_depth(c.get_dag_node()))


def commit_all(chunks: Iterable["ChunkBase"], order: "Callable[[List[ChunkBase]], List[ChunkBase]] | None" = None) -> List["ChunkBase"]:
    """
    Run the reserved `commit(node)` hook of every chunk module that defines one.

    Called once, by whoever drove the solve (the CLI's `run`), after that solve has
    settled -- not from inside the notice propagation loop the way `execute` is
    dispatched. A chunk whose real work is writing a file wants to do that once, against
    a stage that has stopped changing; `execute` is the wrong hook for it because it is
    re-run on every param touch that reaches the chunk.

    Unlike `execute`, no edit context is opened around the call: `commit` reads the
    composed stage and writes files, rather than authoring into the chunk's own layer.

    A module with no `commit` is simply skipped -- the hook is opt-in, and the vast
    majority of chunks have nothing to write.

    Skipping goes through is_blocked() rather than a DISABLED check, which is the same
    gate the execute dispatch uses: ChunkBase.is_blocked() is true for a DISABLED chunk
    as well as for one blocked by sitting outside the active view.

    Args:
        chunks (Iterable[ChunkBase]): The chunks to commit, in any order
        order (Callable | None): How to order them. Defaults to commit_order (DagNode
            depth); pass chunk_usd.commit_order_from_stage bound to a stage to order from
            USD connections instead, which is the only way to see a param-port wire.

    Returns:
        List[ChunkBase]: The chunks whose commit hook actually ran, in the order it ran
    """
    committed: List["ChunkBase"] = []

    for c in (order or commit_order)(list(chunks)):
        if c.is_blocked():
            continue

        hook = getattr(getattr(c, "_module", None), "commit", None)
        if not callable(hook):
            continue

        hook(c)
        committed.append(c)

    return committed
