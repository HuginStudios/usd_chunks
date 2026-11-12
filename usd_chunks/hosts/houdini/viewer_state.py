"""The chunk node's viewer state: its gizmos as Houdini handles in the Scene Viewer.

Select a chunk node and press Enter in the viewport (or pick the Handles tool): each `box`
gizmo is a ``boundingbox`` handle -- grab a face and pull it, only that face moves -- and
each `translate` gizmo a ``translate`` handle that moves it. They are Houdini's own handles, so
they draw, snap and undo as Houdini's do.

Every chunk node is one node type, so the state is one too, registered under the type's
own name (the HDA's DefaultState names it). A state's handles are bound when it is
registered, before it knows which chunk it will meet, so it binds a pool of each kind --
BOXES ``boundingbox`` handles, MOVES ``translate`` handles -- and on entering a node shows the
ones that node's gizmos need. The bindings are dynamic: the gizmo maths (gizmos) runs
between a handle's parms and the chunk's params both ways, and a drag writes through
lop.drive_gizmo, the same apply_bindings path usdview and Blender take.

Houdini loads the state from viewer_states/usdchunks_chunk.py on HOUDINI_PATH.
"""
from __future__ import annotations

import logging

import hou

from usd_chunks.hosts.houdini import gizmos, lop, space

logger = logging.getLogger(__name__)

#: The state's name: the node type's, which the HDA's DefaultState section names.
STATE_NAME = lop.HDA_TYPE
#: How many handles of each kind the state binds: a node's box gizmos, its translate ones.
BOXES = 4
MOVES = 32
_POOLS = {"box": ("boundingbox", BOXES, "translate(0) rotate(0)"),
          "translate": ("translate", MOVES, None)}


def _pool(kind: str) -> list[str]:
    return [f"{kind}{i}" for i in range(_POOLS[kind][1])]


class ChunkHandles:
    """The state: a pool of handles, each shown for one of the node's gizmos."""

    def __init__(self, state_name, scene_viewer):
        self.state_name = state_name
        self.viewer = scene_viewer
        self.node = None
        #: handle name -> the gizmos.Target it is showing.
        self.slots: dict[str, gizmos.Target] = {}
        #: The handle in the hand: its name and what the drag started from.
        self.drag: dict | None = None
        #: Handles shown but not yet placed on their gizmo (see onDraw).
        self.unplaced: set[str] = set()

    # ---------------------------------------------------------------- lifecycle

    def onEnter(self, kwargs):
        self.node = kwargs["node"]
        self._assign()

    def onExit(self, kwargs):
        self._end_drag()
        self.node = None
        self.slots = {}

    def onNodeChangeEvent(self, kwargs):
        """A parm changed: rows added or taken away add or take away their handles."""
        if self.drag is None and kwargs.get("event_type") == hou.nodeEventType.ParmTupleChanged:
            self._assign()

    def _assign(self) -> None:
        """Hand each of the node's gizmos a handle from its pool; hide the rest."""
        targets = []
        try:
            targets = lop.gizmo_targets(self.node)
        except Exception:
            logger.debug("no gizmos for %s", self.node.path(), exc_info=True)
        free = {kind: _pool(kind) for kind in _POOLS}
        slots = {}
        for target in targets:
            pool = free[target.type]
            if not pool:
                logger.warning("%s: more %s gizmos than the %d handles the state has; the rest are not shown",
                               self.node.path(), target.type, _POOLS[target.type][1])
                continue
            slots[pool.pop(0)] = target
        if {k: (t.name, t.anchor) for k, t in slots.items()} == {
                k: (t.name, t.anchor) for k, t in self.slots.items()}:
            return
        self.slots = slots
        logger.debug("%s: handles %s", self.node.path(), {k: t.name for k, t in slots.items()})
        for kind in _POOLS:
            for name in _pool(kind):
                hou.Handle(self.viewer, name).show(name in slots)
        # Placed on the next draw: an update from onEnter comes too early for Houdini to ask
        # onStateToHandle (SideFX's own states update their handles from onDraw too).
        self.unplaced = set(slots)

    def onDraw(self, kwargs):
        unplaced, self.unplaced = self.unplaced, set()
        for name in unplaced:
            hou.Handle(self.viewer, name).update(immediate=True)

    # ---------------------------------------------------------------- the maths' inputs

    def _placement(self, target):
        """(the node's stage, its to-Houdini matrix, the anchor's matrices or None).

        to_host first: it reads the stage of the chunkgroup's input (a Sublayer, say), and
        asking Houdini for another node's stage swaps out one fetched before it, so the
        anchor would not be found on it.
        """
        to_host = space.to_host(self.node)
        stage = self.node.stage()
        return stage, to_host, gizmos.anchor_matrices(stage, target.anchor, to_host)

    # ---------------------------------------------------------------- node -> handle

    def onStateToHandle(self, kwargs):
        name = kwargs["handle"]
        target = self.slots.get(name)
        if target is None or (self.drag is not None and self.drag["handle"] == name):
            return  # not in use, or in the hand: Houdini knows where it is better than we do
        try:
            stage, to_host, matrices = self._placement(target)
            values = lop.gizmo_values(self.node, target)
            if matrices is None or values is None:
                return
            world, _parent = matrices
            out = kwargs["parms"]
            if target.type == "box":
                out.update(gizmos.box_to_handle(values, world))
            else:
                point = gizmos.handle_point(stage, target, world, to_host)
                out.update(dict(zip(gizmos.MOVE_PARMS, point)))
        except Exception:
            logger.debug("could not place handle %s", name, exc_info=True)

    # ---------------------------------------------------------------- handle -> node

    def onBeginHandleToState(self, kwargs):
        name = kwargs["handle"]
        target = self.slots.get(name)
        if target is None or self.drag is not None:
            return
        stage, to_host, matrices = self._placement(target)
        values = lop.gizmo_values(self.node, target)
        if matrices is None or values is None:
            return
        world, parent = matrices
        # Where onStateToHandle put the handle: the drag's start, which the move is measured from.
        point = (gizmos.handle_point(stage, target, world, to_host)
                 if target.type == "translate" else None)
        self.viewer.beginStateUndo(f"Drag {target.label}")
        self.drag = {"handle": name, "target": target, "world": world, "parent": parent,
                     "values": values, "point": point}

    def onHandleToState(self, kwargs):
        drag = self.drag
        if drag is None or drag["handle"] != kwargs["handle"]:
            return
        target, parms = drag["target"], kwargs["parms"]
        try:
            if target.type == "box":
                value = gizmos.handle_to_box(parms, drag["world"])
            else:
                point = tuple(float(parms[n]) for n in gizmos.MOVE_PARMS)
                value = gizmos.moved(drag["values"], drag["point"], point, drag["parent"])
            lop.drive_gizmo(self.node, target.name, value)
        except Exception:
            logger.exception("dragging %s on %s failed", target.name, self.node.path())

    def onEndHandleToState(self, kwargs):
        if self.drag is not None and self.drag["handle"] == kwargs["handle"]:
            self._end_drag()

    def _end_drag(self) -> None:
        drag, self.drag = self.drag, None
        if drag is None:
            return
        try:
            self.viewer.endStateUndo()
        except hou.Error:
            pass
        if self.node is not None:
            # Back on what the chunk made of it: a face on its Snap step, a module on the roof.
            self._assign()
            self.unplaced.add(drag["handle"])


def create_template() -> hou.ViewerStateTemplate:
    """The state's template, with every handle in the pools bound."""
    template = hou.ViewerStateTemplate(STATE_NAME, "USD Chunk", hou.lopNodeTypeCategory())
    template.bindFactory(ChunkHandles)
    template.bindIcon(lop.node_type().icon() if lop.node_type() else "NETWORKS_lop")
    for kind, (handle_type, _count, settings) in _POOLS.items():
        for name in _pool(kind):
            template.bindHandle(handle_type, name, **({"settings": settings} if settings else {}))
    template.bindNodeChangeEvent([hou.nodeEventType.ParmTupleChanged])
    return template
