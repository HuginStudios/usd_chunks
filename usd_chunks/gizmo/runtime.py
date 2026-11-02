"""Viewport gizmos: declaration -> USD -> write-back.

**usd_chunks records. The DCC interprets.** This module authors the `ui:gizmo:*` schema
onto chunk prims, reads it back as plain records, and owns the write-back. It does not
draw, pick or drag -- that lives in the viewport layer (`usd-guides` for usdview and
Kraken) or in a host's native handle system.

The whole "hook into any DCC" surface is four calls:

    chunk_gizmo.resolve_gizmos(prim) -> list[GizmoDesc]   # what handles exist
    chunk_gizmo.apply_bindings(chunk, name, value)        # write-back
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
from collections.abc import Iterable
from typing import Any

import attrs
from pxr import Gf, Sdf, Usd, UsdGeom, UsdUI

from usd_chunks import chunk_core as core
from usd_chunks import chunk_naming
from usd_chunks import chunk_notice
from usd_chunks import chunk_stage
from usd_chunks import chunk_utils
from usd_chunks.chunk_registry import ChunkLayerManager, ChunkRegistry

logger = logging.getLogger(__name__)

GIZMO_PREFIX = "ui:gizmo:"
_INPUTS_PREFIX = "inputs:"


@attrs.frozen
class GizmoDesc:
    """What a host needs to build a handle, and nothing else.

    Deliberately a plain data record: type token, resolved anchor paths, driven properties,
    space, editmode. No Qt types, no callbacks into host-specific objects, nothing beyond
    what `pxr` gives every host. If a third-party DCC needs something that is not on this
    record, the schema is under-specified and that is a finding, not a reason to widen the
    record with host-shaped data.
    """

    name: str
    type: str
    label: str = ""
    # Which chunk declared this handle. A host needs it for two things: to filter to the
    # chunk the user has selected, and to name the chunk when writing back. Without it the
    # loop cannot be closed at all -- `name` is not unique across chunks, since more than
    # one of them calls its handle "transform".
    chunk_prim: Sdf.Path | None = None
    anchor: tuple = ()          # resolved scene prims the handle hangs off
    anchor_source: str = ""     # the param that produced them, for our own write-back
    driver: tuple = ()          # the properties this handle writes, in order
    axis: str | None = None
    space: str = "local"
    editmode: tuple = (core.GIZMO_DEFAULT_EDIT_MODE,)
    offset: tuple | None = None   # where the handle sits relative to the anchor
    size: float | None = None     # relative size hint; None means the host decides


# ---------------------------------------------------------------------------
# attribute names
#
# Read and write go through the same helpers so the two can never drift -- the failure
# mode otherwise is authoring one spelling and resolving another, which looks like "no
# gizmos" rather than like a bug.
# ---------------------------------------------------------------------------

def _attr_name(gizmo_name: str, *parts: str) -> str:
    return GIZMO_PREFIX + ":".join((gizmo_name, *parts))


def _driver_attr_name(gizmo_name: str) -> str:
    return _attr_name(gizmo_name, "driver")


# ---------------------------------------------------------------------------
# declaration -> chunk
# ---------------------------------------------------------------------------

def get_gizmos(chunk: core.ChunkBase) -> list:
    """Every gizmo a chunk declares -- in config(), plus any `gizmo=` sugar on its params.

    Desugaring happens here rather than at load because it needs both halves at once: the
    type token lives on the param (built in main()) and the resulting Gizmo belongs beside
    the config's list (built in config()). The two are assembled at different times, and
    every caller that wants gizmos already comes through this function.

    The sugar's *validation* is not deferred -- that runs in the param's constructor, so a
    bad `gizmo=` still fails at load rather than at first drag.

    Args:
        chunk: The chunk to read.

    Returns:
        The chunk's gizmos; an explicit Gizmo wins over sugar of the same name.
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


def _sugared_gizmos(chunk: core.ChunkBase) -> list:
    """`gizmo=` kwargs on this chunk's params, as Gizmo records.

    A scalar param drives itself; a Vector drives itself as one vector-typed attribute; a
    Group drives its children. Sugar is type-only, so anything wanting an axis, an editmode,
    an offset or a size escalates to the full Gizmo in config() -- exactly one escalation
    path, with no middle ground between them.
    """
    if whole_chunk := _whole_chunk_transform(chunk):
        # Absorbs the groups below it rather than sitting beside them. Three handles and one
        # combined handle over the same nine params would fight for the same drag, and the
        # host has no way to know they are the same thing.
        return [whole_chunk]

    sugared = []
    for path, param in _iter_params(chunk):
        if gizmo_type := getattr(param, "gizmo", None):
            sugared.append(core.GizmoBase(
                name=param.name,
                type=gizmo_type,
                label=param.label or param.name,
                driver=(path,),
            ))

    for path, group in _iter_groups(chunk):
        if gizmo_type := getattr(group, "gizmo", None):
            # The group already *is* the params the handle drives, so nothing needs listing.
            # This is the lightest declaration there is, and the reason it exists: spelling
            # the same three param paths out in config() was consistently longer than every
            # other line in the config put together.
            children = tuple(
                f"{path}.{child.name}" for child in group.get_my_params()
                if isinstance(child, core.ChunkParamBase)
            )
            if not children:
                logger.warning("Group %r declares gizmo=%r but has no params to drive", group.name, gizmo_type)
                continue
            sugared.append(core.GizmoBase(
                name=group.name,
                type=gizmo_type,
                label=group.label or group.name,
                driver=children,
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
                driver=(path,),
            ))
    return sugared


_TRANSFORM_PARTS = ("translate", "rotate", "scale")


def _whole_chunk_transform(chunk: core.ChunkBase):
    """The single transform handle a chunk declares with `gizmo="transform"`, if it does.

    The params come from the groups already carrying `gizmo="translate"` / `"rotate"` /
    `"scale"`, so the chunk-level kwarg says only *how to present* them -- as one native
    manipulator rather than three. No naming convention to guess at, and a chunk that wants
    three separate handles just leaves the chunk-level kwarg off.
    """
    if getattr(chunk, "gizmo", None) != core.GIZMO_TRANSFORM_TYPE:
        return None

    by_type = {}
    for path, group in _iter_groups(chunk):
        if (gizmo_type := getattr(group, "gizmo", None)) in _TRANSFORM_PARTS:
            by_type[gizmo_type] = tuple(
                f"{path}.{child.name}" for child in group.get_my_params()
                if isinstance(child, core.ChunkParamBase)
            )

    missing = [part for part in _TRANSFORM_PARTS if len(by_type.get(part, ())) != 3]
    if missing:
        logger.warning(
            "Chunk %r declares gizmo=transform but has no three-param group for: %s",
            chunk.name, ", ".join(missing),
        )
        return None

    return core.GizmoBase(
        name=core.GIZMO_TRANSFORM_TYPE,
        type=core.GIZMO_TRANSFORM_TYPE,
        label=chunk.name,
        driver=tuple(entry for part in _TRANSFORM_PARTS for entry in by_type[part]),
    )


def find_gizmo(chunk: core.ChunkBase, name: str):
    """The gizmo called `name` on a chunk, or None."""
    for gizmo in get_gizmos(chunk):
        if gizmo.name == name:
            return gizmo
    return None


def resolve_anchor_param(chunk: core.ChunkBase, gizmo) -> core.ChunkParamBase:
    """The param holding the target prim path for this gizmo.

    Name-first, then the flag. Across chunks/, the name is the more reliable signal: only
    about half the chunks with a target path set PRIM_SELECTION (camera.py, light.py,
    reference.py and others name the param but never flag it), while the name
    `primpath_location` is near-universal.

    Args:
        chunk: The chunk declaring the gizmo.
        gizmo: The gizmo whose anchor to find.

    Returns:
        The anchor param.

    Raises:
        ValueError: The anchor cannot be resolved. Raising rather than returning None -- a
            gizmo whose anchor cannot be resolved has nowhere to draw, and failing at load
            with the chunk's name in the message is far cheaper to diagnose than a handle that
            silently never appears.
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


def _anchor_targets(chunk: core.ChunkBase, gizmo, stage: Usd.Stage) -> list:
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
    """(path, param) for every scalar param under obj, not crossing into a nested chunk.

    A near-copy of chunk_stage._iter_chunk_params, duplicated deliberately: that module lives
    under plugins/ and this one must stay importable by a non-Qt, non-plugin host. Eight
    lines of walk is a cheaper price than inverting the dependency direction.
    """
    for param in obj.get_my_params():
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkParamBase):
            yield path, param
        elif isinstance(param, (core.ChunkListGroupBase, core.ChunkVectorBase, core.ChunkBase)):
            continue
        else:
            yield from _iter_params(param, path)


def _iter_groups(obj, prefix: str = ""):
    """(path, group) for every plain container under obj, not crossing into a nested chunk.

    Vectors and list groups are excluded: a Vector is one attribute rather than a bag of
    params, and a list group's contents change size at runtime, so neither can stand in for
    "these are the params my handle drives". A nested ChunkBase is excluded too -- despite
    being a ChunkGroupBase itself, it is a distinct chunk with its own gizmos resolved via
    its own get_gizmos() call, so treating it as a plain group here would let its top-level
    `gizmo=` sugar (e.g. a Transform chunk's `gizmo="transform"`) get misread against only
    its direct scalar params instead of the whole chunk it actually belongs to. This is the
    boundary a container's gizmo scan must stop at -- get_gizmos() is called on one chunk
    at a time, never invoked to aggregate a container's descendants.
    """
    for param in obj.get_my_params():
        if isinstance(param, (core.ChunkParamBase, core.ChunkVectorBase, core.ChunkListGroupBase, core.ChunkBase)):
            continue
        path = f"{prefix}.{param.name}" if prefix else param.name
        yield path, param
        yield from _iter_groups(param, path)


def _iter_vectors(obj, prefix: str = ""):
    """(path, vector) for every ChunkVectorBase under obj, not crossing into a nested chunk.

    Vectors are skipped by _iter_params, which walks scalars only.
    """
    for param in obj.get_my_params():
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkVectorBase):
            yield path, param
        elif not isinstance(param, (core.ChunkParamBase, core.ChunkListGroupBase, core.ChunkBase)):
            yield from _iter_vectors(param, path)


def _param_path(chunk: core.ChunkBase, entry: str) -> str:
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


def _driver_targets(prim: Usd.Prim, chunk: core.ChunkBase, entries: Iterable[str]) -> list:
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


def author_gizmos(stage: Usd.Stage, prim: Usd.Prim, chunk: core.ChunkBase) -> None:
    """Author every gizmo a chunk declares onto its chunk prim.

    Called from both chunk-prim authoring sites -- create-time and re-author/export. Both
    need it; updating one and missing the other is the obvious failure mode, and the symptom
    (gizmos that work until you save and reload) is not obviously a gizmo bug.

    Assumes the caller has already entered the right edit context, exactly like the
    config.outputs loop it sits beside.

    Args:
        stage: The stage to author on.
        prim: The chunk's prim.
        chunk: The chunk whose gizmos to author.
    """
    gizmos = get_gizmos(chunk)
    if not gizmos or not (prim and prim.IsValid()):
        return

    # One change notice instead of one per create/variability/value: a single transform
    # gizmo was 17 of the 31 notices a new Transform chunk sent, and every one of them can
    # make a host refresh (usdview resets its GUI and repaints on any ObjectsChanged). Safe
    # inside a block because the prim already exists -- unlike DefinePrim -- and nothing
    # below reads back a property it created in the same pass.
    with Sdf.ChangeBlock():
        for gizmo in gizmos:
            try:
                _author_one(stage, prim, chunk, gizmo)
            except Exception:
                # One malformed gizmo must not take the chunk prim down with it -- the rest of
                # the prim's bookkeeping (exec ports, params) is what makes the chunk work at all.
                logger.exception("Failed to author gizmo %r on chunk %r", gizmo.name, chunk.name)


def _author_hidden(prim: Usd.Prim, chunk: core.ChunkBase, name: str) -> None:
    """Author (or clear) the suppression flag for one handle.

    Written into USD rather than only answered by resolve_gizmos, so a host doing pure-USD
    discovery honours it too. Cleared rather than left stale when the chunk stops
    suppressing -- a handle that never comes back is worse than one that never hid.
    """
    hidden = is_gizmo_hidden(chunk, name)
    attr_name = _attr_name(name, "hidden")
    if hidden:
        attr = prim.CreateAttribute(attr_name, Sdf.ValueTypeNames.Bool, custom=False)
        attr.SetVariability(Sdf.VariabilityUniform)
        attr.Set(True)
    elif (existing := prim.GetAttribute(attr_name)) and existing.IsValid():
        existing.Set(False)


def _author_offset(prim: Usd.Prim, chunk: core.ChunkBase, gizmo) -> None:
    """Author (or clear) where one handle sits.

    Only authored when there is one. An absent offset means "on the anchor, host's choice of
    exactly where", which is usually the right answer -- a viewport puts a handle on the
    geometry it can see, and only it knows where that is.
    """
    offset = gizmo_offset(chunk, gizmo.name, gizmo.offset)
    attr_name = _attr_name(gizmo.name, "offset")
    existing = prim.GetAttribute(attr_name)

    if offset is None:
        # Cleared rather than left stale, exactly like the hidden flag: a handle stuck at a
        # pivot the user has since zeroed is worse than one that never moved.
        if existing and existing.IsValid():
            existing.Clear()
        return

    value = Gf.Vec3f(*offset)
    if existing and existing.IsValid() and existing.Get() == value:
        return
    attr = prim.CreateAttribute(attr_name, Sdf.ValueTypeNames.Float3, custom=False)
    attr.SetVariability(Sdf.VariabilityUniform)
    attr.Set(value)


def _author_one(stage: Usd.Stage, prim: Usd.Prim, chunk: core.ChunkBase, gizmo) -> None:
    _uniform_token(prim, _attr_name(gizmo.name, "type")).Set(gizmo.type)
    _uniform_token(prim, _attr_name(gizmo.name, "label")).Set(gizmo.label or chunk.name)
    _uniform_token(prim, _attr_name(gizmo.name, "editmode"), array=True).Set(list(gizmo.editmode))

    _author_hidden(prim, chunk, gizmo.name)

    if gizmo.axis is not None:
        _uniform_token(prim, _attr_name(gizmo.name, "axis")).Set(gizmo.axis)
    if gizmo.space and gizmo.space != "local":
        _uniform_token(prim, _attr_name(gizmo.name, "space")).Set(gizmo.space)

    _author_offset(prim, chunk, gizmo)

    # Only authored when set. An absent size means "host decides", which is usually the
    # right answer -- a viewport wants a screen-constant handle and only it knows the camera
    # distance, so authoring a default here would be us guessing at something we cannot see.
    if gizmo.size is not None:
        attr = prim.CreateAttribute(_attr_name(gizmo.name, "size"), Sdf.ValueTypeNames.Float, custom=False)
        attr.SetVariability(Sdf.VariabilityUniform)
        attr.Set(float(gizmo.size))

    # anchorSource is for us, not for hosts: write-back needs to know which param produced
    # the target, and a host that had to read it would be back to the two-hop lookup this
    # schema exists to avoid.
    anchor_param = resolve_anchor_param(chunk, gizmo)
    _uniform_token(prim, _attr_name(gizmo.name, "anchorSource")).Set(anchor_param.name)

    rel = prim.CreateRelationship(_attr_name(gizmo.name, "anchor"), custom=False)
    rel.SetTargets(_anchor_targets(chunk, gizmo, stage))

    attr = _uniform_token(prim, _driver_attr_name(gizmo.name))
    attr.SetConnections(_driver_targets(prim, chunk, gizmo.driver))


def refresh_gizmo_anchors(stage: Usd.Stage, prim: Usd.Prim, chunk: core.ChunkBase) -> None:
    """Re-resolve every gizmo's anchor rel, offset and suppression flag, from current params.

    Cheap no-op for the overwhelming majority of chunks, which declare no gizmos at all --
    that early return is why this can hang off the generic param-changed path.

    Args:
        stage: The stage to author on.
        prim: The chunk's prim.
        chunk: The chunk whose gizmos to refresh.
    """
    gizmos = get_gizmos(chunk)
    if not gizmos or not (prim and prim.IsValid()):
        return

    for gizmo in gizmos:
        # Suppression is a mode toggle, so it changes far more often than an anchor does --
        # ticking lock_to_freecam has to hide the handle without waiting for a re-author.
        try:
            _author_hidden(prim, chunk, gizmo.name)
        except Exception:
            logger.debug("Could not refresh hidden flag for %r", gizmo.name, exc_info=True)

        # Same reasoning again, and more so: a pivot is dragged, so the handle has to follow
        # it live rather than at the next re-author. _author_offset skips the write when the
        # value has not moved, which is every param change that is not the pivot itself.
        try:
            _author_offset(prim, chunk, gizmo)
        except Exception:
            logger.debug("Could not refresh offset for %r", gizmo.name, exc_info=True)

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


def _read_anchor(prim: Usd.Prim, rel: Usd.Relationship, name: str, chunk=None) -> tuple:
    """The anchor rel's targets, without composing one a hosted network's file wrote.

    A network file anchors its gizmos at scene prims (/Headboard). Referenced in by a
    Reference or Payload node, those lie outside the reference: USD drops them with a
    warning on every read, and the handle never attaches. Such an anchor is recomputed
    from the chunk's own anchor param instead, which resolves under the host's namespace.
    """
    if not rel:
        return ()
    stack = rel.GetPropertyStack()
    if not stack or stack[0].layer in prim.GetStage().GetLayerStack():
        return tuple(rel.GetTargets())
    if chunk is None:
        scene = getattr(ChunkRegistry(), "scene", None)
        chunk = next((c for c in (scene.get_chunks() if scene else [])
                      if (p := _chunk_prim(prim.GetStage(), c)) is not None and p.GetPath() == prim.GetPath()),
                     None)
    gizmo = next((g for g in get_gizmos(chunk) if g.name == name), None) if chunk is not None else None
    if gizmo is None:
        return ()
    try:
        return tuple(_anchor_targets(chunk, gizmo, prim.GetStage()))
    except Exception:
        logger.debug("Could not resolve anchor for gizmo %r on %s", name, prim.GetPath(), exc_info=True)
        return ()


def _read_gizmos(prim: Usd.Prim, chunk=None) -> list:
    descs = []
    for name in _gizmo_names(prim):
        hidden_attr = prim.GetAttribute(_attr_name(name, "hidden"))
        if hidden_attr and hidden_attr.Get():
            # Suppressed by its own chunk. Dropped here rather than flagged, so a host
            # cannot draw it by forgetting to check.
            continue

        type_attr = prim.GetAttribute(_attr_name(name, "type"))
        gizmo_type = type_attr.Get() if type_attr else None
        if not gizmo_type:
            # Namespaced attrs with no type are not a gizmo -- skip rather than fabricate one.
            continue

        driver_attr = prim.GetAttribute(_driver_attr_name(name))
        driver = tuple(driver_attr.GetConnections()) if driver_attr else ()

        anchor_rel = prim.GetRelationship(_attr_name(name, "anchor"))
        label_attr = prim.GetAttribute(_attr_name(name, "label"))
        axis_attr = prim.GetAttribute(_attr_name(name, "axis"))
        space_attr = prim.GetAttribute(_attr_name(name, "space"))
        source_attr = prim.GetAttribute(_attr_name(name, "anchorSource"))
        mode_attr = prim.GetAttribute(_attr_name(name, "editmode"))
        modes = tuple(mode_attr.Get() or ()) if mode_attr else ()
        offset_attr = prim.GetAttribute(_attr_name(name, "offset"))
        size_attr = prim.GetAttribute(_attr_name(name, "size"))
        offset = offset_attr.Get() if offset_attr else None
        size = size_attr.Get() if size_attr else None

        descs.append(GizmoDesc(
            name=name,
            type=gizmo_type,
            chunk_prim=prim.GetPath(),
            label=(label_attr.Get() if label_attr else "") or "",
            anchor=_read_anchor(prim, anchor_rel, name, chunk),
            anchor_source=(source_attr.Get() if source_attr else "") or "",
            driver=driver,
            axis=(axis_attr.Get() if axis_attr else None) or None,
            space=(space_attr.Get() if space_attr else None) or "local",
            editmode=modes or (core.GIZMO_DEFAULT_EDIT_MODE,),
            offset=tuple(offset) if offset is not None else None,
            size=float(size) if size is not None else None,
        ))
    return descs


def chunks_for_prim(prim: Usd.Prim) -> list:
    """Every chunk that authored a spec on this prim, via the layer stack.

    Each chunk already owns its own Sdf.Layer, so a prim's spec stack *already records*
    which chunks touched it -- no scan of /chunks, no separate index to keep in sync, and
    no anchor matching. It also stays correct for free when a chunk is disabled or its layer
    muted: no spec in the stack, no handle.

    Args:
        prim: A scene prim.

    Returns:
        The chunks that touched it.
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
        if layer is None or not _is_meaningful_spec(spec):
            continue
        for chunk in by_identifier.get(layer.identifier, ()):
            if not any(chunk is seen for seen in found):
                found.append(chunk)
    return found


def _is_meaningful_spec(spec) -> bool:
    """Whether this spec is the chunk actually touching the prim, or just scaffolding.

    Authoring an override on a prim implicitly creates empty `over` specs for every
    ancestor, because the descendant needs a path to hang off. Those are not a claim on the
    ancestor, but they are in its prim stack all the same -- so a Transform chunk driving
    `/apple/grp/mesh` looked like it also drove `/apple` and `/apple/grp`.

    Only one direction is allowed: a descendant is not scaffolding for an ancestor.
    Dragging a mesh and then its parent must make a second chunk, just as dragging the
    parent and then the mesh does.

    Anything the chunk genuinely authored -- a property, metadata such as an `active` flag
    from a prune, or a specifier that defines rather than overrides -- counts.
    """
    if spec.properties:
        return True
    if set(spec.ListInfoKeys()) - {"specifier"}:
        return True
    return spec.specifier != Sdf.SpecifierOver


def _chunk_prim(stage: Usd.Stage, chunk: core.ChunkBase) -> Usd.Prim | None:
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

    Args:
        prim: The selected prim, or a chunk prim.

    Returns:
        Gizmos from *every* chunk that touched the prim, upstream ones included. The host
        filters to the selected chunk -- a prim touched by two transform chunks in one chain
        would otherwise get two identical handles in the same place. Stated from the other
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
            descs.extend(_read_gizmos(chunk_prim, chunk))
    # Then add anything anchored here that has not authored yet. The layer-stack route only
    # sees chunks that have already written something, and plenty have not: rbd_solver is
    # button-driven, so its offset handles -- the very things you want before running a
    # simulation -- would not appear until after the first one. An Offset Transform sitting
    # at zero authors nothing either, by design.
    #
    # Appended rather than used only when the first route comes back empty, which is what
    # this was at first. That was wrong in a way that only shows in a chain: one Transform
    # that HAS authored was enough to make an un-authored Offset Transform on the same prim
    # invisible, so it could never be dragged into offering its first value.
    #
    # Order matters and is preserved: chunks_for_prim walks GetPrimStack(), which USD
    # returns strength-ordered, so the chunk whose result is actually on screen stays first
    # and a host driving handles[0] drives the one the user can see.
    seen = {(str(d.chunk_prim), d.name) for d in descs}
    for desc in _gizmos_anchored_at(prim):
        if (str(desc.chunk_prim), desc.name) not in seen:
            descs.append(desc)
    return _ordered_downstream_first(stage, descs)


def _exec_depth(stage: Usd.Stage, chunk_prim_path) -> int:
    """How far down the exec chain a chunk prim sits. Bigger is further downstream."""
    depth, path, seen = 0, Sdf.Path(str(chunk_prim_path)), set()
    while path not in seen:
        seen.add(path)
        prim = stage.GetPrimAtPath(path)
        if not (prim and prim.IsValid()):
            break
        upstream = chunk_stage.exec_input_connections(prim)
        if not upstream:
            break
        # The strongest branch, matching how the chain is read everywhere else. A merge
        # feeding several inputs makes depth approximate, and approximate is fine: it only
        # decides which of two handles on ONE prim is offered first.
        path = upstream[0].GetPrimPath()
        depth += 1
    return depth


def _ordered_downstream_first(stage: Usd.Stage, descs: list) -> list:
    """Order gizmo descs nearest the view first, so descs[0] is the chunk with the final say.

    Layer strength alone is not enough. A chunk that has not authored yet is not in the
    prim's layer stack at all -- an Offset Transform sitting at zero authors nothing by
    design -- so it would sort last however far downstream it is, and dragging would keep
    editing the upstream Transform until a value was typed into the Offset by hand.
    Ownership must not depend on whether a number has been entered.

    Exec depth answers it for authored and un-authored chunks alike, and agrees with layer
    strength where both apply: recook already orders each graph layer's subLayerPaths so the
    chunk nearest the view wins, so this is the same rule read off the graph rather than off
    the composed result.
    """
    if len(descs) < 2:
        return descs
    depths = {}
    for desc in descs:
        key = str(desc.chunk_prim)
        if key not in depths:
            depths[key] = _exec_depth(stage, desc.chunk_prim)
    # Stable, so handles from one chunk keep their declared order and equal-depth chunks
    # keep the layer-stack order they arrived in.
    return sorted(descs, key=lambda d: -depths[str(d.chunk_prim)])


def _gizmos_anchored_at(prim: Usd.Prim) -> list:
    """Gizmos from any chunk whose anchor resolves to `prim`."""
    scene = getattr(ChunkRegistry(), "scene", None)
    if scene is None:
        return []
    stage = prim.GetStage()
    prim_path = prim.GetPath()
    found = []
    for chunk in scene.get_chunks():
        if _is_silenced(stage, chunk):
            continue
        chunk_prim = _chunk_prim(stage, chunk)
        if chunk_prim is None or chunk_prim.GetPath() == prim_path:
            continue
        for desc in _read_gizmos(chunk_prim, chunk):
            if any(Sdf.Path(str(a)) == prim_path for a in desc.anchor):
                found.append(desc)
    return found


def _is_silenced(stage: Usd.Stage, chunk: core.ChunkBase) -> bool:
    """Whether this chunk should be offering no handles at all.

    The layer-stack route gets this for free -- a disabled or muted chunk has no spec in the
    prim's stack, so it simply does not come up. Anchors carry no such information: they are
    authored once and keep pointing at the prim regardless, so muting a chunk's layer would
    leave its handles on screen driving a chunk that is no longer contributing anything.
    """
    if chunk.has_state(core.ChunkFlags.State.DISABLED):
        return True
    try:
        layer = chunk.get_edit_context()
    except Exception:  # noqa: BLE001 - a chunk without a layer is not muted
        return False
    return bool(layer is not None and stage.IsLayerMuted(layer.identifier))


# ---------------------------------------------------------------------------
# matrix -> one value per handle
# ---------------------------------------------------------------------------

# Below this, cos(ry) is small enough that the Z angle stops being separable from X and the
# atan2 pair below turns numerical noise into large, jittery angles. At exactly +-90 degrees
# they are genuinely not separable at all (gimbal lock) -- Z is folded into X by convention.
_GIMBAL_EPSILON = 1e-9


def decompose(matrix: Gf.Matrix4d, pivot: tuple | None = None) -> dict:
    """Split a TRS matrix into one value per handle, in the convention transform.py composes in.

    The result is keyed by gizmo *type*, so each entry goes straight to the handle of that
    name::

        for gizmo_type, value in decompose(matrix).items():
            apply_bindings(chunk, handles[gizmo_type], value)

    **Offered to hosts, not required of them.** A native handle that already produces Euler
    angles ignores this entirely. Keeping it here means the Euler convention lives in exactly
    one place instead of being re-derived, subtly differently, in every host.

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

    Args:
        matrix: The TRS matrix.
        pivot: The point the chunk rotates and scales about, in the same space the matrix is
            expressed in. Rotation and scale come off the 3x3 and do not care where the pivot
            is; only the translation does, so that is the sole correction made here. None
            gives the plain origin-pivot decomposition.

    Returns:
        ``{"translate": Vec3d, "rotate": Vec3d, "scale": Vec3d}``. Rotations are Euler XYZ
        in degrees.
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
        "translate": _translation_about(matrix, transform.GetTranslation(), pivot),
        "rotate": Gf.Vec3d(math.degrees(angle_x), math.degrees(angle_y), math.degrees(angle_z)),
        "scale": Gf.Vec3d(transform.GetScale()),
    }


def _translation_about(matrix: Gf.Matrix4d, translation, pivot: tuple | None) -> Gf.Vec3d:
    """The translate a pivoted composition needs to land on the same matrix.

    A chunk composing about a pivot builds ``T(-p) * S * R * T(p) * T(t)``, whose translation
    row is ``-p*A + p + t`` for the linear part A. So the plain decomposition hands back
    ``t - (p*A - p)`` and the difference has to be added back, or a pivoted chunk would
    place the object short of where the handle put it -- by exactly the amount the pivot
    swings under the rotation, which reads as the object lagging the mouse.
    """
    translate = Gf.Vec3d(translation)
    if pivot is None:
        return translate

    point = Gf.Vec3d(*pivot)
    linear = Gf.Matrix4d(matrix)
    linear.SetTranslateOnly(Gf.Vec3d(0, 0, 0))
    return translate + linear.Transform(point) - point


# ---------------------------------------------------------------------------
# write-back
# ---------------------------------------------------------------------------

def chunk_at_prim_path(path) -> core.ChunkBase | None:
    """The chunk whose chunk prim sits at `path`, if it is still around.

    The bridge from a GizmoDesc back to something writable: `resolve_gizmos` gives a host
    plain data, and `apply_bindings` needs the live chunk.

    Args:
        path: The chunk prim's path.

    Returns:
        The live chunk, or None.
    """
    scene = getattr(ChunkRegistry(), "scene", None)
    if scene is None:
        return None
    wanted = str(path)
    for candidate in scene.get_chunks():
        own = getattr(candidate, "_exec_prim_path", None) or getattr(candidate, "prim_path", None)
        if own and str(own) == wanted:
            return candidate
    return None


def _param_for_entry(chunk: core.ChunkBase, entry: str):
    """The param a driver entry names.

    Own chunk only. An earlier draft let an entry be a full property path into another
    chunk, so a camera's body handle could drive an upstream Transform's params -- but the
    answer to that case turned out to be "put the handle on the Transform chunk", the way
    Houdini drops an Edit node at the end of a chain. Nothing needed the indirection.

    A name that does not resolve is a declaration bug -- the chunk's own config naming a
    param it does not have -- so it raises rather than quietly doing nothing.
    """
    param = chunk.get_param(str(entry).strip())
    if param is None:
        raise ValueError(f"Chunk {chunk.name!r} has no param {entry!r} to drive")
    return param


GIZMO_HIDDEN_HOOK = "gizmo_hidden"


def is_gizmo_hidden(chunk: core.ChunkBase, name: str) -> bool:
    """Whether a chunk is currently suppressing one of its own handles.

    Some chunks have a mode that fights a handle. camera.py's `lock_to_freecam` re-authors
    the camera's transform from the DCC freecam on every CAMERA_CHANGED, so a transform
    drag would be overwritten a frame later -- the handle would look draggable and silently
    snap back, which is the worst of both.

    A chunk opts in with a module-level function::

        def gizmo_hidden(node, gizmo):
            return gizmo == "transform" and node.get_param("lock_to_freecam").value

    Chunk-side rather than a call into the host: it needs no new contract, works in every
    host including ones that never import us, and the chunk is the only thing that knows
    its own modes.

    Args:
        chunk: The chunk that declares the gizmo.
        name: The gizmo's name.

    Returns:
        True if the handle should not be shown. A hook that raises counts as False.
    """
    hook = chunk._module_hook(GIZMO_HIDDEN_HOOK)
    if hook is None:
        return False
    try:
        return bool(hook(chunk, name))
    except Exception:
        # A failing hook must not take the handle away -- a visible handle that misbehaves
        # is easier to diagnose than one that silently never appears.
        logger.exception("gizmo_hidden hook failed for gizmo %r on chunk %r", name, chunk.name)
        return False


GIZMO_OFFSET_HOOK = "gizmo_offset"


def gizmo_offset(chunk: core.ChunkBase, name: str, declared: tuple | None = None) -> tuple | None:
    """Where a chunk wants one of its handles right now, in the anchor's local space.

    The declared `offset` on a Gizmo is static config, decided at load. A pivot is not: it is
    a param the user moves, and the handle has to follow it within the same drag. A chunk
    opts in with a module-level function::

        def gizmo_offset(node, gizmo):
            return pivot(node) if gizmo == "transform" else None

    Chunk-side rather than a call into the host, for the same reason as `gizmo_hidden`: the
    chunk is the only thing that knows its own params, and a host reading the authored
    `ui:gizmo:*:offset` attribute needs to know none of this.

    Args:
        chunk: The chunk that declares the gizmo.
        name: The gizmo's name.
        declared: The static offset from the Gizmo declaration.

    Returns:
        The hook's offset, or `declared` when the hook returns None. None means "no opinion"
        and the declared value stands -- *not* "clear it". A chunk with several handles would
        otherwise wipe the static offset on the ones its hook does not answer for.
    """
    hook = chunk._module_hook(GIZMO_OFFSET_HOOK)
    if hook is None:
        return declared

    try:
        value = hook(chunk, name)
    except Exception:
        # A failing hook falls back to the declaration rather than dropping the offset: a
        # handle in slightly the wrong place beats one that jumps somewhere unrelated.
        logger.exception("gizmo_offset hook failed for gizmo %r on chunk %r", name, chunk.name)
        return declared

    if value is None:
        return declared
    return tuple(float(component) for component in value)


GIZMO_OFFSET_CHANGED_HOOK = "gizmo_offset_changed"


def _anchor_prim(chunk: core.ChunkBase, gizmo) -> Usd.Prim | None:
    """The prim a handle's offset is measured against -- the first anchor that resolves.

    First, not all of them: an offset is one point, so it can only be expressed in one prim's
    space. Same choice the drag write-back already makes for its reference prim.
    """
    stage = chunk.get_stage()
    if stage is None:
        return None
    for path in _anchor_targets(chunk, gizmo, stage):
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            return prim
    return None


def apply_offset(chunk: core.ChunkBase, name: str, world_point: Any) -> bool:
    """Move where a handle sits, from a point in **world space**.

    The write counterpart to the `gizmo_offset` hook, and the reason a host can offer
    drag-the-pivot at all. A host has a world position under the mouse and nothing else --
    it does not know that a Transform chunk keeps its pivot in the anchor's object space, and
    should not have to. So the conversion happens here, exactly as `apply_bindings` takes an
    absolute matrix and lets the chunk work out its own share.

    A chunk opts in with a module-level function::

        def gizmo_offset_changed(node, gizmo, value):
            ...             # value is a Gf.Vec3d in the anchor's own space
            return True     # and say so

    There is no declarative path here on purpose: where a chunk keeps its pivot is not
    something a driver list can express, since it also has to hold the object still.

    Args:
        chunk: The chunk, or its chunk prim path.
        name: The gizmo's name.
        world_point: The new handle position, in world space.

    Returns:
        True if the chunk took it. False -- including the common "not my gizmo" -- means
        nothing was written, and a host should then leave the pivot alone rather than fall
        back to moving the prim.

    Raises:
        ValueError: No chunk exists at the given path.
    """
    if not isinstance(chunk, core.ChunkBase):
        resolved = chunk_at_prim_path(chunk)
        if resolved is None:
            raise ValueError(f"No chunk at {chunk!r} to move {name!r} to")
        chunk = resolved

    gizmo = find_gizmo(chunk, name)
    if gizmo is None:
        raise ValueError(f"Chunk {chunk.name!r} declares no gizmo named {name!r}")

    hook = chunk._module_hook(GIZMO_OFFSET_CHANGED_HOOK)
    if hook is None:
        return False

    prim = _anchor_prim(chunk, gizmo)
    if prim is None:
        return False

    # Into the anchor's own space, the same frame `gizmo_offset` reports back out in -- so a
    # pivot dragged to a point on screen reads back as the handle sitting on that point.
    to_world = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    point = to_world.GetInverse().Transform(Gf.Vec3d(*world_point))

    try:
        handled = bool(hook(chunk, name, point))
    except Exception:
        # Treated as handled, for the same reason as gizmo_changed: falling through after a
        # half-finished write would combine two intents in one frame, and the next tick of
        # the drag retries anyway.
        logger.exception("gizmo_offset_changed hook failed for gizmo %r on chunk %r", name, chunk.name)
        return True

    if handled:
        chunk.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_CHANGED, gizmo.name)
    return handled


GIZMO_STROKE_HOOK = "gizmo_stroke"


def apply_stroke(chunk: core.ChunkBase, name: str, samples: Any) -> bool:
    """Hand a brush gesture to the chunk.

    The only handle type whose payload is not a value. Every other one sends back a number, a
    vector or a matrix, and a brush sends a gesture -- so there is no declarative driver path
    here, and a chunk that wants one implements::

        def gizmo_stroke(node, gizmo, samples):
            ...             # accumulate into whatever the chunk owns
            return True

    **The host does not rasterise.** It sends the samples it has and this side resolves them
    onto the surface, for the same reason `decompose()` lives here: otherwise every host
    reimplements falloff slightly differently and two DCCs paint differently from the same
    gesture.

    **Nothing keeps a stroke.** The samples are accumulated and dropped; there is no stroke
    list, no history, no replay. The host owns the gesture, the chunk owns the field. That is
    what keeps a brush inside "usd_chunks records, the DCC interprets" rather than an
    exception to it -- a stroke log would be a recording of host behaviour, replayable only by
    something that agreed on what a stroke meant.

    Args:
        chunk: The chunk, or its chunk prim path.
        name: The gizmo's name.
        samples: A sequence of ``(Gf.Vec3d world_point, float weight)`` -- where the user
            touched the surface this tick and how hard. **World space**, like
            `apply_offset`: a host knows where its cursor hit geometry and nothing else about
            what a chunk means by it.

    Returns:
        True if the chunk took the stroke.

    Raises:
        ValueError: No chunk exists at the given path.
    """
    if not isinstance(chunk, core.ChunkBase):
        resolved = chunk_at_prim_path(chunk)
        if resolved is None:
            raise ValueError(f"No chunk at {chunk!r} to stroke {name!r} on")
        chunk = resolved

    gizmo = find_gizmo(chunk, name)
    if gizmo is None:
        raise ValueError(f"Chunk {chunk.name!r} declares no gizmo named {name!r}")

    hook = chunk._module_hook(GIZMO_STROKE_HOOK)
    if hook is None:
        return False

    try:
        handled = bool(hook(chunk, name, samples))
    except Exception:
        # Treated as handled, in the same family as gizmo_changed: a half-finished stroke
        # followed by a second interpretation of the same gesture is worse than a dropped
        # tick, and the next tick of the drag comes along anyway.
        logger.exception("gizmo_stroke hook failed for gizmo %r on chunk %r", name, chunk.name)
        return True

    if handled:
        chunk.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_CHANGED, gizmo.name)
    return handled


GIZMO_CHANGED_HOOK = "gizmo_changed"


def _dispatch_solve_hook(chunk: core.ChunkBase, name: str, value: Any) -> bool:
    """Give the chunk's module a chance to solve this drag itself. True if it did.

    Some handles are not 1:1 with any param, so there is nothing to declare. A lookat point's
    world position is ``camPos + aimDir * focusDistance``, so dragging it has to decompose
    back into *rotate* params -- no declarative binding can express that.

    A chunk opts in with a module-level function::

        def gizmo_changed(node, gizmo, value):
            if gizmo == "aim":
                ...            # decompose into rx/ry/rz + focusDistance
                return True     # and say so

    **Returning true means "I own this gizmo"** and the declarative drivers are skipped.
    Anything falsy -- including the very common "this is not my gizmo, fall through" -- runs
    the normal path. Signalling explicitly beats inferring from side effects, which would
    mean guessing whether a hook that wrote nothing declined or simply had nothing to do.

    The DCC contract is identical either way: the host emits a value and never learns
    what a chunk means by them. That property is the only reason this hook is acceptable --
    anything making hosts aware of per-chunk solving would be a design failure.

    Note ``gizmo_changed`` is a reserved module-level name, in the same family as
    ``execute(node)``. A chunk module must not use it for anything else.
    """
    # The registry's copy, found by the chunk's module name. Chunk modules exist twice in
    # sys.modules under different keys, and the other copy is not the one the running chunk
    # was built from -- looking the hook up by import would find a function whose closure
    # belongs to a different object graph.
    # That copy is the one _module_hook reads.
    hook = chunk._module_hook(GIZMO_CHANGED_HOOK)
    if hook is None:
        return False

    try:
        return bool(hook(chunk, name, value))
    except Exception:
        # Treated as handled, deliberately. Falling through to the declarative path after a
        # half-finished custom solve would combine two different intents into one frame,
        # which is worse than dropping a tick -- the next tick of the drag retries anyway.
        logger.exception("gizmo_changed hook failed for gizmo %r on chunk %r", name, chunk.name)
        return True


def _values_for(entries: tuple, value: Any) -> list:
    """One value per driven param.

    A single entry takes the value whole -- that covers both a scalar handle and a
    vector-typed param, which is stored as one attribute rather than three. Several entries
    means the value is unpacked across them, which is the ordinary translate/rotate/scale
    case where the params really are separate floats.
    """
    if len(entries) == core.GIZMO_TRANSFORM_ARITY and isinstance(value, Gf.Matrix4d):
        # The one type whose payload is a matrix: the host ran its own transform manipulator
        # and handed back the result whole. Flattened here in the declared order rather than
        # asking the host to take it apart, so the Euler convention stays on this side.
        parts = decompose(value)
        return [component for part in _TRANSFORM_PARTS for component in parts[part]]

    if len(entries) == 1:
        return [value]
    try:
        values = list(value)
    except TypeError:
        raise ValueError(
            f"Handle drives {len(entries)} params but got a single {type(value).__name__}"
        ) from None
    if len(values) != len(entries):
        raise ValueError(f"Handle drives {len(entries)} params but got {len(values)} values")
    return values


def apply_bindings(chunk: core.ChunkBase, name: str, value: Any) -> None:
    """Write a handle's value back through the chunk's params.

    One value, because a gizmo drives one thing and its **type** says what::

        apply_bindings(chunk, "translate_grp", Gf.Vec3d(1, 2, 3))   # type="translate"
        apply_bindings(chunk, "zdist", 12.5)                        # type="cube"

    Never a matrix. Native hosts already produce a scalar for a scalar handle -- Houdini's
    linear handle and Blender's arrow gizmo both hand back a float -- so making them compose
    a matrix just so we can extract the float again is work in both directions, and lossy in
    the direction that matters, since a float inside a matrix no longer says which axis it
    came from. A host holding a full transform calls `decompose()` and makes one call per
    handle, which is what a viewport does anyway: you grab the arrows or the ring, never a
    single thing that is somehow both.

    Args:
        chunk: The chunk, or its chunk prim path.
        name: The gizmo's name.
        value: A `Gf.Vec3d` for a manipulator type (rotations are Euler XYZ in degrees), a
            single number for a shape type.

    Raises:
        ValueError: No chunk exists at the given path.
    """
    # A host holds a GizmoDesc, which carries a chunk *path* rather than a live object --
    # accept either, so closing the loop needs no lookup the caller has to know about.
    if not isinstance(chunk, core.ChunkBase):
        resolved = chunk_at_prim_path(chunk)
        if resolved is None:
            raise ValueError(f"No chunk at {chunk!r} to write {name!r} to")
        chunk = resolved

    gizmo = find_gizmo(chunk, name)
    if gizmo is None:
        raise ValueError(f"Chunk {chunk.name!r} declares no gizmo named {name!r}")

    if _dispatch_solve_hook(chunk, name, value):
        chunk.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_CHANGED, name)
        return

    # Resolve every param before writing any of them, so a bad driver cannot leave the chunk
    # half-updated -- mid-drag that would show as the object tearing apart rather than as an
    # error.
    items = _values_for(gizmo.driver, value)
    params = [_param_for_entry(chunk, entry) for entry in gizmo.driver]

    for param, item in zip(params, items):
        # Writing the param is what dirties the chunk and propagates downstream, which is
        # the entire reason this goes through params instead of writing prims.
        param.value = item

    # One per tick, inside the START/STOP bracket. Deliberately after the writes, so a
    # listener seeing CHANGED can read the new values straight off the params.
    chunk.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_CHANGED, gizmo.name)


# ---------------------------------------------------------------------------
# auto-create: a prim nothing drives still gets a chunk
# ---------------------------------------------------------------------------

#: Module a chunk is created from when a prim has no transform handle of its own.
AUTO_TRANSFORM_MODULE = "transform"


#: Handle types that place a prim in the world. A chunk offering any of these owns that
#: prim's placement, and must not have a second chunk stacked on top doing the same job.
GIZMO_PLACEMENT_TYPES = core.GIZMO_MANIPULATOR_TYPES | {core.GIZMO_TRANSFORM_TYPE}


def placement_gizmos(prim: Usd.Prim) -> list:
    """Every handle that positions `prim`, from any chunk.

    Deliberately not "is there a transform handle". A chunk can own a prim's placement
    without offering a single combined manipulator: rbd_solver declares separate `translate`
    and `rotate` offsets and no `scale`, so it cannot form a whole-chunk transform -- yet it
    plainly owns where that body starts. Testing only for `transform` would report the prim
    as unowned and stack an auto-created Transform chunk on top of the solver, which is the
    exact duplicate this check exists to prevent.

    Args:
        prim: The scene prim.

    Returns:
        The placement handles.
    """
    return [d for d in resolve_gizmos(prim) if d.type in GIZMO_PLACEMENT_TYPES]


def transform_desc(prim: Usd.Prim):
    """The single whole-transform handle for `prim`, if some chunk offers one.

    Narrower than `placement_gizmos` on purpose: this is "who takes a whole matrix", which
    is not the same question as "who owns this prim".

    Args:
        prim: The scene prim.

    Returns:
        The transform handle, or None.
    """
    for desc in resolve_gizmos(prim):
        if desc.type == core.GIZMO_TRANSFORM_TYPE:
            return desc
    return None


def ensure_transform_chunk(prim: Usd.Prim, *, create: bool = True):
    """A transform handle for `prim`, creating a Transform chunk if nothing offers one.

    The point is an invariant, not a convenience: **a viewport edit must never land on the
    prim directly.** A direct write is not in the graph, so it is not reproducible, does not
    survive a chunk re-cook, and is not what gets written out when the scene is saved. If
    the only way to drag a prim is through a chunk, then dragging a prim that no chunk owns
    has to *make* one -- the same move Houdini makes when editing geometry drops an Edit
    node in front of you.

    The new chunk lands at the end of the active context's chain, seeded from the prim's
    current local transform so that creating it does not move anything.

    Args:
        prim: The scene prim.
        create: False only looks, never creates.

    Returns:
        The handle to write through, or None if a chunk could not be created (no
        ``transform`` module registered, no live scene) -- callers decide whether that is
        fatal.
    """
    if not (prim and prim.IsValid()):
        return None

    # Ownership is the broad question (anything that places the prim), the return value is
    # the narrow one (who takes a whole matrix). A prim owned by separate translate/rotate
    # handles gets no chunk created AND no transform desc -- the caller drives those handles
    # individually via decompose(), which is what the host contract already tells it to do.
    if placement_gizmos(prim):
        return transform_desc(prim)
    if not create:
        return None

    # Deliberately no seeding. A chunk owns its own xformOp and composes on top of what is
    # already there, so an untouched chunk contributes identity and moves nothing -- and a
    # node behaves the same whether it was dragged into existence or added by hand. The
    # drag itself is converted from absolute to op-local by the module's gizmo_changed hook.
    chunk = _create_chunk_at_tail(AUTO_TRANSFORM_MODULE)
    if chunk is None:
        return None

    _target_prim(chunk, prim)

    # Re-resolve rather than synthesising: the desc a caller gets from here has to be the
    # same record it would have got had the chunk existed all along, anchor included.
    return transform_desc(prim) or _own_transform_desc(chunk)


def _own_transform_desc(chunk: core.ChunkBase):
    """The chunk's own transform gizmo, read off the chunk rather than the scene prim.

    Fallback for the window where the chunk exists but its `ui:gizmo:*` has not been
    authored onto a prim yet -- resolve_gizmos goes via the layer stack, which only knows
    about the prim once the chunk has cooked at least once.
    """
    stage = getattr(chunk, "get_stage", lambda: None)()
    chunk_prim = _chunk_prim(stage, chunk) if stage is not None else None
    if chunk_prim is None:
        return None
    for desc in _read_gizmos(chunk_prim, chunk):
        if desc.type == core.GIZMO_TRANSFORM_TYPE:
            return desc
    return None


def _create_chunk_at_tail(module_name: str):
    """Create one chunk from `module_name` and add it to the end of the active chain."""
    registry = ChunkRegistry()
    entry = getattr(registry, "registered_modules", {}).get(module_name)
    scene = getattr(registry, "scene", None)
    if entry is None or scene is None:
        logger.warning("Cannot auto-create %r: module or scene unavailable", module_name)
        return None

    created = entry.create() if entry.loaded else entry.load()
    if not created:
        logger.warning("Module %r produced no chunk", module_name)
        return None
    chunk = created[0]

    # Borrow the Params-tab create semantics -- chain onto the tail of the current context
    # and pull any view along with it. Without this the chunk is created unconnected, which
    # is right when a user places a node by hand and wrong here: nobody asked for a node, so
    # nobody is going to wire it up. Not lofted, though: nobody asked for it on the Params
    # page either.
    prev_create, prev_loft = chunk_stage._params_tab_create, chunk_stage._loft_new_chunks
    prev_pos = chunk_stage._create_node_pos
    chunk_stage._params_tab_create = True
    chunk_stage._loft_new_chunks = False
    # Where the node lands, read before the create so the tail is still the tail. The default
    # (child-count * step) counts a view as a child, so an auto-created node is placed by how
    # many prims happen to exist rather than by where it sits in the chain -- which is how it
    # ends up stacked behind the view instead of after the chunk it was wired onto.
    tail = _tail_for_new_chunk(scene)
    chunk_stage._create_node_pos = _position_after(tail)
    # A header, like a node made from the menu: a run of drags stacks them under a Merge.
    chunk_stage.start_collapsed([chunk])
    try:
        scene.add_chunk(chunk)
    finally:
        chunk_stage._params_tab_create = prev_create
        chunk_stage._loft_new_chunks = prev_loft
        chunk_stage._create_node_pos = prev_pos

    # The Params-tab create leaves an Output where the user wired it. Nobody wired this
    # chunk, though: left there, the Output still took the old tail and the drag's chunk
    # dangled after it, delivering nothing. It goes between the tail and the Output.
    if tail is not None and _chained_onto(chunk, tail):
        chunk_stage.ChunkStageSync()._rewire_views_from_source(tail, chunk, outputs=True)
        merge = _gather_into_merge(scene, chunk, tail)
        if merge is not None:
            _nudge_downstream_views(merge)
            return chunk

    _nudge_downstream_views(chunk)
    return chunk


#: Module the Transforms a run of viewport drags makes are gathered into.
GATHER_MODULE = "merge"

#: How far above the last branch a Transform gathered into a Merge is placed: open, and as
#: a collapsed header (48 tall, plus the gap a multi-file drop leaves).
_BRANCH_STEP = 160
_COLLAPSED_BRANCH_STEP = 64


def _is_module_prim(prim: Usd.Prim | None, module_name: str) -> bool:
    """Whether a chunk prim was made from module_name, read off the prim (is_merge_prim's way)."""
    if prim is None or not prim.IsValid():
        return False
    attr = prim.GetAttribute(chunk_naming.CHUNK_TYPE_ATTR)
    return bool(attr and attr.IsValid() and attr.Get() == module_name)


def _set_exec_sources(stage: Usd.Stage, prim: Usd.Prim, sources: list) -> None:
    """Point prim's exec input at sources, where its spec already lives."""
    attr = prim.GetAttribute(chunk_stage._EXEC_INPUT)
    if not (attr and attr.IsValid()):
        return
    target = chunk_utils.get_attr_home_layer(attr) or chunk_utils.get_bookkeeping_layer(stage, None)
    with Usd.EditContext(stage, target):
        attr.SetConnections(list(sources))


def _set_node_pos(stage: Usd.Stage, chunk: core.ChunkBase, prim: Usd.Prim, pos) -> None:
    with Usd.EditContext(stage, chunk_utils.get_bookkeeping_layer(stage, chunk)):
        UsdUI.NodeGraphNodeAPI.Apply(prim).CreatePosAttr().Set(Gf.Vec2f(*pos))


def _gather_into_merge(scene, chunk: core.ChunkBase, tail: core.ChunkBase):
    """Make the Transform just chained onto tail a branch of a Merge instead.

    Each drag of a prim nothing owns makes a Transform, and chained one after another a
    dozen drags make a dozen nodes in a row. They move different prims, so their order means
    nothing: stacked under one Merge they read as the set of edits they are. So the second
    Transform in a row makes a Merge, and every one after that joins it. What fed the first
    goes in the weakest slot and the Transforms, which need no input of their own, in the
    stronger ones, the newest strongest: so each outranks what came before it, as it would
    chained after it. Anything else at the tail keeps the plain chain.

    Args:
        scene: The scene the chunks live in.
        chunk: The new Transform, already wired after tail and feeding what tail fed.
        tail: The chunk it was chained onto.

    Returns:
        The Merge the Transform went into, or None when it stays chained.
    """
    sync = chunk_stage.ChunkStageSync()
    stage = getattr(sync, "_stage", None)
    new_prim = _chunk_prim(stage, chunk) if stage is not None else None
    tail_prim = _chunk_prim(stage, tail) if stage is not None else None
    if new_prim is None or tail_prim is None or not _is_module_prim(new_prim, AUTO_TRANSFORM_MODULE):
        return None

    if chunk_stage.is_merge_prim(tail_prim):
        branches = [stage.GetPrimAtPath(c.GetPrimPath())
                    for c in chunk_stage.exec_input_connections(tail_prim)]
        gathered = [b for b in branches if _is_gathered(b)]
        # At most one other branch, what came before: in the weakest slot, or the strongest
        # in a Merge gathered before the upstream moved to the weakest -- which this puts right.
        rest = [b for b in branches if not _is_gathered(b)]
        if not gathered or len(rest) > 1 or (rest and rest[0] not in (branches[0], branches[-1])):
            return None
        merge, merge_prim = tail, tail_prim
        upstream = [str(b.GetPath()) for b in rest]
    elif _is_module_prim(tail_prim, AUTO_TRANSFORM_MODULE):
        entry = getattr(ChunkRegistry(), "registered_modules", {}).get(GATHER_MODULE)
        if entry is None:
            return None
        tail_pos = _node_pos(tail_prim)
        prev_pos = chunk_stage._create_node_pos
        chunk_stage._create_node_pos = (
            None if tail_pos is None else (tail_pos[0] + chunk_stage._NODE_STEP, tail_pos[1]))
        try:
            created = entry.create() if entry.loaded else entry.load()
            if not created:
                return None
            merge = created[0]
            scene.add_chunk(merge)
        finally:
            chunk_stage._create_node_pos = prev_pos
        merge_prim = _chunk_prim(stage, merge)
        if merge_prim is None:
            return None
        upstream = [str(c.GetPrimPath()) for c in chunk_stage.exec_input_connections(tail_prim)]
        _set_exec_sources(stage, tail_prim, [])
        gathered = [tail_prim]
    else:
        return None

    _set_exec_sources(stage, new_prim, [])
    merge_path = str(merge_prim.GetPath())
    order = [str(new_prim.GetPath()), *(str(b.GetPath()) for b in gathered), *upstream]
    for slot, source in enumerate(order):
        chunk_stage.connect_exec(stage, scene, source, merge_path,
                                 input_name=chunk_naming.exec_input_name(slot))
    sync._rewire_views_from_source(chunk, merge, outputs=True)
    chunk_stage.reconcile_edit_targets(stage, scene)

    # Above the Transforms already there, the way the Merge's slots stack (USD is Y-up).
    placed = [p for p in (_node_pos(b) for b in gathered) if p is not None]
    if placed:
        step = (_COLLAPSED_BRANCH_STEP if chunk.has_state(core.ChunkFlags.State.COLLAPSED)
                else _BRANCH_STEP)
        _set_node_pos(stage, chunk, new_prim, (placed[0][0], max(p[1] for p in placed) + step))
    return merge


def _is_gathered(prim: Usd.Prim) -> bool:
    """A Transform standing in a Merge with no input of its own, as _gather_into_merge leaves it."""
    return (_is_module_prim(prim, AUTO_TRANSFORM_MODULE)
            and not chunk_stage.exec_input_connections(prim))


def _chained_onto(chunk: core.ChunkBase, upstream: core.ChunkBase) -> bool:
    """True if `chunk`'s exec input is wired to `upstream`."""
    stage = getattr(chunk_stage.ChunkStageSync(), "_stage", None)
    prim = _chunk_prim(stage, chunk) if stage is not None else None
    up_prim = _chunk_prim(stage, upstream) if stage is not None else None
    if prim is None or up_prim is None:
        return False
    return any(c.GetPrimPath() == up_prim.GetPath()
               for c in chunk_stage.exec_input_connections(prim))


def _node_pos(prim: Usd.Prim) -> tuple[float, float] | None:
    """A chunk prim's authored node-graph position, in USD space (Y-up)."""
    if prim is None or not prim.IsValid():
        return None
    pos_attr = UsdUI.NodeGraphNodeAPI(prim).GetPosAttr()
    if not (pos_attr and pos_attr.HasAuthoredValue()):
        return None
    value = pos_attr.Get()
    return (float(value[0]), float(value[1]))


def _tail_for_new_chunk(scene) -> core.ChunkBase | None:
    """The chunk a new node is about to be chained onto, or None.

    Takes the context off the *scene*, not off the chunk. The chunk has not been added yet,
    so `chunk.get_context()` is still None and the tail lookup silently finds nothing --
    which reads as "no tail" and falls back to the default placement being fixed here.
    """
    sync = chunk_stage.ChunkStageSync()
    stage = getattr(sync, "_stage", None)
    if stage is None:
        return None
    current = getattr(scene, "_current_context", None)
    context = current() if current else None
    if context is None:
        # Nothing dived into means the new chunk is going into the scene's default
        # chunkgroup, so that is where its tail is. Passing None looks under the /chunks
        # scope, which holds no chunks of its own any more -- the lookup found nothing,
        # read as "no tail", and the node landed on the default placement instead of after
        # the chunk it was being chained onto.
        peek = getattr(scene, "peek_root_context", None)
        context = peek() if peek else None
    return sync._find_dag_tail(context)


def _position_after(tail: core.ChunkBase | None) -> tuple[float, float] | None:
    """One step right of `tail`, where the node chained onto it goes.

    None means "no opinion" -- the caller's default placement then applies, which is right
    when there is no tail to sit after.
    """
    stage = getattr(chunk_stage.ChunkStageSync(), "_stage", None)
    if tail is None or stage is None:
        return None
    pos = _node_pos(_chunk_prim(stage, tail))
    return None if pos is None else (pos[0] + chunk_stage._NODE_STEP, pos[1])


def _nudge_downstream_views(chunk: core.ChunkBase) -> None:
    """Move any view or Output that just got repointed onto `chunk` out from under it.

    View-follows-tail rewires the view to consume the new chunk but leaves it where it was
    -- which is the slot the new node has just been placed in, so the two overlap.
    """
    sync = chunk_stage.ChunkStageSync()
    stage = getattr(sync, "_stage", None)
    if stage is None:
        return
    prim = _chunk_prim(stage, chunk)
    pos = _node_pos(prim)
    if pos is None:
        return

    scene = getattr(ChunkRegistry(), "scene", None)
    if scene is None:
        return
    target_x = pos[0] + chunk_stage._NODE_STEP
    for candidate in scene.get_chunks():
        if not (chunk_stage.is_view_chunk(candidate) or chunk_stage.is_output_chunk(candidate)):
            continue
        view_prim = _chunk_prim(stage, candidate)
        if view_prim is None:
            continue
        exec_in = view_prim.GetAttribute(chunk_stage._EXEC_INPUT)
        if not (exec_in and exec_in.IsValid()):
            continue
        if not any(c.GetPrimPath() == prim.GetPath() for c in exec_in.GetConnections()):
            continue
        view_pos = _node_pos(view_prim)
        if view_pos is None or view_pos[0] >= target_x:
            continue
        # Same home-layer rule as every other bit of node bookkeeping: write where the
        # attribute's spec already lives, not wherever the ambient edit target points.
        with Usd.EditContext(stage, chunk_utils.get_bookkeeping_layer(stage, candidate)):
            UsdUI.NodeGraphNodeAPI.Apply(view_prim).CreatePosAttr().Set(
                Gf.Vec2f(target_x, view_pos[1])
            )


def _target_prim(chunk: core.ChunkBase, prim: Usd.Prim) -> None:
    """Point the new chunk at the prim being dragged."""
    param = chunk.get_param("primpath_location")
    if param is None:
        raise ValueError(
            f"{AUTO_TRANSFORM_MODULE!r} has no 'primpath_location' param to target a prim with"
        )
    setter = getattr(param, "set_prims", None)
    if callable(setter):
        setter([prim])
    else:
        param.value = str(prim.GetPath())


# ---------------------------------------------------------------------------
# xformOpOrder, derived from the graph
# ---------------------------------------------------------------------------

def xform_op_order(node: core.ChunkBase, prim: Usd.Prim, my_op: str) -> list:
    """The full `xformOpOrder` for `prim`: its own ops, then every chunk's op upstream-first.

    Derived from the exec chain rather than from whatever happens to be composed when a
    chunk runs, and that is the whole point.

    Each chunk authors its own copy of `xformOpOrder` into its own layer, and USD keeps one
    copy -- the strongest. Building each copy from "what I can see right now" makes the
    copies DIFFER, so which one survives decides the result: a chunk that cooked before its
    neighbour existed holds a list naming only itself, and if that copy wins, the
    neighbour's op sits on the prim, typed and valued, and is never read. The chunk silently
    does nothing, which is worse than an odd order.

    Building every copy from the graph instead makes them IDENTICAL, so it stops mattering
    which one USD keeps. The contention is not resolved, it is made irrelevant -- which is
    also why this needs no coordination between chunks and no cook-order guarantees.

    The graph order and the op order are the same order: the chain says A feeds B, and the
    op list says A's op then B's op. One fact, written twice.

    Args:
        node: The chunk authoring the order.
        prim: The prim whose ops are ordered.
        my_op: The op `node` authors.

    Returns:
        The op names, in order.
    """
    stage = prim.GetStage()
    scene = getattr(ChunkRegistry(), "scene", None)
    depths: dict = {}
    # Only an op already on the prim can join the order, so with no other chunk op there,
    # there is no one to look for -- the usual case, and asking every chunk in a big scene
    # made each Transform cook cost the size of the scene.
    others = any(p.GetName() != my_op for p in prim.GetPropertiesInNamespace("xformOp:transform"))
    for owner in (scene.get_chunks() if scene is not None and others else ()):
        if owner is node:
            continue
        # Never mint here: asking whether some other chunk owns an op must not give it one.
        op_name = chunk_utils.existing_xform_op_name(owner)
        # Sourced from "who owns an op that is on this prim", NOT from who declares a gizmo.
        # The RBD solver bakes an op and declares no handle at all, so a gizmo-based lookup
        # left it out of everyone else's list -- and the moment another chunk's copy of the
        # order won, the bake was excluded and the simulation vanished.
        if not op_name or not prim.HasAttribute(op_name):
            continue
        owner_prim = _chunk_prim(stage, owner)
        if owner_prim is None:
            continue
        depths[op_name] = _exec_depth(stage, owner_prim.GetPath())
    # Depth only orders this op against others; alone, it sorts the same at any depth, and
    # measuring it walks the whole exec chain above this chunk.
    depths[my_op] = _exec_depth(stage, _own_prim_path(stage, node)) if depths else 0

    # The prim's own ops keep their place at the front. Anything a chunk owns is dropped and
    # re-added below, so an op does not appear twice when a chunk's position changes.
    composed = [op for op in (UsdGeom.Xformable(prim).GetXformOpOrderAttr().Get() or [])]
    base = [op for op in composed if op not in depths]
    # Ties keep a stable order by name so two unconnected chunks do not swap places between
    # cooks -- identical copies matter more here than any particular winner.
    return base + sorted(depths, key=lambda name: (depths[name], name))


def _own_prim_path(stage: Usd.Stage, node: core.ChunkBase):
    prim = _chunk_prim(stage, node)
    return prim.GetPath() if prim is not None else Sdf.Path("/")
