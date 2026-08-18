"""Viewport gizmos: declaration -> USD -> write-back.

**usd_chunks records. The DCC interprets.** This module authors the `ui:gizmo:*` schema
onto chunk prims, reads it back as plain records, and owns the write-back. It does not
draw, pick or drag -- that lives in the viewport layer (`usd-guides` for usdview and
Kraken) or in a host's native handle system.

The whole "hook into any DCC" surface is four calls:

    chunk_gizmo.resolve_gizmos(prim) -> list[GizmoDesc]   # what handles exist
    chunk_gizmo.apply_bindings(chunk, name, values)       # write-back
    chunk.start_gizmo() / chunk.stop_gizmo()              # drag bracket

Write-back is the crux and must stay here. A host computes a handle transform, but only
usd_chunks knows how to turn that into ``node.get_param("tx").value = ...`` so it flows
through the exec graph. A chunk like transform.py clears its own layer and re-authors
``xformOp:transform`` on every cook, so a host writing the prim directly would have its
edit wiped by the next execute.
"""

from __future__ import annotations

import itertools
import logging
import math
from typing import Any, Iterable, Optional

import attrs
from pxr import Gf, Sdf, Usd

from usd_chunks import chunk_core as core
from usd_chunks.chunk_registry import ChunkLayerManager, ChunkRegistry

logger = logging.getLogger(__name__)

GIZMO_PREFIX = "ui:gizmo:"
_INPUTS_PREFIX = "inputs:"


@attrs.frozen
class GizmoDesc:
    """What a host needs to build a handle, and nothing else.

    Deliberately a plain data record: type token, resolved anchor paths, driver map, axis,
    space, editmode. No Qt types, no callbacks into host-specific objects, nothing beyond
    what `pxr` gives every host. If a third-party DCC needs something that is not on this
    record, the schema is under-specified and that is a finding, not a reason to widen the
    record with host-shaped data.
    """

    name: str
    type: str
    label: str = ""
    anchor: tuple = ()          # resolved scene prims the handle hangs off
    anchor_source: str = ""     # the param that produced them, for our own write-back
    driver: dict = attrs.field(factory=dict)   # slot -> tuple[Sdf.Path] of driven properties
    axis: Optional[str] = None
    space: str = "local"
    editmode: tuple = (core.GIZMO_DEFAULT_EDIT_MODE,)


# ---------------------------------------------------------------------------
# attribute names
#
# Read and write go through the same helpers so the two can never drift -- the failure
# mode otherwise is authoring one spelling and resolving another, which looks like "no
# gizmos" rather than like a bug.
# ---------------------------------------------------------------------------

def _attr_name(gizmo_name: str, *parts: str) -> str:
    return GIZMO_PREFIX + ":".join((gizmo_name, *parts))


def _driver_attr_name(gizmo_name: str, slot: str) -> str:
    # The singular driver is `...:driver`, not `...:driver:value` -- a chunk with one driven
    # attribute should read as one, in the usda and in a host's parser.
    if slot == core.GIZMO_VALUE_SLOT:
        return _attr_name(gizmo_name, "driver")
    return _attr_name(gizmo_name, "driver", slot)


def _slot_from_driver_attr(attr_name: str, gizmo_name: str) -> Optional[str]:
    base = _attr_name(gizmo_name, "driver")
    if attr_name == base:
        return core.GIZMO_VALUE_SLOT
    if attr_name.startswith(base + ":"):
        return attr_name[len(base) + 1:]
    return None


# ---------------------------------------------------------------------------
# declaration -> chunk
# ---------------------------------------------------------------------------

def get_gizmos(chunk: "core.ChunkBase") -> list:
    """Every gizmo a chunk declares -- in config(), plus any `gizmo=` sugar on its params.

    Desugaring happens here rather than at load because it needs both halves at once: the
    type token lives on the param (built in main()) and the resulting Gizmo belongs beside
    the config's list (built in config()). The two are assembled at different times, and
    every caller that wants gizmos already comes through this function.

    The sugar's *validation* is not deferred -- that runs in the param's constructor, so a
    bad `gizmo=` still fails at load rather than at first drag.
    """
    config = chunk.get_config() if hasattr(chunk, "get_config") else None
    gizmos = list(getattr(config, "gizmos", None) or ())

    declared = {g.name for g in gizmos}
    for sugared in _sugared_gizmos(chunk):
        # An explicit Gizmo of the same name wins: the escalation path from sugar to config
        # form is "write the full one", and that has to actually replace the short one.
        if sugared.name not in declared:
            gizmos.append(sugared)
    return gizmos


def _sugared_gizmos(chunk: "core.ChunkBase") -> list:
    """`gizmo=` kwargs on this chunk's params, as Gizmo records.

    Scalar params drive the singular `value` slot; a Vector drives the slot its type names
    (`gizmo="translate"` -> the translate slot). Sugar is type-only, so anything wanting an
    axis, an editmode, several slots or a cross-chunk driver escalates to the config form --
    exactly one escalation path, with no tuple-or-dict middle ground between them.
    """
    sugared = []
    for path, param in _iter_params(chunk):
        if gizmo_type := getattr(param, "gizmo", None):
            sugared.append(core.GizmoBase(
                name=param.name,
                type=gizmo_type,
                label=param.label or param.name,
                driver={core.GIZMO_VALUE_SLOT: (path,)},
            ))

    for path, vector in _iter_vectors(chunk):
        if gizmo_type := getattr(vector, "gizmo", None):
            # One entry, not three. A Vector is stored as a single vector-typed attribute
            # (inputs:aim), not as inputs:aim:x/y/z -- so the driver connects to the vector
            # itself and write-back assigns the whole value.
            sugared.append(core.GizmoBase(
                name=vector.name,
                type=gizmo_type,
                label=vector.label or vector.name,
                driver={gizmo_type: (path,)},
            ))
    return sugared


def find_gizmo(chunk: "core.ChunkBase", name: str):
    for gizmo in get_gizmos(chunk):
        if gizmo.name == name:
            return gizmo
    return None


def resolve_anchor_param(chunk: "core.ChunkBase", gizmo) -> "core.ChunkParamBase":
    """The param holding the target prim path for this gizmo.

    Name-first, then the flag. Surveying scripts/, the name is the more reliable signal:
    only about half the chunks with a target path set PRIM_SELECTION (camera.py, light.py,
    reference.py and others name the param but never flag it), while the name
    `primpath_location` is near-universal.

    Raises loudly rather than returning None -- a gizmo whose anchor cannot be resolved has
    nowhere to draw, and failing at load with the chunk's name in the message is far
    cheaper to diagnose than a handle that silently never appears.
    """
    if gizmo.anchor:
        param = chunk.get_param(gizmo.anchor)
        if param is None:
            raise ValueError(
                f"Gizmo {gizmo.name!r} on chunk {chunk.name!r}: anchor param {gizmo.anchor!r} does not exist"
            )
        return param

    if (param := chunk.get_param("primpath_location")) is not None:
        return param

    flagged = [
        p for p in chunk.get_params()
        if isinstance(p, core.ChunkParamBase)
        and p.has_flag(core.ChunkFlags.ParamFlags.PRIM_SELECTION)
    ]
    if len(flagged) == 1:
        return flagged[0]

    raise ValueError(
        f"Gizmo {gizmo.name!r} on chunk {chunk.name!r}: cannot infer anchor -- no param named "
        f"'primpath_location' and {len(flagged)} params carry PRIM_SELECTION. Set anchor= explicitly."
    )


def _anchor_targets(chunk: "core.ChunkBase", gizmo, stage: Usd.Stage) -> list:
    """Resolved scene prim paths for a gizmo's anchor.

    A rel to the *resolved prim*, not to the anchor param, is the whole point: a host must
    never have to evaluate a chunk param to find out where to draw. We already re-read this
    param on every cook, so keeping the rel fresh costs nothing.
    """
    param = resolve_anchor_param(chunk, gizmo)

    resolve = getattr(param, "resolve_paths", None)
    raw_paths: Iterable = list(resolve()) if callable(resolve) else []
    if not raw_paths:
        # resolve_paths needs a live stage and only returns prims that already exist. Neither
        # holds when a chunk prim is first authored: the target is routinely created by an
        # upstream chunk that has not cooked yet. Fall back to the literal value so the rel
        # is authored anyway -- a rel to a not-yet-existing prim heals when it appears, while
        # an anchor we declined to author never comes back on its own.
        raw_paths = [p for p in str(param.value or "").replace(",", " ").split() if p]

    targets = []
    for raw in raw_paths:
        if not raw:
            continue
        # resolve_paths can hand back variant-decorated paths (/prim{LOD=high}/geom); the
        # handle hangs off the prim, not the variant selection that authored it.
        try:
            from usd_chunks import chunk_utils
            _, clean, _ = chunk_utils.split_variant_path(str(raw))
        except Exception:
            clean = str(raw)
        if not clean or not Sdf.Path.IsValidPathString(clean):
            continue
        path = Sdf.Path(clean)
        # Globs and property paths are not somewhere a handle can hang.
        if not path.IsAbsolutePath() or not path.IsPrimPath():
            continue
        if path not in targets:
            targets.append(path)
    return targets


def _iter_params(obj, prefix: str = ""):
    """(path, param) for every scalar param under obj.

    A near-copy of chunk_usd._iter_chunk_params, duplicated deliberately: that module lives
    under plugins/ and this one must stay importable by a non-Qt, non-plugin host. Eight
    lines of walk is a cheaper price than inverting the dependency direction.
    """
    for param in obj.get_my_params():
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkParamBase):
            yield path, param
        elif isinstance(param, (core.ChunkListGroupBase, core.ChunkVectorBase)):
            continue
        else:
            yield from _iter_params(param, path)


def _iter_vectors(obj, prefix: str = ""):
    """(path, vector) for every ChunkVectorBase under obj. Vectors are skipped by
    _iter_params, which walks scalars only."""
    for param in obj.get_my_params():
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkVectorBase):
            yield path, param
        elif not isinstance(param, (core.ChunkParamBase, core.ChunkListGroupBase)):
            yield from _iter_vectors(param, path)


def _param_path(chunk: "core.ChunkBase", entry: str) -> str:
    """The full dotted path of a driven param, from either its path or its bare name.

    Needed because a chunk's params are usually nested -- transform.py's translate sliders
    live in a `translate_grp` Group, so `tx` is stored as `inputs:translate_grp:tx`. Naively
    turning the declared entry into an attribute name would author a connection to
    `inputs:tx`, which resolves to nothing and fails silently.
    """
    for path, param in itertools.chain(_iter_params(chunk), _iter_vectors(chunk)):
        if path == entry or param.name == entry:
            return path
    raise ValueError(f"Chunk {chunk.name!r} has no param {entry!r} to drive")


def _driver_targets(prim: Usd.Prim, chunk: "core.ChunkBase", entries: Iterable[str]) -> list:
    """Turn declared driver entries into connection targets.

    A bare param path (`tx`, `translate_grp.tx`) resolves on the chunk's own prim; a full
    property path (`/chunks/Tab/Transform.inputs:tx`) binds to another chunk's param. The
    same string type covers both, so cross-chunk binding costs nothing at the schema level --
    which matters immediately, since camera.py has no transform params of its own.
    """
    targets = []
    for entry in entries:
        text = str(entry).strip().strip("<>")
        if text.startswith("/"):
            targets.append(Sdf.Path(text))
        else:
            path = _param_path(chunk, text).replace(".", ":")
            targets.append(Sdf.Path(f"{prim.GetPath()}.inputs:{path}"))
    return targets


# ---------------------------------------------------------------------------
# authoring
# ---------------------------------------------------------------------------

def _uniform_token(prim: Usd.Prim, name: str, array: bool = False) -> Usd.Attribute:
    type_name = Sdf.ValueTypeNames.TokenArray if array else Sdf.ValueTypeNames.Token
    attr = prim.CreateAttribute(name, type_name, custom=False)
    attr.SetVariability(Sdf.VariabilityUniform)
    return attr


def author_gizmos(stage: Usd.Stage, prim: Usd.Prim, chunk: "core.ChunkBase") -> None:
    """Author every gizmo a chunk declares onto its chunk prim.

    Called from both chunk-prim authoring sites -- create-time and re-author/export. Both
    need it; updating one and missing the other is the obvious failure mode, and the symptom
    (gizmos that work until you save and reload) is not obviously a gizmo bug.

    Assumes the caller has already entered the right edit context, exactly like the
    config.outputs loop it sits beside.
    """
    gizmos = get_gizmos(chunk)
    if not gizmos or not (prim and prim.IsValid()):
        return

    for gizmo in gizmos:
        try:
            _author_one(stage, prim, chunk, gizmo)
        except Exception:
            # One malformed gizmo must not take the chunk prim down with it -- the rest of
            # the prim's bookkeeping (exec ports, params) is what makes the chunk work at all.
            logger.exception("Failed to author gizmo %r on chunk %r", gizmo.name, chunk.name)


def _author_one(stage: Usd.Stage, prim: Usd.Prim, chunk: "core.ChunkBase", gizmo) -> None:
    _uniform_token(prim, _attr_name(gizmo.name, "type")).Set(gizmo.type)
    _uniform_token(prim, _attr_name(gizmo.name, "label")).Set(gizmo.label or chunk.name)
    _uniform_token(prim, _attr_name(gizmo.name, "editmode"), array=True).Set(list(gizmo.editmode))

    if gizmo.axis is not None:
        _uniform_token(prim, _attr_name(gizmo.name, "axis")).Set(gizmo.axis)
    if gizmo.space and gizmo.space != "local":
        _uniform_token(prim, _attr_name(gizmo.name, "space")).Set(gizmo.space)

    # anchorSource is for us, not for hosts: write-back needs to know which param produced
    # the target, and a host that had to read it would be back to the two-hop lookup this
    # schema exists to avoid.
    anchor_param = resolve_anchor_param(chunk, gizmo)
    _uniform_token(prim, _attr_name(gizmo.name, "anchorSource")).Set(anchor_param.name)

    rel = prim.CreateRelationship(_attr_name(gizmo.name, "anchor"), custom=False)
    rel.SetTargets(_anchor_targets(chunk, gizmo, stage))

    for slot, entries in gizmo.driver.items():
        attr = _uniform_token(prim, _driver_attr_name(gizmo.name, slot))
        attr.SetConnections(_driver_targets(prim, chunk, entries))


def refresh_gizmo_anchors(stage: Usd.Stage, prim: Usd.Prim, chunk: "core.ChunkBase") -> None:
    """Re-resolve every gizmo's anchor rel against the chunk's current param values.

    Cheap no-op for the overwhelming majority of chunks, which declare no gizmos at all --
    that early return is why this can hang off the generic param-changed path.
    """
    gizmos = get_gizmos(chunk)
    if not gizmos or not (prim and prim.IsValid()):
        return

    for gizmo in gizmos:
        try:
            targets = _anchor_targets(chunk, gizmo, stage)
        except Exception:
            # Runs off the generic param-changed path, so an escaping exception here would
            # break ordinary param editing for the whole chunk -- a far worse failure than a
            # stale anchor rel, which the next edit gets another chance to fix.
            logger.debug("Could not refresh anchor for gizmo %r on %r", gizmo.name, chunk.name, exc_info=True)
            continue
        rel = prim.GetRelationship(_attr_name(gizmo.name, "anchor"))
        if not rel:
            rel = prim.CreateRelationship(_attr_name(gizmo.name, "anchor"), custom=False)
        if list(rel.GetTargets()) != targets:
            rel.SetTargets(targets)


# ---------------------------------------------------------------------------
# read-back
# ---------------------------------------------------------------------------

def _gizmo_names(prim: Usd.Prim) -> list:
    names = []
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith(GIZMO_PREFIX):
            continue
        gizmo_name = name[len(GIZMO_PREFIX):].split(":")[0]
        if gizmo_name and gizmo_name not in names:
            names.append(gizmo_name)
    return names


def _read_gizmos(prim: Usd.Prim) -> list:
    descs = []
    for name in _gizmo_names(prim):
        type_attr = prim.GetAttribute(_attr_name(name, "type"))
        gizmo_type = type_attr.Get() if type_attr else None
        if not gizmo_type:
            # Namespaced attrs with no type are not a gizmo -- skip rather than fabricate one.
            continue

        driver = {}
        for attr in prim.GetAttributes():
            slot = _slot_from_driver_attr(attr.GetName(), name)
            if slot is None:
                continue
            connections = tuple(attr.GetConnections())
            if connections:
                driver[slot] = connections

        anchor_rel = prim.GetRelationship(_attr_name(name, "anchor"))
        label_attr = prim.GetAttribute(_attr_name(name, "label"))
        axis_attr = prim.GetAttribute(_attr_name(name, "axis"))
        space_attr = prim.GetAttribute(_attr_name(name, "space"))
        source_attr = prim.GetAttribute(_attr_name(name, "anchorSource"))
        mode_attr = prim.GetAttribute(_attr_name(name, "editmode"))
        modes = tuple(mode_attr.Get() or ()) if mode_attr else ()

        descs.append(GizmoDesc(
            name=name,
            type=gizmo_type,
            label=(label_attr.Get() if label_attr else "") or "",
            anchor=tuple(anchor_rel.GetTargets()) if anchor_rel else (),
            anchor_source=(source_attr.Get() if source_attr else "") or "",
            driver=driver,
            axis=(axis_attr.Get() if axis_attr else None) or None,
            space=(space_attr.Get() if space_attr else None) or "local",
            editmode=modes or (core.GIZMO_DEFAULT_EDIT_MODE,),
        ))
    return descs


def chunks_for_prim(prim: Usd.Prim) -> list:
    """Every chunk that authored a spec on this prim, via the layer stack.

    Each chunk already owns its own Sdf.Layer, so a prim's spec stack *already records*
    which chunks touched it -- no scan of /chunks, no separate index to keep in sync, and
    no anchor matching. It also stays correct for free when a chunk is disabled or its layer
    muted: no spec in the stack, no handle.
    """
    manager = ChunkLayerManager()
    if not getattr(manager, "initialized", False) or not manager.chunk_layers:
        return []

    # chunk_layers is keyed by ChunkHash, not by the chunk object, so it cannot be inverted
    # on its own -- go via the scene's live chunks to get back to something with params to
    # write. Several chunks can legitimately share one layer (a CONTEXT group hands its
    # layer to its children), hence a list per identifier rather than a single entry.
    scene = getattr(ChunkRegistry(), "scene", None)
    if scene is None:
        return []

    by_identifier: dict = {}
    for candidate in scene.get_chunks():
        layer = manager.chunk_layers.get(candidate.get_hash())
        if layer is not None:
            by_identifier.setdefault(layer.identifier, []).append(candidate)
    if not by_identifier:
        return []

    found = []
    for spec in prim.GetPrimStack():
        layer = getattr(spec, "layer", None)
        if layer is None:
            continue
        for chunk in by_identifier.get(layer.identifier, ()):
            if not any(chunk is seen for seen in found):
                found.append(chunk)
    return found


def _chunk_prim(stage: Usd.Stage, chunk: "core.ChunkBase") -> Optional[Usd.Prim]:
    path = getattr(chunk, "_exec_prim_path", None) or getattr(chunk, "prim_path", None)
    if not path:
        return None
    prim = stage.GetPrimAtPath(Sdf.Path(str(path)))
    return prim if prim and prim.IsValid() else None


def resolve_gizmos(prim: Usd.Prim) -> list:
    """The gizmos that apply to `prim`.

    Takes **the prim the host has selected** -- the Camera, not the chunk prim. `ui:gizmo:*`
    is authored under /chunks, which is not in the viewport and is not what anyone clicks,
    so something has to get from the scene prim back to the chunk; doing it here rather than
    in every host keeps both the /chunks layout and the layer-to-chunk mapping private.

    A chunk prim is accepted too, for the node-graph case.

    Returns gizmos from *every* chunk that touched the prim, upstream ones included. The
    host filters to the selected chunk -- a prim touched by two transform chunks in one
    chain would otherwise get two identical handles in the same place. Stated from the other
    end: a handle only ever edits the chunk you are looking at.
    """
    if not (prim and prim.IsValid()):
        return []

    if own := _read_gizmos(prim):
        return own

    stage = prim.GetStage()
    descs = []
    for chunk in chunks_for_prim(prim):
        chunk_prim = _chunk_prim(stage, chunk)
        if chunk_prim is not None:
            descs.extend(_read_gizmos(chunk_prim))
    return descs


# ---------------------------------------------------------------------------
# matrix -> slot dict
# ---------------------------------------------------------------------------

# Below this, cos(ry) is small enough that the Z angle stops being separable from X and the
# atan2 pair below turns numerical noise into large, jittery angles. At exactly +-90 degrees
# they are genuinely not separable at all (gimbal lock) -- Z is folded into X by convention.
_GIMBAL_EPSILON = 1e-9


def decompose(matrix: "Gf.Matrix4d") -> dict:
    """A TRS matrix as a slot dict, in the convention transform.py composes in.

    **Offered to hosts, not required of them.** The contract boundary is the slot dict; a
    native handle that already produces Euler angles ignores this entirely. Keeping it here
    means the Euler convention lives in exactly one place instead of being re-derived, subtly
    differently, in every host.

    Must invert `transform.py`'s composition exactly::

        Gf.Rotation(X, rx) * Gf.Rotation(Y, ry) * Gf.Rotation(Z, rz)

    which in Gf's row-vector convention is the matrix product Mx * My * Mz, giving::

        M[0][2] = -sin(ry)
        M[0][0], M[0][1] = cos(ry)cos(rz), cos(ry)sin(rz)
        M[1][2], M[2][2] = sin(rx)cos(ry), cos(rx)cos(ry)

    Note `Gf.Rotation.Decompose` is *not* this convention and returns different angles for
    the same rotation -- it looks like the obvious call to make and quietly is not.

    A mismatch here does not fail loudly. It shows up as the handle drifting a little further
    from the object on each successive drag, which is miserable to diagnose from a bug report.

    Rotations are Euler XYZ in degrees; translate and scale are `Gf.Vec3d`.
    """
    transform = Gf.Transform(matrix)
    rotation = Gf.Matrix3d(transform.GetRotation().GetQuat())

    sin_y = max(-1.0, min(1.0, -rotation[0][2]))
    angle_y = math.asin(sin_y)

    if abs(rotation[0][2]) < 1.0 - _GIMBAL_EPSILON:
        angle_z = math.atan2(rotation[0][1], rotation[0][0])
        angle_x = math.atan2(rotation[1][2], rotation[2][2])
    else:
        # Gimbal lock: X and Z rotate about the same world axis, so the split between them is
        # arbitrary. Pin Z to zero and put the whole thing in X -- an arbitrary choice, but a
        # *stable* one, which is what keeps repeated drags from wandering.
        angle_z = 0.0
        angle_x = math.atan2(rotation[1][0] * (1.0 if sin_y > 0 else -1.0), rotation[1][1])

    return {
        "translate": Gf.Vec3d(transform.GetTranslation()),
        "rotate": Gf.Vec3d(math.degrees(angle_x), math.degrees(angle_y), math.degrees(angle_z)),
        "scale": Gf.Vec3d(transform.GetScale()),
    }


# ---------------------------------------------------------------------------
# write-back
# ---------------------------------------------------------------------------

def chunk_at_prim_path(path) -> Optional["core.ChunkBase"]:
    """The chunk whose chunk prim sits at `path`, if it is still around."""
    scene = getattr(ChunkRegistry(), "scene", None)
    if scene is None:
        return None
    wanted = str(path)
    for candidate in scene.get_chunks():
        own = getattr(candidate, "_exec_prim_path", None) or getattr(candidate, "prim_path", None)
        if own and str(own) == wanted:
            return candidate
    return None


def _param_for_entry(chunk: "core.ChunkBase", entry: str):
    """The param a driver entry names, or None if a cross-chunk target has gone away.

    Two different kinds of "not found", handled differently on purpose:

    * A bare name that does not resolve on the own chunk is a **declaration bug** -- the
      chunk's own config names a param it does not have -- so it raises.
    * A full property path that does not resolve is a **runtime condition**. The chunk it
      points at can be deleted or disabled independently at any time, and the gizmo it
      belongs to is still perfectly good. Returns None so the caller can drop that one slot
      and keep the rest working.
    """
    text = str(entry).strip().strip("<>")

    if not text.startswith("/"):
        param = chunk.get_param(text)
        if param is None:
            raise ValueError(f"Chunk {chunk.name!r} has no param {text!r} to drive")
        return param

    path = Sdf.Path(text)
    owner = chunk_at_prim_path(path.GetPrimPath())
    if owner is None:
        logger.warning("Gizmo driver %s: no chunk at %s", text, path.GetPrimPath())
        return None

    # /chunks/Tab/Transform.inputs:translate_grp:tx -> translate_grp.tx
    prop = path.name
    if prop.startswith(_INPUTS_PREFIX):
        prop = prop[len(_INPUTS_PREFIX):]
    param = owner.get_param(prop.replace(":", "."))
    if param is None:
        logger.warning("Gizmo driver %s: chunk %r has no param %r", text, owner.name, prop)
    return param


def _values_for(entries: tuple, value: Any) -> list:
    if len(entries) == 1:
        return [value]
    try:
        values = list(value)
    except TypeError:
        raise ValueError(
            f"Slot drives {len(entries)} params but got a single {type(value).__name__}"
        ) from None
    if len(values) != len(entries):
        raise ValueError(f"Slot drives {len(entries)} params but got {len(values)} values")
    return values


def apply_bindings(chunk: "core.ChunkBase", name: str, values: dict) -> None:
    """Write a handle's slot values back through the chunk's params.

    `values` is a slot dict, never a matrix -- keyed by the same slot names the driver map
    uses:

        apply_bindings(chunk, "body",  {"translate": Gf.Vec3d(1, 2, 3)})
        apply_bindings(chunk, "zdist", {"value": 12.5})

    Native hosts already produce a scalar for a scalar handle (Houdini's linear handle and
    Blender's arrow gizmo both hand back a float), so making them compose a matrix just so we
    can extract the float again is work in both directions -- and lossy in the direction that
    matters, since a float inside a matrix no longer says which axis it came from. Arity
    comes from the driver map instead of the signature, so one call shape covers every gizmo
    type without this boundary knowing any geometry.

    Hosts that genuinely hold a matrix call `decompose()` to get the slot dict.
    """
    gizmo = find_gizmo(chunk, name)
    if gizmo is None:
        raise ValueError(f"Chunk {chunk.name!r} declares no gizmo named {name!r}")

    for slot, value in values.items():
        entries = gizmo.driver.get(slot)
        if entries is None:
            # Loudly: a host sending a slot this gizmo does not declare is a contract
            # mismatch, and silently dropping it looks exactly like a handle that does not work.
            raise KeyError(
                f"Gizmo {name!r} on chunk {chunk.name!r} has no driver for slot {slot!r}. "
                f"Declared slots: {sorted(gizmo.driver)}"
            )
        for entry, item in zip(entries, _values_for(entries, value)):
            # A cross-chunk target that has gone away drops out here rather than taking the
            # whole drag with it. Writing the param -- on whichever chunk owns it -- is what
            # dirties that chunk and propagates downstream, which is the entire reason this
            # goes through params instead of writing prims.
            if (param := _param_for_entry(chunk, entry)) is not None:
                param.value = item
