"""The gizmo *declaration*: what a chunk says it wants a viewport handle for.

Deliberately a leaf. It imports nothing from usd_chunks, because both sides of the
gizmo boundary need this vocabulary and neither should have to drag in the other:
chunk.py validates a `gizmo=` kwarg at param construction, and chunk_gizmo.py resolves
a declaration against a live stage at runtime.

The runtime half -- anchors, placement, stroke handling, the ui:gizmo: attribute
names -- lives in chunk_gizmo.py. This file is only the record and the token sets.
"""
from __future__ import annotations

from typing import Any

import attrs


# Gizmo vocabulary. Kept as frozensets rather than enums so a chunk author writes a plain
# string in config() and a host reads a plain token out of USD -- the whole point of the
# schema is that nothing on either side needs to import usd_chunks to understand it.
# Every gizmo drives exactly one thing, and its type says what: a manipulator sends a
# Gf.Vec3d, a shape sends a single number. That makes the type and the payload the same
# question, which is why there is no separate notion of a "slot" -- an earlier draft had
# one gizmo driving translate *and* rotate *and* scale at once, and every host then needed
# to be told which of them a given drag meant.
GIZMO_MANIPULATOR_TYPES = frozenset({"translate", "rotate", "scale"})
GIZMO_SHAPE_TYPES = frozenset({"cube", "sphere"})
# A whole-chunk handle: "run your native transform manipulator and give me the result".
# Its payload is a Gf.Matrix4d rather than a vector, and it drives nine params in a fixed
# order -- translate, then rotate, then scale. Declared on the Chunk, not on a group,
# because it is the chunk's handle rather than one group's.
GIZMO_TRANSFORM_TYPE = "transform"
GIZMO_TRANSFORM_ARITY = 9
# A surface tool rather than a manipulator: the user drags across geometry and what comes
# back is *where they touched and how hard*, not a value. The only type whose payload is a
# stream of samples, and the only one whose result is a field rather than a param -- see
# apply_stroke. Its `driver` names the params holding the brush's own settings, so a host can
# read the radius to draw the ring; it never writes them.
GIZMO_BRUSH_TYPE = "brush"
GIZMO_TYPES = (
    GIZMO_MANIPULATOR_TYPES | GIZMO_SHAPE_TYPES | {GIZMO_TRANSFORM_TYPE, GIZMO_BRUSH_TYPE}
)
# Signed, because direction is half the meaning. Focus distance runs along the camera's
# *-Z*: an unsigned token would put the handle out the back of the camera, and nothing else
# in the schema could correct it.
GIZMO_AXES = frozenset({"X", "-X", "Y", "-Y", "Z", "-Z", "screen"})
GIZMO_EDIT_MODES = frozenset({"onPrimSelection", "onChunkSelection", "always"})
GIZMO_DEFAULT_EDIT_MODE = "onPrimSelection"


def check_gizmo_sugar(owner: str, gizmo_type: str, scalar: bool) -> None:
    """Validate a `gizmo=` kwarg against the param it was written on.

    The whitelist is by **arity, not param class**: a shape type sends one number so it
    belongs on a scalar param, a manipulator type sends a vector so it belongs on a Vector
    or a param group.

    Args:
        owner: Name of the param, for the error message.
        gizmo_type: The `gizmo=` value.
        scalar: Whether the param holds a single number.

    Raises:
        ValueError: The type does not fit the param. Raised at param construction -- i.e. at
            chunk load -- rather than when a handle is first resolved. `gizmo="translate"` on
            a Slider would otherwise silently bind one axis, which looks like a broken handle
            rather than a broken declaration.
    """
    allowed = GIZMO_SHAPE_TYPES if scalar else GIZMO_MANIPULATOR_TYPES
    if gizmo_type not in allowed:
        kind = "a scalar param" if scalar else "a Vector or param group"
        raise ValueError(
            f"Param {owner!r}: gizmo={gizmo_type!r} cannot go on {kind}. "
            f"Valid here: {sorted(allowed)}"
        )


@attrs.define
class GizmoBase:
    """A viewport manipulator declared by a chunk.

    Deliberately a plain record, not an AttributesBase: a gizmo declaration is static
    config read once at load, never mutated at runtime, so it needs none of the notice
    or validation machinery the param classes carry.

    Attributes:
        offset (tuple[float, float, float] | None): Where the handle sits relative to its
            anchor, in the anchor's local space. None means on the anchor.
        size (float | None): Relative size hint. None leaves it entirely to the host, which
            is usually right -- a viewport wants a screen-constant handle, not a world-sized
            one, and only it knows the camera distance.
        name (str): Namespace segment under ui:gizmo:. The gizmo's *name*, never its type --
            two cubes on one chunk is ordinary (a camera wants a body handle and a distance
            handle), so the type cannot be the key.
        type (str): One of GIZMO_TYPES. Manipulator types are multi-slot; shape types drive
            a single scalar.
        label (str): Display name. Falls back to the chunk's name when empty.
        anchor (str): Name of the param holding the target prim path. Empty means infer --
            see chunk_gizmo.resolve_anchor_param.
        driver (tuple[str, ...]): The params this handle writes, as paths on its own chunk --
            ("translate_grp.tx", "translate_grp.ty", "translate_grp.tz"), or a single entry
            for a scalar or a vector-typed param. A bare string normalises to a one-tuple.
        axis (str | None): Optional drag direction in the anchor's local space. None means
            free drag -- any mouse direction moves the driven param, which is the common case.
            Set it where the direction carries the meaning (focus distance along -Z), which is
            also what lets a native host build a real linear handle.
        space (str): "local" or "world".
        editmode (tuple[str, ...]): When the host should show the handle.
    """

    name: str
    type: str
    label: str = ""
    anchor: str = ""
    driver: Any = attrs.field(factory=dict)
    axis: str | None = None
    space: str = "local"
    editmode: Any = (GIZMO_DEFAULT_EDIT_MODE,)
    offset: Any = None
    size: float | None = None

    def __attrs_post_init__(self) -> None:
        """Validate the declaration at construction, so a bad one fails at chunk load.

        Raises:
            ValueError: The type is unknown, the driver is missing or not a list of param paths,
                or a transform handle does not drive exactly nine params.
        """
        if self.type not in GIZMO_TYPES:
            raise ValueError(
                f"Gizmo {self.name!r}: unknown type {self.type!r}. Known types: {sorted(GIZMO_TYPES)}"
            )

        # One param, three params, or a single vector-typed one -- but always a flat list,
        # never keyed by anything. What the handle sends is decided by its type.
        driver = (self.driver,) if isinstance(self.driver, str) else tuple(self.driver or ())
        if not driver:
            raise ValueError(f"Gizmo {self.name!r}: driver is required -- a handle that writes nothing is not a gizmo")
        if not all(isinstance(entry, str) for entry in driver):
            raise ValueError(f"Gizmo {self.name!r}: driver entries must be param paths, got {driver!r}")
        if self.type == GIZMO_TRANSFORM_TYPE and len(driver) != GIZMO_TRANSFORM_ARITY:
            # Fixed order, checked here rather than discovered during a drag: a transform
            # handle writing the wrong nine params would look like a broken manipulator
            # rather than a broken declaration.
            raise ValueError(
                f"Gizmo {self.name!r}: a transform handle drives {GIZMO_TRANSFORM_ARITY} params "
                f"(translate xyz, rotate xyz, scale xyz, in that order), got {len(driver)}"
            )
        self.driver = driver

        if self.axis is not None and self.axis not in GIZMO_AXES:
            raise ValueError(
                f"Gizmo {self.name!r}: unknown axis {self.axis!r}. Known axes: {sorted(GIZMO_AXES)}"
            )

        if self.offset is not None:
            offset = tuple(float(v) for v in self.offset)
            if len(offset) != 3:
                raise ValueError(f"Gizmo {self.name!r}: offset must be three numbers, got {len(offset)}")
            self.offset = offset

        if self.size is not None:
            if float(self.size) <= 0.0:
                raise ValueError(f"Gizmo {self.name!r}: size must be positive, got {self.size}")
            self.size = float(self.size)

        editmode = (self.editmode,) if isinstance(self.editmode, str) else tuple(self.editmode)
        if not editmode:
            raise ValueError(f"Gizmo {self.name!r}: editmode cannot be empty")
        for mode in editmode:
            if mode not in GIZMO_EDIT_MODES:
                raise ValueError(
                    f"Gizmo {self.name!r}: unknown editmode {mode!r}. Known modes: {sorted(GIZMO_EDIT_MODES)}"
                )
        self.editmode = editmode
