"""The chunk prim schema — every attribute the chunk system persists on a USD prim.

Before this module the schema was a set of string literals: ``"info:chunkType"`` appeared
in five files, ``"chunk:activeView"`` was authored from both ``chunk_usd`` and the Qt node
graph, and each persisted flag came with its own hand-written getter/setter pair repeating
the same resolve-prim / get-attr / Set-with-default shape. Eight such pairs had drifted into
three different write policies between them.

What a caller has to know now is one thing: the :class:`PrimAttr` describing the state they
want. :func:`read` and :func:`write` handle the rest.

Where the state lives
---------------------
Two different prims hold chunk state, and picking the wrong one is a documented footgun
rather than an error you can see:

* :attr:`Where.CHUNK` — the chunk's own prim, resolved as a *child* of its context.
* :attr:`Where.CONTEXT` — a CONTEXT chunk's (tab or chunkgroup) *own* prim.

``chunk_usd.get_chunk_prim_path`` answers the first. Called on a context it double-appends
the context's name, because ``ChunkBase.get_context()`` returns ``self`` for a chunk that
already carries the CONTEXT flag, so ``_resolve_chunk_root`` already resolved to
``context.prim_path``. That bug shipped once in the filter-bar accessors. Making the choice
a field of the attribute's own declaration means a caller can no longer make it by accident.

The write policy
----------------
Every write goes to the attribute's *existing home* (``chunk_utils.get_attr_home_layer``)
when it has one. Only a first write — nothing authored anywhere yet — consults the
attribute's declared :class:`Home`.

That rule already existed, correctly, in ``set_chunk_pinned`` alone. The three filter-bar
setters re-resolved the stage's ambient edit target on every write instead, so a toggle made
before an Edit Target chunk moved that target and one made after landed in two different
layers. The older, stronger opinion kept winning and the toggle silently stopped responding.
Stating the rule once here is what fixes all three at once, and is why it is stated once
rather than per accessor.

The one thing that does stay per attribute is where a *first* write goes, because the two
kinds of state genuinely differ: graph scaffolding belongs in the bookkeeping layer, and
anything the user expects to save has to land somewhere that exports. Getting that backwards
is silent in both directions — a dropped setting on reload one way, a scattered container
prim the other — so it is a declared field rather than a default anyone can drift from.
"""

from __future__ import annotations

import dataclasses
from enum import Enum, auto
from typing import Any, Callable, TYPE_CHECKING

from pxr import Sdf, Usd

from usd_chunks import chunk_notice, chunk_utils
from usd_chunks.chunk_flags import ChunkFlags

if TYPE_CHECKING:  # pragma: no cover - typing only
    from usd_chunks import chunk_core as core


# --------------------------------------------------------------------------------------
# Prim types and roots
# --------------------------------------------------------------------------------------

#: Type name of a chunk's own prim.
CHUNK_PRIM_TYPENAME = "Chunk"

#: Type name of the ``/chunks`` scope every chunk prim hangs under.
CHUNKS_ROOT_TYPENAME = "Scope"

#: Where chunks live when they have no context of their own.
CHUNKS_ROOT = "/chunks"


# --------------------------------------------------------------------------------------
# Attribute names
# --------------------------------------------------------------------------------------

#: Which chunk module authored this prim ("view", "merge", "chunkgroup", a module name...).
CHUNK_TYPE = "info:chunkType"

#: Set on a chunk prim created at runtime rather than loaded from a module.
CHUNK_VERSION = "info:chunkVersion"

#: Exec-chain ports.
EXEC_INPUT = "inputs:chunk"
EXEC_OUTPUT = "outputs:chunk"

#: Persisted per-chunk state.
PINNED = "chunk:pinned"
ACTIVE_VIEW = "chunk:activeView"
EDIT_TARGET_LAYER = "chunk:editTargetLayer"

#: Persisted per-context Params-page filter state.
FILTER_PIN_ENABLED = "filter:pinEnabled"
FILTER_SEARCH_QUERY = "filter:searchQuery"
FILTER_SEARCH_SCOPE = "filter:searchScope"

#: Node position, via UsdUI.NodeGraphNodeAPI.
NODE_POS = "ui:nodegraph:node:pos"

#: The accepted values of :data:`FILTER_SEARCH_SCOPE`. An unrecognised stored value reads
#: back as the first entry rather than propagating out to callers.
SEARCH_SCOPES = ("both", "name", "type")


class Where(Enum):
    """Which prim an attribute lives on. See the module docstring."""

    #: The chunk's own prim, resolved as a child of its context.
    CHUNK = auto()
    #: A CONTEXT chunk's own prim (a tab or a chunkgroup), never a child of it.
    CONTEXT = auto()


class Home(Enum):
    """Which layer a *first* write lands in, when the attribute has no spec anywhere yet.

    Every later write goes to wherever the attribute already lives, whichever value this
    takes — that part is the fix, and it is not negotiable per attribute. This only decides
    where the first one goes, and the two answers are genuinely different requirements:
    """

    #: The chunk's bookkeeping layer (``chunk_utils.get_bookkeeping_layer``): follows the
    #: Edit Target for a regular chunk, and is pinned to the session layer for a CONTEXT,
    #: so a container's own scaffolding never scatters across layers over its lifetime.
    #: Right for state that describes the graph's wiring.
    BOOKKEEPING = auto()

    #: The stage's ambient edit target. Right for state a user expects to *save* — a tab's
    #: filter bar is part of the document, and the session layer does not export. Pinning
    #: this to the bookkeeping layer instead silently drops the setting on reload.
    EDIT_TARGET = auto()


@dataclasses.dataclass(frozen=True)
class PrimAttr:
    """One persisted piece of chunk state.

    Args:
        name: The USD attribute name.
        type: Its ``Sdf.ValueTypeName``.
        default: What :func:`read` returns when nothing is authored. Absent always means
            the pre-feature behaviour, so old scenes keep working — that is why the
            defaults are declared here rather than decided per call site.
        where: Which prim holds it.
        home: Which layer a first write lands in. See :class:`Home`.
        coerce: Applied to an authored value on read. Returning ``None`` rejects the stored
            value and falls back to ``default`` — used where a stale scene can hold
            something no longer valid.
    """

    name: str
    type: Any
    default: Any
    where: Where
    home: Home = Home.BOOKKEEPING
    coerce: Callable[[Any], Any] | None = None


def _scope(val: Any) -> "str | None":
    return val if val in SEARCH_SCOPES else None


#: The pin state of one chunk in its context's Params page. Absent means pinned, so a scene
#: saved before pinning shipped does not lose Params visibility.
ATTR_PINNED = PrimAttr(PINNED, Sdf.ValueTypeNames.Bool, True, Where.CHUNK, Home.BOOKKEEPING, bool)

#: Which view slot (1/2/3) drives execution for a chunkgroup.
ATTR_ACTIVE_VIEW = PrimAttr(ACTIVE_VIEW, Sdf.ValueTypeNames.Int, 0, Where.CONTEXT, Home.EDIT_TARGET, int)

#: Whether a context's Params page applies the pin filter. Absent means applied.
ATTR_PIN_FILTER = PrimAttr(FILTER_PIN_ENABLED, Sdf.ValueTypeNames.Bool, True, Where.CONTEXT, Home.EDIT_TARGET, bool)

#: A context's Params-page search box contents. Absent means nothing was ever typed.
ATTR_SEARCH_QUERY = PrimAttr(FILTER_SEARCH_QUERY, Sdf.ValueTypeNames.String, "", Where.CONTEXT, Home.EDIT_TARGET, str)

#: Which fields that search box matches against.
ATTR_SEARCH_SCOPE = PrimAttr(FILTER_SEARCH_SCOPE, Sdf.ValueTypeNames.String, "both", Where.CONTEXT, Home.EDIT_TARGET, _scope)


# --------------------------------------------------------------------------------------
# Prim resolution
# --------------------------------------------------------------------------------------

def prim_path(chunk: "core.ChunkBase", where: Where) -> "str | None":
    """The prim path holding ``where``-scoped state for ``chunk``, or None if there is none.

    ``Where.CONTEXT`` is ``chunk.prim_path`` directly: both a plain tab and a chunkgroup have
    it assigned at creation (``add_context`` / the group-node assignment in
    ``_on_param_added``). Deriving it instead is the double-append bug the module docstring
    describes.
    """
    if where is Where.CONTEXT:
        return chunk.prim_path
    # Local import: this module is core and chunk_usd is a plugin, so the dependency only
    # runs at call time. chunk_utils.get_bookkeeping_layer resolves the same cycle the same
    # way, for the same reason.
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

    return get_chunk_prim_path(chunk)


def is_context(chunk: "core.ChunkBase") -> bool:
    """True if ``chunk`` is a CONTEXT — a tab or a chunkgroup."""
    return bool(chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT))


def chunk_type(prim: "Usd.Prim") -> "str | None":
    """The ``info:chunkType`` of ``prim``, or None if it has none.

    Safe on a prim that is invalid or carries no such attribute, which is why call sites
    should prefer it to ``prim.GetAttribute(...).Get()`` spelled out by hand.
    """
    if not (prim and prim.IsValid()):
        return None
    return prim.GetAttribute(CHUNK_TYPE).Get()


# --------------------------------------------------------------------------------------
# Read / write
# --------------------------------------------------------------------------------------

def read_authored(chunk: "core.ChunkBase", attr: PrimAttr) -> Any:
    """``attr``'s authored value for ``chunk``, or None if nothing usable is authored.

    Distinct from :func:`read` for the callers that have to tell "nobody has decided this
    yet" apart from "somebody decided the value that happens to equal the default" — pinning
    has a three-way fallback that turns on exactly that difference.

    Never raises for a missing stage, prim or attribute: each of those is a way of not
    being authored.
    """
    stage = chunk.get_stage()
    path = prim_path(chunk, attr.where)
    if not (stage and path):
        return None
    prim = stage.GetPrimAtPath(Sdf.Path(path))
    if not prim.IsValid():
        return None
    usd_attr = prim.GetAttribute(attr.name)
    if not (usd_attr and usd_attr.IsValid()):
        return None
    val = usd_attr.Get()
    if val is None:
        return None
    return attr.coerce(val) if attr.coerce is not None else val


def read(chunk: "core.ChunkBase", attr: PrimAttr) -> Any:
    """Return ``attr``'s value for ``chunk``, or ``attr.default`` if nothing is authored."""
    val = read_authored(chunk, attr)
    return attr.default if val is None else val


def write(chunk: "core.ChunkBase", attr: PrimAttr, value: Any) -> bool:
    """Author ``value`` for ``attr`` on ``chunk``'s prim. Returns whether it was written.

    Defines the prim on demand. A plain tab context's prim is never authored by
    ``_define_prim`` — ``SceneSender.add_chunk`` passes ``send_added=False`` for a CONTEXT
    chunk with no module, so PARAM_ADDED never fires for it and the prim only starts existing
    as an implicit ancestor once a child chunk appears under it. An empty, just-created tab
    therefore has no prim at all, and silently doing nothing would lose the setting.

    The write goes to the attribute's existing home layer, falling back to the chunk's
    bookkeeping layer — never the ambient edit target. See the module docstring for what
    goes wrong otherwise.
    """
    stage = chunk.get_stage()
    path = prim_path(chunk, attr.where)
    if not (stage and path):
        return False

    prev_write_active = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim.IsValid():
            prim = stage.DefinePrim(Sdf.Path(path), CHUNK_PRIM_TYPENAME)
            if not prim.IsValid():
                return False
        existing = prim.GetAttribute(attr.name)
        target = chunk_utils.get_attr_home_layer(existing) if (existing and existing.IsValid()) else None
        if target is None and attr.home is Home.BOOKKEEPING:
            target = chunk_utils.get_bookkeeping_layer(stage, chunk)
        if target is None:
            # Home.EDIT_TARGET with nothing authored yet: the stage's ambient target, which
            # is what Usd.EditContext would resolve to anyway. Left explicit so the two
            # policies read as one decision rather than one policy and one omission.
            prim.CreateAttribute(attr.name, attr.type, custom=True).Set(value)
            return True
        with Usd.EditContext(stage, target):
            prim.CreateAttribute(attr.name, attr.type, custom=True).Set(value)
        return True
    finally:
        chunk_notice._usd_write_active = prev_write_active
