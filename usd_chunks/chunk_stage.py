"""USD I/O for chunks: layer management, stage synchronization and chunk serialization."""

from __future__ import annotations

import contextlib
import difflib
import functools
import logging
import math
import weakref
from pathlib import Path
from collections.abc import Callable, Generator, Iterable
from typing import TYPE_CHECKING

from pxr import Gf, Sdf, Tf, Usd, UsdGeom, UsdUI, UsdUtils, Vt

from usd_chunks import chunk as _chunk_mod
from usd_chunks import chunk_cook
from usd_chunks import chunk_glob
from usd_chunks import chunk_registry
from usd_chunks import chunk_core as core
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks import chunk_notice
from usd_chunks import chunk_scheduler
from usd_chunks import chunk_utils
from usd_chunks import skip_cook
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection, RegistrySingleton
from usd_chunks.chunk_registry import ChunkLayerManager  # noqa: F401  re-exported

if TYPE_CHECKING:  # pragma: no cover -- typing only: chunk_scene imports this module
    from usd_chunks import chunk_scene

from usd_chunks import chunk_layers, chunk_naming
from usd_chunks.chunk_topology import (  # noqa: F401  re-exported
    _iter_exec_upstream,
    _prim_edit_target_layer,
    _resolve_chunk_root,
    edit_layer_memo_affected,
    freeze_edit_layer_memo,
    get_chunk_prim_path,
    invalidate_edit_layer_memo,
    nearest_upstream_edit_layer,
    upstream_edit_target_layer,
)

logger = logging.getLogger(__name__)

# The chunk vocabulary itself lives in chunk_naming, which imports nothing and can be read
# without this module's machinery. Re-exported under the existing private names so every
# call site here and elsewhere is unaffected -- one definition, two spellings.
_CHUNK_TYPE_ATTR = chunk_naming.CHUNK_TYPE_ATTR
_INPUTS_PREFIX = chunk_naming.INPUTS_PREFIX
#: What a chunk with no module of its own is saved as, so it can be read back (chunks/core/runtime.py).
_RUNTIME_CHUNK_TYPE = "runtime"
# The ObjectsChanged field name for a connection edit; a value write reports "default".
_CONNECTION_PATHS_FIELD = "connectionPaths"
_EXEC_OUTPUT = chunk_naming.EXEC_OUTPUT
_EXEC_INPUT = chunk_naming.EXEC_INPUT
_CHUNK_PRIM_TYPENAME = chunk_naming.CHUNK_PRIM_TYPENAME
# Usd.Stage.Traverse()'s default predicate skips INACTIVE prims, and a disabled chunk is
# authored as exactly that (see _on_state_changed). Loading a file through the default
# predicate therefore never saw a disabled chunk at all -- it was in the file, with
# active=false recording why, and simply vanished on reopen.
_LOAD_INCLUDE_INACTIVE = Usd.PrimIsDefined & ~Usd.PrimIsAbstract
# /chunks is just a scope -- like /lights or /render. Nothing lives at it: the default
# chunkgroup sits under it, and everything else under that. Typing it Chunk would make the
# scope look like a chunk prim to anything reading the stage back.
_CHUNKS_ROOT_TYPENAME = chunk_naming.CHUNKS_ROOT_TYPENAME
_CHUNKS_ROOT = chunk_naming.CHUNKS_ROOT

# Merge chunks fan several exec branches into one node, so they need more than the single
# inputs:chunk every other chunk has. Extra slots are numbered from 1 -- slot 0 deliberately
# keeps the plain _EXEC_INPUT name so that any consumer which only ever looks up
# inputs:chunk (most of them: Edit Target resolution, exec healing, auto-chain on create)
# transparently sees the merge's PRIMARY input, which is the answer those consumers want
# anyway. Only code that genuinely needs every branch (edge drawing, tail detection,
# disconnect) has to iterate.
_EXEC_INPUT_NUMBERED = chunk_naming.EXEC_INPUT_NUMBERED

is_exec_input_name = chunk_naming.is_exec_input_name
exec_input_index = chunk_naming.exec_input_index
exec_input_name = chunk_naming.exec_input_name
iter_exec_inputs = chunk_naming.iter_exec_inputs


def exec_input_connections(prim: Usd.Prim) -> list[Sdf.Path]:
    """Every exec connection target on prim, strongest branch first."""
    return [conn for attr in iter_exec_inputs(prim) for conn in attr.GetConnections()]

# An Edit Target chunk publishes the identifier of the layer it currently points at onto
# its own prim under this attribute. The presence of the attr marks an exec-chain boundary
# for chunk_utils.get_edit_layer's upstream walk; its value names the layer everything
# downstream of it authors into. Publishing it (rather than importing the Edit Target
# script's resolution logic into core) keeps core decoupled from the script -- same
# published-marker pattern as chunk:pinned / set_chunk_pinned.
_EDIT_TARGET_MARKER = chunk_naming.EDIT_TARGET_MARKER

_NODE_STEP = 260  # horizontal spacing between nodes in the default auto-layout

# Set True by the Params-tab create path while adding a chunk, so _on_param_added inserts the new
# chunk before the active view (view-follows-tail). Node-graph creates leave it False -- there the
# user wires manually.
_params_tab_create = False

# Auto-pin decision for the next chunk(s) about to be created, or None when no create/paste flow
# is active (idle -- e.g. during a scene load). Tri-state, not a plain bool, to fix a real race:
# the Params-page widget's PARAM_ADDED listener can run BEFORE ChunkStageSync's own listener (both
# react to the same notice; delivery order isn't guaranteed), so the widget may ask is_chunk_pinned()
# before _define_prim has written chunk:pinned at all. is_chunk_pinned() falls back to this flag
# for a chunk with no authored value yet -- but only distinguishing "mid-creation, going to author
# False" (this flag is True/False) from "not creating anything right now, this is a genuinely old
# chunk with no attribute" (this flag is None, so the absent-means-pinned migration default holds)
# requires the None state; a plain bool defaulting False can't tell those two apart.
# True: created via the Params tab's own create menu, or pasted/duplicated while Params is showing.
# False: created via the Graph page's create menu, or pasted/duplicated while Graph is showing.
# Kept separate from _params_tab_create -- that flag also drives view-follows-tail auto-chaining,
# which paste must NOT pick up (it already copies the source's own wiring explicitly).
_pin_new_chunks: bool | None = None

# Set by the node graph to the USD-space (x, y) a new chunk node should be created at (e.g. the
# right-click / wire-drop point). _define_prim reads it for the node pos instead of the far-away
# index default. None → use the default. Cleared by the caller after the create.
_create_node_pos: tuple[float, float] | None = None


@contextlib.contextmanager
def creating_at(pos: tuple[float, float] | None, pin: bool | None = None):
    """Create chunks at `pos` (USD space) for the duration of the block.

    The module globals are how a caller passes a position and a pin preference down into
    _define_prim, which is several frames below and has no argument for either. Doing that
    save/assign/restore by hand in every caller would mean one try/finally per call site,
    each an opportunity to forget the restore and leave every later chunk landing at someone
    else's mouse position.

    Nesting works: the previous values come back rather than being cleared, so grouping
    (which creates a group, then materializes chunks inside it) does not lose the outer
    position when the inner block ends.

    Args:
        pos: The node position new chunks get, in USD coordinates, or None for the default.
        pin: Whether new chunks are pinned to the Params page; None leaves it unchanged.

    Yields:
        None, with the position and pin preference in effect.
    """
    global _create_node_pos, _pin_new_chunks
    previous_pos, previous_pin = _create_node_pos, _pin_new_chunks
    _create_node_pos = pos
    if pin is not None:
        _pin_new_chunks = pin
    try:
        yield
    finally:
        _create_node_pos = previous_pos
        _pin_new_chunks = previous_pin

# Just above float32's ~1e-7 relative precision. A param holds a Python float (64-bit) but
# is stored in USD as a 32-bit float, so a value that made the round trip comes back
# differing in the 8th significant digit -- e.g. param -3.3492076146840537 vs USD
# -3.349207639694214, seen live. Comparing those exactly makes them permanently unequal, so
# every notice mentioning the attribute would write the param again; harmless per write, but
# it dirties the chunk and cooks it, and resyncs are frequent enough for that to matter.
_FLOAT_AGREEMENT = 1e-6


def _values_agree(usd_value, param_value) -> bool:
    """Whether a value read back from USD is the same as the param's, allowing for float32.

    Falls back to plain equality for anything not a pair of numbers -- strings, tokens,
    bools -- where exactness is the whole point.
    """
    # Strings first, and not merely for speed: a str is iterable, and iterating one yields
    # 1-character strs that are iterable in turn, so falling through to the sequence branch
    # below recurses until the stack runs out. Equal-length strings are the trigger --
    # different lengths raise from strict=True and land on plain equality, which is why a
    # test comparing "/World/Cube" with "/World/Sphere" passes while any prim path compared
    # against another of the same length brings down whatever is holding the notice.
    if isinstance(usd_value, (str, bytes)) or isinstance(param_value, (str, bytes)):
        return usd_value == param_value
    if isinstance(usd_value, bool) or isinstance(param_value, bool):
        return usd_value == param_value
    if isinstance(usd_value, (int, float)) and isinstance(param_value, (int, float)):
        return math.isclose(usd_value, param_value, rel_tol=_FLOAT_AGREEMENT, abs_tol=_FLOAT_AGREEMENT)
    # Vectors and other sequences: compare component-wise under the same rule.
    try:
        pairs = list(zip(usd_value, param_value, strict=True))
    except (TypeError, ValueError):
        return usd_value == param_value
    return all(_values_agree(a, b) for a, b in pairs)


_SDF_TYPE_MAP = chunk_utils.SDF_TYPE_MAP


def param_sdf_type(param: core.ChunkParamBase) -> Sdf.ValueTypeName | None:
    """The type a param's chunk-prim attribute is created with, or None if it has none.

    From the value, or from the default while the value is a @Node.param@ expression -- the
    expression text is not the param's type, and a Slider wired to another chunk must still
    get a number attribute, or the first plain number typed into it after the wire is gone
    cannot be written.
    """
    raw = getattr(param, "raw_value", None)
    is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
    type_source = getattr(param, "default_value", None) if is_expr or raw is None else raw
    if ChunkFlags.ParamType.BUTTON in param.evaluate_type():
        # A button has no value. Its attribute exists only so Loft up can wire to it.
        return Sdf.ValueTypeNames.Bool
    # A ColorPicker holds an (r, g, b) tuple, and _SDF_TYPE_MAP is keyed by Python
    # type -- there is no tuple entry, so without this a colour param resolves to no
    # type at all and never gets a chunk-prim attr, leaving its value unable to
    # round-trip through USD.
    # Compared with ==: VECTOR | FLOAT_SLIDER also contains every bit of COLOR_PICKER, and
    # a wired Vector typed as a colour read back as a colour port once its chunk was gone.
    param_type = param.evaluate_type()
    if param_type == ChunkFlags.ParamType.COLOR_PICKER:
        return Sdf.ValueTypeNames.Color3f
    if isinstance(param, core.ChunkVectorBase):
        if ChunkFlags.ParamType.FLOAT in param_type:
            return _VECTOR_TYPE_MAP.get((float, param.length), (None, None))[0]
        return _sdf_vector_type(type_source)[0] if type_source else None
    return _SDF_TYPE_MAP.get(type(type_source))

_VECTOR_TYPE_MAP: dict = {
    (float, 2): (Sdf.ValueTypeNames.Float2, Gf.Vec2f),
    (float, 3): (Sdf.ValueTypeNames.Float3, Gf.Vec3f),
    (float, 4): (Sdf.ValueTypeNames.Float4, Gf.Vec4f),
    (int, 2): (Sdf.ValueTypeNames.Int2, Gf.Vec2i),
    (int, 3): (Sdf.ValueTypeNames.Int3, Gf.Vec3i),
    (int, 4): (Sdf.ValueTypeNames.Int4, Gf.Vec4i),
}

_LIST_SDF_TYPE_MAP = {
    bool: Sdf.ValueTypeNames.BoolArray,
    int: Sdf.ValueTypeNames.IntArray,
    float: Sdf.ValueTypeNames.FloatArray,
    str: Sdf.ValueTypeNames.StringArray,
}

_LIST_VT_MAP = {
    bool: Vt.BoolArray,
    int: Vt.IntArray,
    float: Vt.FloatArray,
    str: Vt.StringArray,
}


def _sdf_vector_type(val: tuple) -> tuple:
    """Return (Sdf type, Gf value) for a vector tuple, or (None, None) if unsupported."""
    non_none = [v for v in val if v is not None]
    if not non_none:
        return None, None
    entry = _VECTOR_TYPE_MAP.get((type(non_none[0]), len(val)))
    if entry is None:
        return None, None
    sdf_type, gf_cls = entry
    return sdf_type, gf_cls(*[v if v is not None else 0 for v in val])


def _sdf_type_and_value(val):
    """Return (Sdf value type, USD-safe value) for val, or (None, None) if unsupported."""
    if isinstance(val, tuple):
        return _sdf_vector_type(val)
    if isinstance(val, list):
        elem_type = type(val[0]) if val else str
        sdf_type = _LIST_SDF_TYPE_MAP.get(elem_type)
        vt_cls = _LIST_VT_MAP.get(elem_type)
        if sdf_type is None or vt_cls is None:
            return None, None
        return sdf_type, vt_cls(val)
    sdf_type = _SDF_TYPE_MAP.get(type(val))
    return sdf_type, val


def _store_row_expressions(attr: Usd.Attribute, list_group, template_name: str) -> None:
    """Keep a list group's row expressions with its array, one entry per row.

    The array holds resolved values, like any typed attr, so the text goes to customData
    under the same key chunk_utils.store_expression_text uses. A row with none gets "".
    """
    rows = [list_group.get_indexed_param(template_name, i) for i in range(list_group.size)]
    texts = [r.raw_value if r is not None and chunk_utils.is_expression_text(r.raw_value) else "" for r in rows]
    if any(texts):
        attr.SetCustomDataByKey(chunk_utils.EXPR_TEXT_KEY, Vt.StringArray(texts))
    else:
        chunk_utils.clear_expression_text(attr)


def _restore_row_expressions(attr: Usd.Attribute, list_group, template_name: str) -> None:
    """Put the row expressions _store_row_expressions saved back on their rows."""
    texts = attr.GetCustomDataByKey(chunk_utils.EXPR_TEXT_KEY)
    if isinstance(texts, str):  # a plain expression, not row text
        return
    for i, text in enumerate(texts or ()):
        if text and (row := list_group.get_indexed_param(template_name, i)):
            row.value = text


def sdf_type_for_param(param) -> Sdf.ValueTypeName | None:
    """The USD type an attribute backing this param should have, or None if unmappable.

    Typed from the param's DEFAULT rather than whatever it currently holds. A live value
    can be an unresolved expression string, or something that arrived over a wire, and an
    attribute's type is fixed for good once it is created -- so typing from the value in
    hand risks pinning a float param to String and failing every numeric write afterwards.
    The default is the one value that always reflects what the param actually is.

    Args:
        param: The param.

    Returns:
        The Sdf value type name, or None.
    """
    if param is None:
        return None
    try:
        if ChunkFlags.ParamType.COLOR_PICKER in param.evaluate_type():
            # A ColorPicker holds an (r, g, b) tuple; _SDF_TYPE_MAP is keyed by Python type
            # and has no tuple entry, so it needs saying explicitly.
            return Sdf.ValueTypeNames.Color3f
    except Exception:
        pass
    type_source = getattr(param, "default_value", None)
    if type_source is None:
        type_source = getattr(param, "raw_value", None)
    sdf_type, _ = _sdf_type_and_value(type_source)
    return sdf_type


def _push_driven_values(stage: Usd.Stage, param, layer: Sdf.Layer | None = None) -> None:
    """Write param's resolved value onto every real prim attr it drives.

    The chunk's own layer by default, where its execute() authors. Text is never written
    onto a non-String attr -- an expression that did not resolve stays text.

    A path connect_usd_attr recorded a variant scope for (show_attribute, from inside a
    variant) is the exception: `layer` is a plain layer and would author the value outside
    the variant, applying it to every variant equally, so that path re-enters the recorded
    selections (chunk_utils.author_in_variant_selections) instead.
    """
    paths = getattr(param, "_driven_attr_paths", None)
    if not paths:
        return
    value = param.value
    layer = layer or param.get_edit_context()
    # USD refuses to author into a muted layer; set_chunks_muted re-pushes on un-mute.
    if value is None or layer is None or stage.IsLayerMuted(layer.identifier):
        return
    driven_var_selections = getattr(param, "_driven_var_selections", {})
    for path in paths:
        # Inside the recorded variant before the lookup: a prim that only exists in that
        # variant is not on the stage while another one is selected.
        var_selections = driven_var_selections.get(path, ())
        with chunk_utils.edit_context_for(stage, var_selections, layer):
            attr = stage.GetAttributeAtPath(Sdf.Path(path))
            if not attr:
                continue
            if isinstance(value, str) and not chunk_utils.attr_holds_text(attr):
                continue
            if attr.Get(0) != value:
                attr.Set(value)


def _variant_scope_of(param, stage) -> tuple:
    """(var_selections, layer) for re-entering the variant `param` was bound in.

    The layer is the param's chunk layer, since a chunk's edits always go there -- or None
    outside a variant, which leaves edit_context_for a no-op there as before.
    """
    selections = getattr(param, "_var_selections", ())
    return selections, (chunk_utils.chunk_write_layer(stage, param) if selections else None)


def params_are_bound(chunk: core.ChunkBase) -> bool:
    """Whether any of this chunk's params still drives a real prim attribute.

    For a chunk that builds its params from the prim it authors -- a Light, a Camera,
    anything on show_attribute -- this is the difference between a cook that landed and one
    that only looked like it did. Cooking against a muted layer composes no prim, so
    show_attribute binds nothing; a chunk that caches "already cooked this type at this
    path" would then never rebuild the binding, and every value it holds drives nothing.

    Args:
        chunk: The chunk.

    Returns:
        True if some param has a driven attribute path.
    """
    return any(
        getattr(param, "_driven_attr_paths", None)
        for _, param in _iter_chunk_params(chunk)
    )


def push_chunk_values(stage: Usd.Stage, chunk: core.ChunkBase) -> None:
    """Re-author every value chunk's params drive, e.g. a Light's inputs.

    Needed after a chunk's layer comes back from being muted: USD refuses to author into a
    muted layer, so a value restored on load (or edited) while the chunk was gated off
    never landed, and the light would come back at its schema default.

    Args:
        stage: The stage to author on.
        chunk: The chunk whose driven values to write.
    """
    for _, param in _iter_chunk_params(chunk):
        _push_driven_values(stage, param)
    for _, vector in _iter_chunk_vectors(chunk):
        _push_driven_values(stage, vector)


def _adopt_cooked_inputs(chunk: core.ChunkBase) -> None:
    """chunk_cook.after_cook's hook: the stage sync of the moment reads what the cook changed."""
    ChunkStageSync()._adopt_cooked_inputs(chunk)


def _ancestor_paths(path: str):
    """Every ancestor of an absolute prim path, as a string, nearest first, "/" left out."""
    end = path.rfind("/")
    while end > 0:
        path = path[:end]
        yield path
        end = path.rfind("/")


class _PathSet(set):
    """A set of prim paths that also knows every ancestor of them.

    The stage sync asks, of each path a USD notice names, whether a chunk prim sits under
    it -- on every notice, of which building a network sends dozens per chunk. Scanning the
    set for a prefix made each notice cost the size of the scene; this answers by lookup.
    Only the mutators the sync uses are kept in step; the rest refuse rather than drift.
    """

    def __init__(self, paths: Iterable[str] = ()):
        super().__init__()
        self._ancestors: dict[str, int] = {}
        for path in paths:
            self.add(path)

    def add(self, path: str) -> None:
        if path in self:
            return
        super().add(path)
        for a in _ancestor_paths(path):
            self._ancestors[a] = self._ancestors.get(a, 0) + 1

    def discard(self, path: str) -> None:
        if path not in self:
            return
        super().discard(path)
        for a in _ancestor_paths(path):
            if (n := self._ancestors[a] - 1):
                self._ancestors[a] = n
            else:
                del self._ancestors[a]

    def remove(self, path: str) -> None:
        if path not in self:
            raise KeyError(path)
        self.discard(path)

    def update(self, *others: Iterable[str]) -> None:
        for other in others:
            for path in other:
                self.add(path)

    def clear(self) -> None:
        super().clear()
        self._ancestors.clear()

    def has_under(self, path: str) -> bool:
        """Whether a path in the set lies strictly below `path`."""
        return path in self._ancestors

    def _refuse(self, *args, **kwargs):
        raise TypeError("_PathSet keeps its ancestors only through add/discard/remove/update/clear")

    pop = difference_update = intersection_update = symmetric_difference_update = _refuse
    __ior__ = __iand__ = __isub__ = __ixor__ = _refuse


class _PathIndex(dict):
    """id(chunk) -> chunk prim path, that can also be read the other way round.

    By path (which chunk is at this prim) and by prefix (is any chunk under this prim) --
    the questions each USD notice asks, which a scan of every chunk answered at the cost of
    the scene's size per notice.
    """

    def __init__(self):
        super().__init__()
        self._paths = _PathSet()
        self._ids_at: dict[str, set[int]] = {}
        # id -> the chunk, for the entries set through track().
        self._refs: dict[int, weakref.ref] = {}

    def track(self, chunk: core.ChunkBase, path: str) -> None:
        """Register chunk at path, as index[id(chunk)] = path does, and remember the chunk."""
        self[id(chunk)] = path
        self._refs[id(chunk)] = weakref.ref(chunk)

    def chunk(self, key: int) -> core.ChunkBase | None:
        """The chunk registered under key through track(), if it is still that chunk."""
        ref = self._refs.get(key)
        c = ref() if ref is not None else None
        return c if c is not None and id(c) == key else None

    def _link(self, key: int, path: str) -> None:
        ids = self._ids_at.setdefault(path, set())
        ids.add(key)
        self._paths.add(path)

    def _unlink(self, key: int, path: str) -> None:
        ids = self._ids_at.get(path)
        if ids is None:
            return
        ids.discard(key)
        if not ids:
            del self._ids_at[path]
            self._paths.discard(path)

    def __setitem__(self, key: int, path: str) -> None:
        if key in self:
            self._unlink(key, dict.__getitem__(self, key))
        super().__setitem__(key, path)
        self._link(key, path)

    def __delitem__(self, key: int) -> None:
        self._unlink(key, dict.__getitem__(self, key))
        super().__delitem__(key)
        self._refs.pop(key, None)

    _MISSING = object()

    def pop(self, key: int, default=_MISSING):
        if key in self:
            path = super().pop(key)
            self._unlink(key, path)
            self._refs.pop(key, None)
            return path
        if default is _PathIndex._MISSING:
            raise KeyError(key)
        return default

    def clear(self) -> None:
        super().clear()
        self._paths.clear()
        self._ids_at.clear()
        self._refs.clear()

    def ids_at(self, path: str) -> set[int]:
        """The chunks registered at exactly this prim path."""
        return self._ids_at.get(path, set())

    def has_under(self, path: str) -> bool:
        """Whether a chunk is registered strictly below `path`."""
        return self._paths.has_under(path)

    def _refuse(self, *args, **kwargs):
        raise TypeError("_PathIndex keeps its reverse map only through item assignment, pop and clear")

    update = setdefault = popitem = _refuse
    __ior__ = _refuse


class ChunkStageSync(metaclass=RegistrySingleton):
    """Singleton that keeps a Chunk prim on the stage for every chunk in the scene.

    It authors a Chunk prim whenever a Python chunk is added to the scene, then wires each
    inputs: attribute to the matching param via connect_usd_attr. That keeps the stage live,
    so any USD tool can save and capture the current param state.
    """

    initialized: bool = False
    # True only while reload_for_stage tears down the previous file's chunks -- see
    # _on_chunk_deleted, which must not touch USD during that window.
    _reloading: bool = False

    # True while discarding_stage deletes the chunks of a stage about to be thrown away.
    _discarding: bool = False

    # True while reconcile_dag_dependencies runs off an ObjectsChanged notice, i.e. while
    # the DAG is being mirrored FROM the stage. Stops the mirror writing back: adding the
    # edge fires DEPENDENCY_ADDED, whose handler would author inputs:chunk and re-home edit
    # targets, both of which edit the stage and notify straight back here.
    _reconciling_exec: bool = False

    def initialize(self, stage: Usd.Stage, scene: chunk_scene.SceneSender) -> None:
        """Start syncing a stage and scene: listen for chunk and stage changes and load its chunks.

        Args:
            stage: The stage to author chunk prims on.
            scene: The scene whose chunks are synced.
        """
        self._stage = stage
        self._scene = scene
        self._reloading = False
        self._discarding = False
        self._loaded_stage = None
        self._loaded_prim_paths: set[str] = _PathSet()
        _output_children_known.clear()
        self._chunk_prim_paths: dict[int, str] = _PathIndex()  # id(chunk) → current prim path
        # What USD has said changed since the last scan for new chunk prims, used to scope
        # that scan -- see _on_objects_changed and _scan_for_new_chunks. Resyncs are kept
        # apart from info-only changes because they justify different amounts of looking.
        self._pending_resyncs: set[Sdf.Path] = set()
        self._pending_info_changes: set[Sdf.Path] = set()
        # inputs: properties a running cook changed, for _adopt_cooked_inputs once it ends.
        self._cooked_inputs: set[str] = set()
        # id(chunk) -> where its prim composes from (chunk_utils.composition_sources), for
        # _adopt_resynced_inputs to tell a recomposed chunk from one merely resynced.
        self._sources: dict[int, tuple] = {}
        # id(chunk) -> (chunk, input names) edited this solve, for _follow_composed.
        self._follow_pending: dict[int, tuple[core.ChunkBase, set[str]]] = {}
        chunk_cook.after_cook(_adopt_cooked_inputs)
        self._attr_changed_listener = NoticeConnection(
            self._on_attr_changed, ChunkNoticeAlias.ATTRIBUTE_CHANGED, globally=True
        )
        self._param_added_listener = NoticeConnection(
            self._on_param_added, ChunkNoticeAlias.PARAM_ADDED, globally=True
        )
        self._dependency_added_listener = NoticeConnection(
            self._on_dependency_added, ChunkNoticeAlias.DEPENDENCY_ADDED, globally=True
        )
        self._objects_changed_listener = NoticeConnection(
            self._on_objects_changed, ChunkNoticeAlias.OBJECT_CHANGED, globally=True
        )
        self._chunk_deleted_listener = NoticeConnection(
            self._on_chunk_deleted, ChunkNoticeAlias.CHUNK_DELETED, globally=True
        )
        self._state_changed_listener = NoticeConnection(
            self._on_state_changed, ChunkNoticeAlias.STATE_CHANGED, globally=True
        )
        self._contents_changed_listener = NoticeConnection(
            self._on_contents_changed, ChunkNoticeAlias.STAGE_CONTENTS_CHANGED, globally=True
        )
        self._chunk_loaded_listener = NoticeConnection(
            self._on_chunk_loaded, ChunkNoticeAlias.CHUNK_LOADED, globally=True
        )

        saved_targets: dict[str, str] = {}
        if stage:
            self._loaded_stage = stage
            # Before load_chunks: rebuilding an Edit Target whose layer doesn't exist yet
            # overwrites its saved selection in USD (see snapshot_edit_target_selections).
            saved_targets = snapshot_edit_target_selections(stage)
            load_chunks(stage, scene, self._loaded_prim_paths)
        self.initialized = True
        if stage:
            restore_edit_target_selections(stage, scene, saved_targets)
            # Same reason as the in-load pass above: opening a file must not rewrite it.
            reconcile_edit_targets(stage, scene, protect_root_layer=True)

    def _chunk_prim_edit_context(self, owner: core.ChunkBase | None = None):
        """An EditContext for authoring a *new* chunk-prim bookkeeping attr spec.

        That covers inputs:/outputs:/exec connections, node position, and the like.

        Targets chunk_utils.get_bookkeeping_layer(stage, owner) -- the Edit Target resolved
        for the chunk's exec-chain position (nearest_upstream_edit_layer) for a regular
        chunk, or always the session layer for a CONTEXT chunk (Tab containers, groups)
        regardless of what's active, since letting a CONTEXT chunk's own bookkeeping follow
        a target would split its prim across layers over its lifetime. owner=None (caller
        couldn't resolve one) also falls back to session via get_bookkeeping_layer.

        Only for *first-time* attr/prim creation, where "where should this new content
        go" is the right question. Updating an *existing* attr's value must instead
        target wherever its spec already lives (see chunk_core.py's update_usd_prop /
        data_changed) -- re-resolving the current target on every value change would
        scatter a single attr's opinions across layers as Edit Target moves on.
        """
        if not (self.initialized and self._stage):
            return contextlib.nullcontext()
        return Usd.EditContext(self._stage, chunk_utils.get_bookkeeping_layer(self._stage, owner))

    def clear_attr_connections(self, attr: Usd.Attribute) -> None:
        """Clear a chunk prim attr's connections in every layer that holds one.

        A connection may have been authored while a different Edit Target was active (or before
        any Edit Target chunk ran at all); clearing only the current edit layer would silently
        no-op, leaving that original opinion composed in as a stale, seemingly-undeletable
        connection.

        Args:
            attr: The attribute to clear.
        """
        if not (self.initialized and self._stage):
            return
        chunk_utils.clear_connections_everywhere(self._stage, attr)

    def _expr_connections(self, prim: Usd.Prim, raw: str) -> list:
        """Build Sdf.Path connection targets for an expression string, preferring outputs: over inputs:.

        The referenced node need not exist yet -- a connection is just a path, and USD
        resolves it once the target is authored, same as any other forward reference within
        a stage's own layers. GetAttribute on an invalid Usd.Prim raises rather than
        returning an invalid attribute, so the target prim's own validity has to be checked
        first: without this, a chunk load order where the referring chunk's expression gets
        resolved before the node it names has been defined crashed the whole load.
        """
        chunk_root = str(prim.GetParent().GetPath())
        targets = []
        for node, attr_name in chunk_utils.parse_expression_attrs(raw):
            target_prim = self._stage.GetPrimAtPath(Sdf.Path(f"{chunk_root}/{node}"))
            kind = ("outputs" if target_prim.IsValid()
                    and target_prim.GetAttribute(f"outputs:{attr_name}").IsValid() else "inputs")
            targets.append(Sdf.Path(f"{chunk_root}/{node}.{kind}:{attr_name}"))
        return targets

    def _drive_from_storage(self, param, real_attr: Usd.Attribute) -> None:
        """Turn the real prim attr param was bound to into one its storage attr drives.

        The real attr takes the resolved value -- expression text is only storable on a
        String attr, and a Light's float input handed `if(...)` raises -- in the chunk's
        own layer, unless that layer is muted (re-pushed on un-mute).

        real_attr is param's ORIGINAL direct binding (from_usd_attr, e.g. show_attribute)
        being promoted into a driven connection -- param._var_selections, if any, is the
        variant this same attribute was resolved under, and has to carry over so later
        writes land back inside that variant rather than on the plain composed path.
        """
        param.connect_usd_attr(real_attr, var_selections=getattr(param, "_var_selections", ()))
        self._clear_driven_attr_value(real_attr)
        _push_driven_values(self._stage, param)

    def _clear_driven_attr_value(self, driven_attr: Usd.Attribute) -> None:
        """Remove any authored default value from the driven attr in session/anonymous layers.

        After a param is connected to a chunk-prim storage attr, the light prim attr
        should carry only the connection spec -- no value opinion -- so that the
        connection is the sole driver (matching how inputs:radius is authored).
        """
        if not self._stage:
            return
        path = driven_attr.GetPath()
        # The default may have been authored in whatever layer was the edit target at the
        # time -- clear it wherever it exists across the stack, not just one tracked layer.
        for layer in self._stage.GetLayerStack(includeSessionLayers=True):
            spec = layer.GetAttributeAtPath(path)
            if spec and spec.HasDefaultValue():
                spec.ClearDefaultValue()

    def _on_contents_changed(self, notice, sender):
        # The edit-layer memo is not invalidated here: every StageContentsChanged follows
        # an ObjectsChanged for the same change (test_objects_changed_precedes_stage_
        # contents_changed), and _on_objects_changed decides from that one whether the
        # change can move an answer -- including a mute, which it recognises by the layer
        # stack having changed rather than by paths, since USD reports one with none.
        # Not while the stage is being thrown away: a chunk deleted with it can still edit
        # it on the way out (a Mute Layer gives back its mutes), and the scan that followed
        # found the prims of the chunks just deleted and built them again.
        if chunk_notice._usd_write_active or not self._scene or self._discarding:
            return
        stage = notice.GetStage()
        if not stage or self._is_foreign_stage(stage):
            return
        scene_stage = self._scene.get_stage()
        if not scene_stage or stage.GetRootLayer().identifier != scene_stage.GetRootLayer().identifier:
            return
        self._stage = stage
        self._scan_for_new_chunks(stage)

    def _scan_for_new_chunks(self, stage: Usd.Stage) -> None:
        """Turn any chunk prim that has appeared since the last scan into a chunk.

        Scoped to the subtrees ObjectsChanged reported resynced, rather than the whole
        stage. A prim cannot appear outside a resynced subtree, so this looks everywhere it
        needs to and nowhere else -- which matters twice over: the scan runs on every stage
        change, so walking the whole stage made building a graph quadratic in the number of
        chunks, and in a real scene it also walked every piece of geometry on the stage to
        find out that nothing had happened.

        A resync is a structural change, so a prim that was not there before can be
        anywhere beneath one -- those get walked. An info-only change cannot create a prim,
        but it can still make an existing one loadable, which is not a hypothetical: a
        chunk prim becomes loadable when its info:chunkType gets a value, and setting a
        value is an info-only change. So those contribute the one prim they name, and
        deliberately not its subtree -- otherwise a metadata change on a big prim would drag
        its whole subtree through here.

        Having heard nothing at all is the one case with no basis to narrow, and walks the
        stage. State is cleared only here, where the scan actually happens: the callers bail
        out ahead of this on a suppressed write, and whatever they saw still has to be
        looked at on the next real one.
        """
        resyncs, info_changes = self._pending_resyncs, self._pending_info_changes
        self._pending_resyncs = set()
        self._pending_info_changes = set()
        prims = None
        if resyncs or info_changes:
            prims = _iter_scoped_prims(stage, resyncs, info_changes)
        load_chunks(stage, self._scene, self._loaded_prim_paths, prims=prims)

    @contextlib.contextmanager
    def discarding_stage(self):
        """Delete chunks inside this block without touching the stage they are on.

        Deleting a chunk removes its prim, re-heals the exec chain around it and reconciles
        Edit Targets across the scene -- right for a chunk deleted from a stage that lives
        on, and wasted work, once per chunk, for a stage that is about to be thrown away
        whole. File > Open already tears down that way (_reloading); this is the same for
        any caller that owns the stage it is discarding, such as a script's Scene closing.
        """
        was_reloading, was_discarding = self._reloading, self._discarding
        self._reloading = self._discarding = True
        try:
            yield
        finally:
            self._reloading, self._discarding = was_reloading, was_discarding

    def _on_chunk_deleted(self, notice, sender: core.ChunkBase) -> None:
        if not self._stage or not isinstance(sender, core.ChunkBase):
            return
        # Keyed by id, which a later chunk can be handed again.
        self._sources.pop(id(sender), None)
        if self._reloading:
            # File > Open: these chunks belong to the file being *replaced*, but self._stage
            # is already the newly opened one. Everything below addresses prims by path, and
            # the outgoing graph's paths routinely collide with the incoming file's (both
            # sides came from the same tab, e.g. /Tab1/Prim) -- so the teardown would delete
            # the new file's own prim specs and heal_exec_chain_on_delete would rewire its
            # exec chain, all before load_chunks ever reads it. Nothing to unauthor either
            # way: the outgoing stage is being discarded whole.
            self._loaded_prim_paths.discard(self._chunk_prim_paths.get(id(sender), ""))
            self._chunk_prim_paths.pop(id(sender), None)
            return
        if sender.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            if not getattr(sender, "prim_path", None):
                return  # tab context -- no USD prim to remove
            sdf_path = Sdf.Path(sender.prim_path)
            chunk_root = str(sdf_path.GetParentPath())
            self._delete_nested_contexts(sdf_path)
        else:
            # request_delete clears the owner before firing CHUNK_DELETED, so get_context()
            # (and thus _resolve_chunk_root) is None here and would resolve to the wrong root
            # (/chunks instead of e.g. /Tab1). Use the path recorded when the prim was authored;
            # only recompute if we never tracked it.
            stored = self._chunk_prim_paths.get(id(sender))
            if stored:
                sdf_path = Sdf.Path(stored)
                chunk_root = str(sdf_path.GetParentPath())
            else:
                chunk_root = _resolve_chunk_root(sender)
                sdf_path = Sdf.Path(f"{chunk_root}/{sender.name}")
        prim_path_str = str(sdf_path)
        self._loaded_prim_paths.discard(prim_path_str)
        self._chunk_prim_paths.pop(id(sender), None)

        # Read who was downstream before healing rewires them off this chunk: nothing after
        # this point can tell, and they are the only chunks whose Edit Target can change.
        successor_paths = _exec_successor_paths(self._stage, prim_path_str)

        # Heal the exec chain while the prim (and its inputs:chunk predecessor) still exists:
        # deleting a middle node A -> X -> B must leave A -> B, not a broken chain.
        heal_exec_chain_on_delete(self._stage, prim_path_str)

        # The nameChildren edits below are raw Sdf writes that fire StageContentsChanged
        # like any other stage edit. Without this guard, deleting a context with several
        # children re-enters _on_contents_changed -> load_chunks after every single child
        # removal (mid-cascade, before the remaining siblings are deleted), causing a full
        # stage rescan per child instead of once for the whole delete.
        prev_w = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            # Owned layers only: a chunk composed in from a sequence file (sublayered, or
            # hosted by reference) is not this file's to delete, and pulling its spec out
            # of the sequence layer gutted the sequence in the layer cache. What the shot
            # can do is what USD lets it: drop its own overrides and deactivate the prim.
            prim_before = self._stage.GetPrimAtPath(sdf_path)
            composed_in = bool(prim_before and prim_before.IsValid()
                               and chunk_utils.is_composed_in(prim_before))
            for layer in self._stage.GetLayerStack(includeSessionLayers=True):
                if chunk_utils.is_foreign_layer(layer, self._stage):
                    continue
                parent = layer.GetPrimAtPath(sdf_path.GetParentPath())
                if parent and sdf_path.name in parent.nameChildren:
                    del parent.nameChildren[sdf_path.name]
            if composed_in:
                remaining = self._stage.GetPrimAtPath(sdf_path)
                if remaining and remaining.IsValid():
                    with Usd.EditContext(self._stage, chunk_utils.host_edit_layer(self._stage)):
                        remaining.SetActive(False)

            # Tidy up the chunks root once its last chunk goes -- an empty /chunks prim is
            # bookkeeping nobody wants left behind. ONLY that root, though: a chunk inside a
            # chunkgroup has the GROUP as its root, and an empty group is still a group.
            # Deleting a wired group deletes its contents first; if the last of those took
            # the group's prim with it, heal_exec_chain_on_delete would bail on its IsValid()
            # guard and leave a downstream view connected to a prim that no longer exists.
            root_sdf = Sdf.Path(chunk_root)
            root_prim = self._stage.GetPrimAtPath(root_sdf)
            if chunk_root == _CHUNKS_ROOT and root_prim.IsValid() and not any(
                child.GetTypeName() == _CHUNK_PRIM_TYPENAME for child in root_prim.GetChildren()
            ):
                for layer in self._stage.GetLayerStack(includeSessionLayers=True):
                    root_parent = layer.GetPrimAtPath(root_sdf.GetParentPath())
                    if root_parent and root_sdf.name in root_parent.nameChildren:
                        del root_parent.nameChildren[root_sdf.name]
        finally:
            chunk_notice._usd_write_active = prev_w

        # Deleting an Edit Target chunk changes the target that now applies to everything
        # that was downstream of it (heal_exec_chain_on_delete has just rewired the chain
        # around it) -- re-home those chunks to the target that survives at their position.
        # Only they can have changed, so the pass is scoped to them rather than run over the
        # whole scene: a delete used to re-home and re-cook every chunk in the graph.
        # protect_root_layer, as on load: a chunk opened from a file keeps its bookkeeping in
        # that file's root layer, and migrating deletes the original spec -- the first delete
        # after opening would empty the file's own layer, which Sdf then hands back to the
        # next open of the same path.
        successors = [
            c for c in core.all_chunks(self._scene.get_chunks())
            if get_chunk_prim_path(c) in successor_paths
        ]
        if not successors:
            return
        # The delete itself is done. A cook downstream failing here (say on an expression
        # that referenced the chunk just deleted) must not escape and abort the rest of the
        # CHUNK_DELETED dispatch, which would leave the chunk half torn down.
        try:
            reconcile_edit_targets(
                self._stage, self._scene, roots=successors, protect_root_layer=True
            )
        except Exception:  # noqa: BLE001
            logger.exception("Re-homing chunks after deleting %s failed", sender.name)

    def _delete_nested_contexts(self, group_path: Sdf.Path) -> None:
        """Delete the chunkgroups nested inside the group being deleted.

        ChunkContainerBase.request_delete cascades through get_my_params(), which reaches a
        plain chunk created inside a group -- it really is a param of it. A nested
        CHUNKGROUP is not: it nests by prim path, as a context in its own right, so the
        cascade never reached it and neither did anything downstream of CHUNK_DELETED.

        Without that, removing the group's prim takes the whole subtree's prim specs with
        it while the inner chunks stay alive in Python, still owning their layers and still
        sublayered into the graph: their content stays authored and composed, with nothing
        left on the graph to remove it.

        Deleting each nested group is enough to reach the leaves, since its own params
        cascade normally from there. Snapshot first: request_delete mutates the scene's
        chunk list as it goes. Descendants before the parent's prim specs are removed,
        while their paths still resolve.
        """
        prefix = str(group_path) + "/"
        for chunk in list(self._scene.get_chunks()):
            if chunk is None or getattr(chunk, "_deleting", False):
                continue
            if not chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                # A plain chunk under this prefix is a param of one of these groups (or of
                # the group being deleted) and is already covered by the param cascade.
                continue
            if get_chunk_prim_path(chunk).startswith(prefix):
                chunk.request_delete()

    def _on_state_changed(self, notice, sender: core.ChunkBase) -> None:
        if not self._stage or not isinstance(sender, core.ChunkBase):
            return
        prim_path = self._chunk_prim_paths.get(id(sender))
        if not prim_path:
            chunk_root = _resolve_chunk_root(sender)
            prim_path = f"{chunk_root}/{sender.name}"
        disabled = sender.has_state(ChunkFlags.State.DISABLED)
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if prim.IsValid():
            self._write_expansion_state(sender, prim)
        if not prim.IsValid():
            # A chunk under a deactivated ancestor is not in the composed scene at all, so
            # there is no prim to ask -- and a group re-enables its children before its own
            # prim comes back, so this is exactly when they are told to wake up. Returning
            # here left their authored active=false in place, and the group came back with
            # its contents still switched off. Clear it on the spec directly instead.
            if not disabled:
                prev_w = chunk_notice._usd_write_active
                chunk_notice._usd_write_active = True
                try:
                    for layer in self._stage.GetLayerStack(includeSessionLayers=True):
                        spec = layer.GetPrimAtPath(Sdf.Path(prim_path))
                        if spec is not None and spec.HasInfo("active"):
                            spec.ClearInfo("active")
                finally:
                    chunk_notice._usd_write_active = prev_w
            return
        # Compare against what is authored ON THIS PRIM, not the composed value, and for the
        # same reason the mirror does: a chunk inside a disabled group composes inactive
        # without anything being authored on it. Reading the composed value here left an
        # enabled chunk with a stale authored active=false -- the group came back but its
        # contents stayed switched off, because IsActive() already agreed with the new state
        # and the write was skipped.
        authored_active = prim.GetMetadata("active") if prim.HasAuthoredActive() else True
        if authored_active == disabled:
            # Mark this as our own write. Without the guard the resulting ObjectsChanged is
            # read back as if a user had deactivated the prim on the stage, and the mirror in
            # _on_objects_changed answers by rewriting the chunk's state -- disabling a
            # chunkgroup would clear the group's OWN disabled flag. Re-enabling would then be
            # a no-op: the group would never see a DISABLED->enabled transition, so it would
            # never tell its children to come back.
            prev_w = chunk_notice._usd_write_active
            chunk_notice._usd_write_active = True
            try:
                prim.SetActive(not disabled)
            finally:
                chunk_notice._usd_write_active = prev_w

    def _write_expansion_state(self, chunk: core.ChunkBase, prim: Usd.Prim) -> None:
        """Record whether this node is collapsed, so a reopened scene looks as it was left.

        USD already has the attribute for it: UsdUINodeGraphNodeAPI's expansionState, the
        same schema the node's position is authored through, so a graph collapsed here reads
        as collapsed to anything else that speaks that schema rather than only to us.

        Authored only when closed. Open is the state a chunk is born in, so writing it would
        put a line on every chunk prim in every scene to say nothing -- the same reasoning
        chunk:pinned follows.
        """
        collapsed = chunk.has_state(ChunkFlags.State.COLLAPSED)
        attr = prim.GetAttribute(UsdUI.Tokens.uiNodegraphNodeExpansionState)
        current = attr.Get() if (attr and attr.IsValid()) else None
        if collapsed == (current == UsdUI.Tokens.closed):
            return

        prev_w = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            if not collapsed:
                # Back to the default: drop the opinion rather than author "open" over it,
                # wherever it was written.
                for layer in self._stage.GetLayerStack(includeSessionLayers=True):
                    spec = layer.GetPrimAtPath(prim.GetPath())
                    name = UsdUI.Tokens.uiNodegraphNodeExpansionState
                    if spec and name in spec.properties:
                        del spec.properties[name]
                return
            home = chunk_utils.get_attr_home_layer(attr) if (attr and attr.IsValid()) else None
            with Usd.EditContext(self._stage, home or chunk_utils.get_bookkeeping_layer(self._stage, chunk)):
                UsdUI.NodeGraphNodeAPI.Apply(prim).CreateExpansionStateAttr(UsdUI.Tokens.closed)
        finally:
            chunk_notice._usd_write_active = prev_w

    def _is_foreign_stage(self, stage: Usd.Stage | None) -> bool:
        """Whether a notice's stage is known to be some other stage than the scene's.

        Only a definite no: without a scene, a scene stage or a notice stage, nothing is
        known, and the notice is handled as it always was. Compared by root layer, as the
        rest of the sync does, since the Python wrapper of one stage is not unique -- except
        for a cook stage (skip_cook), which shares the scene's root layer and is not it. Its
        changes are the scene's too, and arrive from the scene's stage as well.
        """
        if stage is None or not self._scene:
            return False
        if skip_cook.is_cook_stage(stage) or any(stage == s for s in _flatten_stages):
            return True
        scene_stage = self._scene.get_stage()
        if not scene_stage:
            return False
        return stage.GetRootLayer().identifier != scene_stage.GetRootLayer().identifier

    def _on_objects_changed(self, notice: Usd.Notice.ObjectsChanged, sender):
        # The listener is global, so it hears every stage in the process -- including ones
        # nothing here owns. Opening a .mtlx builds its material on a private stage of the
        # MaterialX file format plugin's, and that alone is ~700 change rounds per material:
        # each one dropped the edit layer memo (a notice from another stage looks like a
        # replaced stage to edit_layer_memo_affected) and queued that stage's paths as
        # resyncs of ours. Nothing below is about any stage but the scene's.
        if self._is_foreign_stage(notice.GetStage()):
            return
        # Before the guards, since a prim appearing or an inputs:chunk connection changing
        # is exactly what the memo is keyed against, and those arrive here regardless of
        # which flag is set. Only when the change can actually move an answer, though: a
        # chunk cooking writes hundreds of driven-prim changes that cannot, and each used
        # to cost every chunk a fresh walk of the exec chain above it.
        if edit_layer_memo_affected(notice, has_chunk_under=self._has_chunk_under):
            invalidate_edit_layer_memo()
        # Before the guards too: a chunk prim made during a suppressed write is still made.
        self._forget_output_children(notice)
        # Same reasoning, and the only place the resynced paths are visible at all: the scan
        # for new chunk prims runs off StageContentsChanged, which carries no paths. USD
        # sends this notice first for a given change (locked in by
        # test_objects_changed_precedes_stage_contents_changed), so what is recorded here is
        # what that scan then scopes itself by. A write suppressed by the guards below still
        # has to be recorded -- suppression stops us reacting now, not the prims existing.
        self._pending_resyncs.update(notice.GetResyncedPaths())
        self._pending_info_changes.update(notice.GetChangedInfoOnlyPaths())
        if chunk_notice._executing_chunk is not None and chunk_notice._usd_write_active:
            # A cook writing onto a chunk prim's inputs -- an Override Chunk Prim's `over`,
            # or its layer being cleared -- changes that chunk's params, and the guard below
            # is up for the whole cook. Kept for after it (_adopt_cooked_inputs), read from
            # the composed stage then rather than replayed, so a write half done is never
            # seen. Names only: which prims are chunk prims is sorted out then.
            self._cooked_inputs.update(
                str(p) for paths in (notice.GetResyncedPaths(), notice.GetChangedInfoOnlyPaths())
                for p in paths if p.IsPropertyPath() and p.name.startswith(_INPUTS_PREFIX)
            )
        if chunk_notice._usd_write_active or self._reconciling_exec or not self._scene:
            return
        # Resolve the stage from the notice itself, like _on_contents_changed does, rather
        # than trusting the raw `sender` argument -- RegisterGlobally's sender has been
        # observed to arrive as None here (unlike in isolation), crashing on
        # sender.GetRootLayer() below. notice.GetStage() is the same battle-tested fallback
        # _on_contents_changed already uses for exactly this reason.
        stage = notice.GetStage()
        if not stage:
            return
        scene_stage = self._scene.get_stage()
        if not scene_stage or stage.GetRootLayer().identifier != scene_stage.GetRootLayer().identifier:
            return
        self._stage = stage
        changed = {str(p) for p in notice.GetChangedInfoOnlyPaths()}
        # Include resynced paths for editors (e.g. usdview) that author active via the layer
        all_changed = changed | {str(p) for p in notice.GetResyncedPaths()}
        # NOT extended with paths from notices that the guard above suppressed, though the
        # temptation is strong: a change arriving mid-write is genuinely lost, which is a
        # candidate for "undo moved the prim every step but the sliders followed only about
        # half the time". Replaying them is unsound. A suppressed notice means WE were
        # writing, and a multi-component write leaves USD transiently holding partial
        # values -- replaying such a path afterwards pushed a stale (1, 0, 0) over a param
        # that already correctly held (1, 2, 3). That is precisely what the guard is for.
        # Fixing the dropped-undo case needs the drain to happen where USD is known
        # consistent (as the write block closes), not on the next unrelated notice.
        if not all_changed:
            return
        self._reconcile_changed_exec_inputs(all_changed, notice)
        self._mirror_changed_params(stage, all_changed)
        self._adopt_changed_inputs(all_changed)
        self._adopt_resynced_inputs(notice.GetResyncedPaths())
        self._prune_vanished(stage, {str(p) for p in notice.GetResyncedPaths()})
        for chunk in self._chunks_at(all_changed):
            chunk_prim_path = self._chunk_prim_paths.get(id(chunk))
            prim = stage.GetPrimAtPath(Sdf.Path(chunk_prim_path))
            if not prim.IsValid():
                continue
            # This chunk's OWN activation, not the composed one. Deactivating a chunkgroup
            # deactivates its descendants too, and that is inherited -- nothing is authored
            # on the children. Reading prim.IsActive() here would make every inner chunk
            # latch a DISABLED state it never had, and re-enabling the group would then
            # reactivate only the group's prim: each inner chunk would write its own
            # active=false straight back from that latched state.
            #
            # A chunk that really is disabled has active=false authored on its own prim --
            # that is what _on_state_changed writes -- so this still sees a genuine deactivate,
            # including one done externally on the stage.
            is_active = prim.GetMetadata("active") if prim.HasAuthoredActive() else True
            is_disabled = chunk.has_state(ChunkFlags.State.DISABLED)
            if not is_active and not is_disabled:
                chunk.set_state(ChunkFlags.State.DISABLED)
            elif is_active and is_disabled:
                chunk.unset_state(ChunkFlags.State.DISABLED)

    @staticmethod
    def _forget_output_children(notice) -> None:
        """Drop what _output_children remembers of any child this change round touched.

        A resync is how USD reports a prim made, deleted or recomposed, and an info change
        on "/" or none at all is a layer stack change -- after which nothing is trusted.
        """
        known = _output_children_known
        # A chunk's own layer or a graph layer moving in or out of the stack: they hold
        # cook output, never a chunk prim, so no child changes what it is.
        if not known or chunk_notice._internal_layer_write_active:
            return
        resynced = notice.GetResyncedPaths()
        info = notice.GetChangedInfoOnlyPaths()
        if not resynced and not info:
            known.clear()
            return
        for path in (*resynced, *info):
            if path == Sdf.Path.absoluteRootPath:
                known.clear()
                return
            if path.IsPropertyPath() and path.name == _CHUNK_TYPE_ATTR:
                prim_path = path.GetPrimPath()
                known.get(str(prim_path.GetParentPath()), {}).pop(prim_path.name, None)
        for path in resynced:
            if path.IsPropertyPath():
                continue
            known.get(str(path.GetParentPath()), {}).pop(path.name, None)
            prefix = str(path) + "/"
            for key in [k for k in known if k == str(path) or k.startswith(prefix)]:
                del known[key]

    def _has_chunk_under(self, path: str) -> bool:
        """Whether a chunk prim the sync knows of lies strictly below `path`."""
        return self._loaded_prim_paths.has_under(path) or self._chunk_prim_paths.has_under(path)

    def _chunk_order(self) -> dict[int, tuple[int, core.ChunkBase]]:
        """id(chunk) -> (its place in get_chunks(), the chunk), kept while the scene is."""
        chunks = self._scene.get_chunks()
        cached = getattr(self, "_chunk_order_cache", None)
        if cached is None or cached[0] is not chunks:
            cached = (chunks, {id(c): (i, c) for i, c in enumerate(chunks)})
            self._chunk_order_cache = cached
        return cached[1]

    def _in_scene_order(self, ids: Iterable[int]) -> list[core.ChunkBase]:
        """The scene's chunks among `ids`, in get_chunks() order."""
        ids = set(ids)
        if not ids:
            return []
        # One chunk -- what nearly every notice asks about -- has no order to put it in, and
        # the order is the expensive part: it is rebuilt from a walk of the whole scene after
        # every chunk added, so building a network walked the scene once per chunk.
        if len(ids) == 1:
            index = getattr(self, "_chunk_prim_paths", None)
            c = index.chunk(next(iter(ids))) if isinstance(index, _PathIndex) else None
            if c is not None and self._scene.surely_holds(c):
                return [c]
        order = self._chunk_order()
        found = sorted(order[i] for i in ids if i in order)
        return [c for _, c in found]

    def _chunks_at(self, paths: Iterable[str]) -> list[core.ChunkBase]:
        """The scene's chunks whose prim is one of `paths`, found by lookup, not by a scan.

        Each USD notice asks this, and building a network sends dozens per chunk: walking
        every chunk to compare its path made each notice cost the size of the scene.
        """
        index = self._chunk_prim_paths
        return self._in_scene_order(i for p in paths for i in index.ids_at(p))

    def _chunks_under(self, roots: Iterable[str]) -> list[core.ChunkBase]:
        """The scene's chunks at or below any of `roots` -- the walk only when one has any."""
        index = self._chunk_prim_paths
        ids = set()
        for root in roots:
            ids.update(index.ids_at(root))
            if index.has_under(root):
                prefix = root.rstrip("/") + "/"
                ids.update(i for i, p in index.items() if p.startswith(prefix))
        return self._in_scene_order(ids)

    def _prune_vanished(self, stage: Usd.Stage, resynced: set[str]) -> None:
        """Tear down the chunk objects whose prims composition no longer has.

        A chunk that arrived by composition can leave the same way: the sequence file drops
        it and the layer reloads, or the host's arc is cleared. Nothing authored it here,
        so nothing here deletes it -- its prim just stops being on the stage. Left alone,
        the object would linger, muted, until its next cook authored a fresh prim of its own
        at whatever path it then resolved to.

        Only a chunk inside a hosted subtree -- under a prim carrying a reference or
        payload arc -- is a candidate: a locally authored chunk never loses its prim
        except through _on_chunk_deleted, and its path can be registered an instant
        before its prim is defined, which is not "vanished". Only under a resynced path,
        and only a prim that is genuinely gone: one pruned because an ancestor was
        deactivated (a disabled host) is still there in every way that matters and comes
        back with the ancestor, objects intact. Deepest first, so a nested group goes
        before its parent; under _reloading, so nothing is unauthored.
        """
        if not resynced or not self._scene:
            return
        gone = []
        for chunk in self._chunks_under(resynced):
            path = self._chunk_prim_paths.get(id(chunk))
            if stage.GetPrimAtPath(Sdf.Path(path)).IsValid():
                continue
            hosted = False
            ancestor = Sdf.Path(path).GetParentPath()
            while not ancestor.isEmpty and ancestor != Sdf.Path.absoluteRootPath:
                prim = stage.GetPrimAtPath(ancestor)
                if prim.IsValid():
                    if not prim.IsActive():
                        hosted = False
                        break
                    if _hosts_by_arc(prim):
                        hosted = True
                        break
                ancestor = ancestor.GetParentPath()
            if hosted:
                gone.append((path, chunk))
        if not gone:
            return
        gone.sort(key=lambda item: item[0].count("/"), reverse=True)
        was_reloading = self._reloading
        self._reloading = True
        try:
            for path, chunk in gone:
                logger.info("chunk prim %s left the stage; dropping its chunk", path)
                chunk.request_delete()
        finally:
            self._reloading = was_reloading

    def _mirror_changed_params(self, stage: Usd.Stage, all_changed: set[str]) -> None:
        """Pull each changed USD property's value into the params bound to it.

        Straight from the notice's paths to their params (core.params_bound_to), not a walk
        of every param of every chunk comparing paths -- the notice already says exactly
        which properties changed, and nearly every round changes none that any param is
        bound to.

        all_changed, not just the info-only set. An ordinary edit -- typing in a field, a
        gizmo writing a param -- arrives as an info-only change, but undo restores layer
        content wholesale and USD reports THAT as a resync. Reading only the info-only set
        meant precisely the edits a user cannot make by hand (undo, redo, a layer reload)
        were the ones the mirror ignored: Ctrl+Z moved the prim back while the chunk's
        sliders kept the undone value, and only caught up when the gizmo was next touched.
        Confirmed live -- a real Ctrl+Z reports `<chunk>.inputs:translate_grp:tz` as
        resynced, with nothing in the info-only set.

        Only params of this scene's chunks, and never a list group's rows. Rows store as one array on the group's template attr, so a row bound to a
        path of its own is not something this mirror knows how to read back. The scene's
        chunk set is built only once a bound param is actually hit.
        """
        live: dict | None = None
        for data_path in all_changed:
            for param in core.params_bound_to(data_path):
                if not isinstance(param, (core.ChunkParamBase, core.ChunkVectorBase)):
                    continue
                if live is None:
                    live = self._chunk_order()
                chunk = param.get_chunk()
                if chunk is None or id(chunk) not in live or _under_list_group(param, chunk):
                    continue
                attr = stage.GetAttributeAtPath(Sdf.Path(data_path))
                if not (attr and attr.IsValid()):
                    continue
                val = attr.Get(0)
                # The expression guard: writing the resolved value over an expression
                # replaces a live link with a frozen number, and the param stops tracking
                # whatever it was pointed at.
                raw = getattr(param, "raw_value", None)
                if (val is not None and not _values_agree(val, param.value)
                        and not (isinstance(raw, str) and chunk_utils.has_expression(raw))):
                    param.value = val

    def _adopt_changed_inputs(self, paths: Iterable[str], skip: core.ChunkBase | None = None) -> None:
        """Bring each chunk whose prim's inputs: changed in line with what the prim composes.

        By name, where _mirror_changed_params goes by binding: a param still at its default
        has no attribute, so nothing is bound to one that another layer then authors -- and
        when the layer holding it goes, the param goes back to its default, which the bound
        mirror (reading only values that exist) never did.

        Args:
            paths: Changed property paths; only <chunk prim>.inputs:* ones count.
            skip: A chunk to leave alone -- the one whose cook made the changes.
        """
        by_prim: dict[str, list[str]] = {}
        for path in paths:
            prim_path, sep, name = str(path).partition(".")
            if sep and name.startswith(_INPUTS_PREFIX):
                by_prim.setdefault(prim_path, []).append(name[len(_INPUTS_PREFIX):])
        if not by_prim or not self._scene:
            return
        for chunk in self._chunks_at(by_prim):
            names = by_prim.get(self._chunk_prim_paths.get(id(chunk), ""))
            if names and chunk is not skip:
                _adopt_inputs(chunk, names)

    def _adopt_resynced_inputs(self, resynced: Iterable[Sdf.Path]) -> None:
        """A chunk whose prim now composes from different sources re-reads all its inputs.

        A resync recomposes a prim from scratch and reports the prim, not which inputs:
        changed. H1 under a house whose version went v1 -> v2 is the same prim path with
        another file's values behind it. Which chunks that applies to is read from USD: the
        fingerprint of each one's prim index (chunk_utils.composition_sources) against the
        one recorded before. Making, pasting or grouping chunks resyncs too, but leaves the
        sources of the chunks already there alone, so their pending edits are not overwritten.

        Not for the pseudo-root: every sublayer insert resyncs it, which is every cook layer
        placed, and fingerprinting every chunk on each would be the whole cost of a cook.
        """
        roots = [p for p in resynced if p.IsPrimPath() and p != Sdf.Path.absoluteRootPath]
        if not roots or not self._scene or not self._stage:
            return
        for chunk in self._chunks_under(str(root) for root in roots):
            path = self._chunk_prim_paths.get(id(chunk))
            sources = chunk_utils.composition_sources(self._stage.GetPrimAtPath(path))
            before = self._sources.get(id(chunk))
            self._sources[id(chunk)] = sources
            if before is not None and before != sources:
                _adopt_inputs(chunk)

    def record_sources(self, chunks: Iterable[core.ChunkBase], only_new: bool = False) -> None:
        """Note where each chunk's prim composes from, for _adopt_resynced_inputs.

        Args:
            chunks: The chunks to note.
            only_new: Skip the chunks already noted.
        """
        if not self._stage:
            return
        for chunk in chunks:
            if only_new and id(chunk) in self._sources:
                continue
            if path := self._chunk_prim_paths.get(id(chunk)):
                self._sources[id(chunk)] = chunk_utils.composition_sources(
                    self._stage.GetPrimAtPath(path))

    def _follow_composed(self, chunk: core.ChunkBase, param) -> None:
        """Once the solve has written everything, set param to what its prim composes.

        Its edit lands in its home (chunk_utils.get_attr_home_layer), and usually that is
        what composes. Not when a stronger layer holds the input -- an Override Chunk Prim's,
        a department's above this one (chunk_stage.param_held_by): the param would keep the
        value it was given while the prim says otherwise, and the chunk would cook with a
        value USD does not have. After the solve, not now: a bound param writes from its own
        data_changed, in no set order with this listener, and reading before it had written
        would undo the edit instead.

        Args:
            chunk: The param's chunk.
            param: The param (or vector) just edited.
        """
        owner = param.get_owner()
        target = owner if isinstance(owner, core.ChunkVectorBase) else param
        names = _param_object_path(chunk, target)
        if not names:
            return
        pending = self._follow_pending.setdefault(id(chunk), (chunk, set()))[1]
        pending.add(":".join(names))

        def follow():
            entry = self._follow_pending.pop(id(chunk), None)
            stage = chunk.get_scene_stage()
            if entry is None or stage is None:
                return
            # Only what a stronger layer holds. Anything else is the param's own value, on
            # its way to USD: a chunk prim's writes can be debounced, so the attribute may
            # not be there yet, and reading it then would put the param back to its default.
            held = [n for n in entry[1] if _input_held_by(chunk, stage, n) is not None]
            if held:
                _adopt_inputs(chunk, held)

        # Outside a solve nothing says the write is done, so nothing is read.
        if not chunk_cook.defer_to_batch_end(("follow_composed", id(chunk)), follow):
            self._follow_pending.pop(id(chunk), None)

    def _adopt_cooked_inputs(self, chunk: core.ChunkBase) -> None:
        """After chunk's cook: what it changed on other chunk prims' inputs: (chunk_cook.after_cook)."""
        # Not from a cook inside another one: the outer cook may be part way through its
        # own writes, and its end reads them all.
        if not getattr(self, "_cooked_inputs", None) or chunk_notice._executing_chunk is not None:
            return
        paths, self._cooked_inputs = self._cooked_inputs, set()
        self._adopt_changed_inputs(paths, skip=chunk)

    def _reconcile_changed_exec_inputs(self, all_changed: set[str], notice) -> None:
        """Mirror any changed inputs:chunk connection into the Python DAG.

        A connection edit is reported against the *attribute* path
        (/chunks/b.inputs:chunk), not the prim -- verified against Usd.Notice.ObjectsChanged,
        which puts it in GetChangedInfoOnlyPaths(). The rest of _on_objects_changed matches
        on prim paths only, which is why this wiring went unnoticed there for so long.

        Routing this through the stage notice rather than the node graph's
        _author_connection keeps every authoring path in sync -- the Qt graph, the CLI, a
        USD-level edit in usdview, and any future frontend -- instead of each one having to
        remember to call add_dependency itself. Merge slot compaction rides here for exactly
        the same reason: every way a slot can be cleared (a port disconnect, a shake, an
        upstream chunk being deleted, a hand edit in usdview) surfaces as this one notice, so
        the invariant is enforced once instead of at each call site that can break it.
        """
        touched_prims = set()
        for path in all_changed:
            # A property path is "<prim>.<attr>"; prim paths contain no dot at all. Matching
            # the attribute name (rather than endswith(".inputs:chunk")) is what lets a
            # merge's numbered slots through -- inputs:chunk1 does NOT end with
            # ".inputs:chunk", and without its Python DAG edge editing that branch would
            # never recook the merge.
            prim_path, sep, attr_name = path.partition(".")
            if not sep or not attr_name.startswith(_INPUTS_PREFIX):
                continue
            # Any inputs: connection, not only the exec slots: a param wire is a real
            # chunk-level dependency for ordering too, and drag-to-connect authors it the
            # same way. _wired_dependencies decides what actually counts.
            if not is_exec_input_name(attr_name):
                # But only when the *connection* moved. A param's stored value lives under
                # this same inputs: prefix, so without this every keystroke into any param
                # would reconcile the whole scene -- a value write reports the "default"
                # field, a wire reports "connectionPaths". No fields at all means the path
                # arrived by resync, where the connection may genuinely have moved, so it
                # is let through rather than guessed at.
                fields = notice.GetChangedFields(Sdf.Path(path))
                if fields and _CONNECTION_PATHS_FIELD not in fields:
                    continue
            touched_prims.add(prim_path)
        if not touched_prims:
            return

        wanted = self._chunks_at(touched_prims)
        if not wanted:
            return

        self._reconciling_exec = True
        try:
            # Compact BEFORE reconciling: compaction renames connections between slots, and
            # the DAG must be reconciled against the settled result, not the torn state.
            for prim_path in touched_prims:
                normalize_merge_slots(self._stage, self._stage.GetPrimAtPath(Sdf.Path(prim_path)))
            for chunk in wanted:
                reconcile_dag_dependencies(chunk)
        finally:
            self._reconciling_exec = False

    def _teardown_chunks(self) -> None:
        """Drop every chunk in the scene, Python-side only.

        _reloading keeps _on_chunk_deleted off the stage: by the time this runs self._stage
        is the stage being switched TO, and the outgoing chunks' prim paths routinely match
        the incoming file's, so the usual per-chunk prim removal would delete the new file's
        own content. The outgoing stage is discarded whole anyway, so there is nothing to
        unauthor on either side.
        """
        self._reloading = True
        try:
            for ctx in list(self._scene.chunks.get_my_params()):
                ctx.request_delete()
        finally:
            self._reloading = False
        self._loaded_prim_paths.clear()
        self._chunk_prim_paths.clear()

    def reload_for_stage(self, stage: Usd.Stage) -> None:
        """Reinitialize for a newly opened stage (File > Open).

        Safe to call from a deferred context (e.g. QTimer.singleShot) once the stage has fully
        settled.

        Compares stage *object identity*, not the root layer's identifier string --
        re-exporting to the same path and reloading (a common iteration workflow)
        gives a genuinely new Usd.Stage with the same file path, so comparing
        identifiers would wrongly skip the reset here, leaving the previous file's
        chunk objects (and their now-orphaned bookkeeping/positions) still registered
        and silently re-wrapped alongside the freshly loaded content.

        Args:
            stage: The new stage, or None when the host has dropped its stage.
        """
        if not self._scene:
            return
        if stage is None:
            # The host dropped its stage -- usdview clears it before opening the next file,
            # notifying twice (None, then the new stage). Drop the graph now instead of
            # waiting for the second notice: it belongs to a stage that no longer exists,
            # and usdview keeps pumping the Qt event loop across the gap (it even clears
            # the prim selection in there), so anything the previous graph still reacts to
            # -- a selection sync, a deferred rebuild, a click -- ran against no stage at all.
            self._teardown_chunks()
            self._loaded_stage = None
            self._stage = None
            return
        if stage is self._loaded_stage:
            return
        self._loaded_stage = stage
        self._stage = stage
        chunk_registry.ChunkLayerManager().initialize(stage)
        self._teardown_chunks()
        # Snapshot before load_chunks -- the load itself overwrites any selection whose layer
        # isn't back yet (see snapshot_edit_target_selections).
        saved_targets = snapshot_edit_target_selections(stage)
        load_chunks(stage, self._scene, self._loaded_prim_paths)
        # Re-apply Edit Target selections dropped during load (Menu rejected them before their
        # layers existed), then consolidate any chunk whose saved home doesn't match its
        # topology-resolved target (e.g. a file authored before Edit Target became position-based).
        restore_edit_target_selections(stage, self._scene, saved_targets)
        # protect_root_layer, exactly as initialize() above: opening a file must not rewrite
        # it. This is the path a host that hands over its stage after the plugin is up
        # (usdview, via signalStageReplaced) loads through, and unprotected it migrated
        # every chunk's bookkeeping out of the file's own root layer -- 34 Chunk specs down
        # to 6 on a measured open -- and re-cooked each one it moved, 36 executes for 8.
        # The file on disk survives until something saves that layer; the Sdf cache hands
        # the gutted layer to the next open of the same path in the same process either way.
        reconcile_edit_targets(stage, self._scene, protect_root_layer=True)

    def _on_attr_changed(self, notice, sender):
        if not self.initialized or not self._stage:
            return
        attr_sender = notice.data
        if not isinstance(attr_sender, core.AttributeSender):
            return

        if attr_sender.name == "value":
            owner = attr_sender.get_owner()
            if owner is not None and self._records_param_writes(owner):
                self._sync_list_proxy_to_usd(owner)
                if isinstance(owner, core.ChunkParamBase):
                    chunk = owner.get_chunk()
                    if chunk and id(chunk) in self._chunk_prim_paths:
                        self._sync_param_input(chunk, owner)
                        # Once per chunk per batch or solve: a Wedge change writes hundreds of params,
                        # and each refresh reads every param the chunk has.
                        refresh = functools.partial(self._refresh_gizmo_anchors, chunk)
                        if not chunk_cook.defer_to_batch_end(("gizmo_anchors", id(chunk)), refresh):
                            refresh()
                        self._follow_composed(chunk, owner)
                elif isinstance(owner, core.ChunkVectorBase):
                    chunk = owner.get_chunk()
                    if chunk and id(chunk) in self._chunk_prim_paths:
                        self._sync_vector_input(chunk, owner)
                        self._follow_composed(chunk, owner)
            return

        if attr_sender.name != "name":
            return
        old_name = attr_sender.old_value
        owner = attr_sender.get_owner()
        if not old_name or old_name == getattr(owner, "name", None):
            return

        if isinstance(owner, core.ChunkBase):
            old_path = self._chunk_prim_paths.get(id(owner))
            if not old_path:
                return  # chunk not yet registered -- name collision during creation, not a user rename
            # Rename in place (sibling): keep the prim's parent, swap the leaf name. Do NOT use
            # _resolve_chunk_root -- for a group chunk get_context() returns self, so its root is the
            # group's own prim, which would nest the rename inside itself (/G -> /G/newname).
            new_path = str(Sdf.Path(old_path).GetParentPath().AppendChild(owner.name))
            if new_path == old_path:
                # Already registered under the new name: a change_block held the creation-time
                # collision rename until after the chunk was added.
                return
            self._handle_rename(
                old_path=old_path,
                new_path=new_path,
                old_name=old_name,
                new_name=owner.name,
                chunk=owner,
            )
            self._chunk_prim_paths.track(owner, new_path)
            if owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                # Renaming a group renames its whole subtree (BatchNamespaceEdit renames
                # descendants too). Update the group's prim_path and rebase every descendant's
                # tracked path so /old/... becomes /new/...
                owner.prim_path = new_path
                self._rebase_descendant_paths(old_path, new_path)
        elif isinstance(owner, (core.ChunkParamBase, core.ChunkVectorBase, core.ChunkGroupBase)):
            # A param, a vector or a plain group -- ChunkBase is a ChunkGroupBase too, which
            # is why the chunk case has to come first.
            self._handle_param_rename(owner, old_name, owner.name)

    def _records_param_writes(self, owner) -> bool:
        """Whether this param write should be recorded on its chunk prim.

        Normally yes, and normally the question is only asked of a user edit. The exception is
        chunk_notice._usd_write_active, set around our own USD writes so the synchronous
        ObjectsChanged that follows doesn't come back through here as if it were an edit.

        A chunk executing sets that same flag for the whole of module.execute(), which is
        correct for its purpose -- a cook authors content, and none of it should bounce back
        into params -- but it also silenced the chunk assigning its OWN params, which is a
        real value and the only record of it. A saved scene is chunk prims and nothing else
        (export_chunks), so a param that never gets an attr is a param that does not survive
        a save.

        For example, a Camera on a prim that already carries a placement takes it into its
        transform params (camera.py's `_adopt_existing_transform`) during a cook -- the live
        camera would be right while the file carried no placement at all. Every chunk
        that renames its own prim path mid-cook when the one it wanted was taken (camera,
        light, the render_* chunks) depends on this the same way.

        So a chunk writing its own params while it executes is recorded. Anything else
        arriving mid-write still isn't -- including one chunk reaching into another's params
        during its cook, which is a push, not a record of the writer's own state.
        """
        if not chunk_notice._usd_write_active:
            return True
        executing = chunk_notice._executing_chunk
        if executing is None:
            return False
        # Not every owner that reaches here is a param with a chunk behind it -- a list-row
        # proxy arrives too, and answering "whose is this" for one is what _sync_list_proxy_
        # to_usd exists to do.
        get_chunk = getattr(owner, "get_chunk", None)
        return callable(get_chunk) and get_chunk() is executing

    def _handle_rename(self, old_path: str, new_path: str, old_name: str, new_name: str, chunk=None) -> None:
        """Rename a chunk prim and patch all references to it.

        Renames the prim in every layer that actually authors it. If the prim can't be
        resolved to any spec, fall back to the stage's current edit target rather than bail
        (bailing left the chunk renamed but the prim not, so the node vanished).
        """
        if not self._stage:
            return
        old_prim = self._stage.GetPrimAtPath(Sdf.Path(old_path))
        if old_prim and old_prim.IsValid() and chunk_utils.is_composed_in(old_prim):
            # USD has no rename override: the name is the sequence file's, and renaming
            # it in place would rewrite that file. Localise the network first.
            logger.warning(
                "Cannot rename %s: it is composed in from another file (localise the network "
                "to edit its structure). The chunk keeps the name %r on the stage.",
                old_path, old_name)
            if chunk is not None:
                object.__setattr__(chunk, "name", old_name)
            return
        target_layers = []
        if old_prim and old_prim.IsValid():
            for spec in old_prim.GetPrimStack():
                if spec.layer not in target_layers:
                    target_layers.append(spec.layer)
        # Fallback if the prim couldn't be resolved: the stage's current edit target.
        if not target_layers:
            fallback = self._stage.GetEditTarget().GetLayer()
            if fallback is not None:
                target_layers = [fallback]
        if not target_layers:
            return

        # Guard the whole rename as a chunk-driven write. Applying the namespace edit fires
        # StageContentsChanged synchronously; without this, _on_contents_changed re-enters
        # load_chunks before _loaded_prim_paths is updated below, sees the renamed prim as
        # "new", and creates a duplicate chunk (the "renamed node vanishes / stale copy" bug).
        old_write = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            applied_any = False
            for lyr in target_layers:
                edit = Sdf.BatchNamespaceEdit()
                edit.Add(Sdf.Path(old_path), Sdf.Path(new_path))
                if lyr.Apply(edit):
                    applied_any = True
            if not applied_any:
                logger.warning("Failed to rename prim %s → %s", old_path, new_path)
                return
            if chunk is not None:
                object.__setattr__(chunk, "_exec_prim_path", new_path)
            # BatchNamespaceEdit renames the prim but does NOT fix up connections that target
            # it (e.g. a view's inputs:chunk -> old.outputs:chunk), so they'd dangle and the
            # wire would drop. Repoint every chunk-prim connection from old_path to new_path.
            self._fixup_connection_targets(old_path, new_path)
            _patch_expressions(self._stage, f"@{old_name}.", f"@{new_name}.")
            _patch_param_expressions(self._scene.get_chunks(), f"@{old_name}.", f"@{new_name}.")
            self._loaded_prim_paths.discard(old_path)
            self._loaded_prim_paths.add(new_path)
        finally:
            chunk_notice._usd_write_active = old_write

    def _fixup_connection_targets(self, old_path: str, new_path: str) -> None:
        """Repoint every chunk-prim connection to old_path onto new_path, so wiring survives a rename.

        BatchNamespaceEdit leaves connection targets dangling.
        """
        old_sdf, new_sdf = Sdf.Path(old_path), Sdf.Path(new_path)
        for prim in self._stage.Traverse():
            if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
                continue
            for attr in prim.GetAttributes():
                if not attr.HasAuthoredConnections():
                    continue
                # Rewritten in each layer that authors the connection, not set once through
                # the edit target: an exec wire lives in the session layer, so a corrected
                # copy in the root layer sat under the stale one, the wire kept pointing at
                # the old name, and the root layer gained a stray opinion besides.
                for spec in attr.GetPropertyStack():
                    paths = spec.connectionPathList
                    for field in ("explicitItems", "prependedItems", "appendedItems",
                                  "deletedItems", "orderedItems"):
                        items = list(getattr(paths, field))
                        moved = [c.ReplacePrefix(old_sdf, new_sdf) for c in items]
                        if moved != items:
                            setattr(paths, field, moved)

    def _rebase_descendant_paths(self, old_root: str, new_root: str) -> None:
        """Rebase every tracked descendant path after a group is renamed.

        The group's subtree moved from old_root/... to new_root/..., so the prim-path map, the
        loaded set, _exec_prim_path and nested group prim_paths are all rebased to keep later
        lookups matching the moved prims.
        """
        old_prefix = old_root + "/"

        def rebase(p: str) -> str:
            return new_root + p[len(old_root):] if p.startswith(old_prefix) else p

        for cid, cpath in list(self._chunk_prim_paths.items()):
            self._chunk_prim_paths[cid] = rebase(cpath)
        self._loaded_prim_paths = _PathSet(rebase(p) for p in self._loaded_prim_paths)
        for c in self._scene.get_chunks():
            ep = getattr(c, "_exec_prim_path", None)
            if ep and ep.startswith(old_prefix):
                object.__setattr__(c, "_exec_prim_path", rebase(ep))
            if (c.has_flag(ChunkFlags.GroupFlags.CONTEXT) and getattr(c, "prim_path", None)
                    and c.prim_path.startswith(old_prefix)):
                c.prim_path = rebase(c.prim_path)

    def _patch_renamed_param_refs(self, chunk: core.ChunkBase, old_name: str, new_name: str) -> None:
        """Point every expression that reads a renamed param of chunk at its new name."""
        # An expression names a param by its bare name (@node.param@), at any depth, so
        # match the whole token with this chunk's name: ".a@" alone would also rewrite
        # another chunk's param of the same name. Patched whether or not the param has an
        # attribute of its own: one still at its default has none, and other chunks can
        # refer to it all the same. A list row is @node.param[1]@, matched by the second pair.
        for old_ref, new_ref in ((f"@{chunk.name}.{old_name}@", f"@{chunk.name}.{new_name}@"),
                                 (f"@{chunk.name}.{old_name}[", f"@{chunk.name}.{new_name}[")):
            _patch_expressions(self._stage, old_ref, new_ref)
            _patch_param_expressions(self._scene.get_chunks(), old_ref, new_ref)

    def _handle_param_rename(self, owner: core.ChunkCommon, old_name: str, new_name: str) -> None:
        """Rename the USD attributes behind a renamed param, vector or group, and rebind them.

        An attribute is named by the whole path to its param -- inputs:group:param -- so a
        rename at any depth has to rename the attribute, and a group's rename renames every
        attribute under it. The params then have to be pointed at their new attributes: a
        param writes its value to the attribute it was bound to, so a binding left on the
        old name would send every later edit nowhere, and the next save would write back
        the value from before the rename.
        """
        if not (self.initialized and self._stage):
            return
        found = next(
            (
                (c, segments)
                for c in self._scene.get_chunks()
                if (segments := _param_object_path(c, owner)) is not None
            ),
            None,
        )
        if found is None:
            # A list template is not on the path walk (a list shows its rows, not its
            # template), and holds no attribute of its own, but rows are read by its name.
            if (isinstance(owner, core.ChunkParamBase) and (chunk := owner.get_chunk()) is not None
                    and _under_list_group(owner, chunk)):
                self._patch_renamed_param_refs(chunk, old_name, new_name)
            return
        chunk, segments = found
        new_ns = ":".join(segments)
        old_ns = ":".join(segments[:-1] + [old_name])

        prim_path = self._chunk_prim_paths.get(id(chunk)) or f"{_resolve_chunk_root(chunk)}/{chunk.name}"
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not prim.IsValid():
            return

        # A leaf names one attribute; a group (or vector) is also a namespace for the
        # attributes of everything inside it.
        is_leaf = isinstance(owner, core.ChunkParamBase)
        old_attr = f"{_INPUTS_PREFIX}{old_ns}"
        renames = {}
        for name in prim.GetPropertyNames():
            if name == old_attr:
                renames[name] = f"{_INPUTS_PREFIX}{new_ns}"
            elif not is_leaf and name.startswith(old_attr + ":"):
                renames[name] = f"{_INPUTS_PREFIX}{new_ns}{name[len(old_attr):]}"

        self._patch_renamed_param_refs(chunk, old_name, new_name)
        if not renames:
            return

        prev_write = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            # Wherever each attr's spec currently lives -- its Edit Target when first
            # authored, not necessarily the session layer anymore. A batch edit only applies
            # to a layer holding every one of its sources, so each layer gets its own.
            for layer in self._stage.GetLayerStack(includeSessionLayers=True):
                prim_spec = layer.GetPrimAtPath(Sdf.Path(prim_path))
                if not prim_spec:
                    continue
                edit = Sdf.BatchNamespaceEdit()
                for old, new in renames.items():
                    if old in prim_spec.properties:
                        edit.Add(Sdf.Path(f"{prim_path}.{old}"), Sdf.Path(f"{prim_path}.{new}"))
                if edit.edits:
                    layer.Apply(edit)

            # A namespace edit leaves connections to the old attributes dangling (a loft
            # into this param, say); point them at the new names.
            owner_prim = Sdf.Path(prim_path)
            for other in self._stage.Traverse():
                if other.GetTypeName() != _CHUNK_PRIM_TYPENAME:
                    continue
                for attr in other.GetAttributes():
                    conns = attr.GetConnections()
                    if not conns:
                        continue
                    moved = [
                        owner_prim.AppendProperty(renames[c.name])
                        if c.GetPrimPath() == owner_prim and c.name in renames else c
                        for c in conns
                    ]
                    if moved != list(conns):
                        attr.SetConnections(moved)
        finally:
            chunk_notice._usd_write_active = prev_write

        paths = {f"{prim_path}.{old}": f"{prim_path}.{new}" for old, new in renames.items()}
        bound = [p for _, p in _iter_chunk_params(chunk)] + [v for _, v in _iter_chunk_vectors(chunk)]
        for param in bound:
            if (new_path := paths.get(getattr(param, "_param_attr_path", None))) is not None:
                param._param_attr_path = new_path

    def _sync_list_proxy_to_usd(self, proxy_param) -> None:
        """Write the full array for a ListGroup template param when any proxy value changes."""
        # Ownership chain: ProxyParam → NameSyncedContainer → ChunkGroupBase("0") → ChunkListGroupBase
        container = proxy_param.get_owner()
        proxy_group = container.get_owner() if container is not None else None
        if not isinstance(proxy_group, core.ChunkGroupBase) or not proxy_group.name.isdigit():
            return
        list_group = proxy_group.get_owner()
        if not isinstance(list_group, core.ChunkListGroupBase):
            return
        chunk = list_group.get_chunk()
        if chunk is None:
            return
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path:
            prim_path = f"{_resolve_chunk_root(chunk)}/{chunk.name}"
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not prim.IsValid():
            return
        lg_path = next(
            (p for p, lg in _iter_list_group_arrays(chunk) if lg is list_group), None
        )
        if lg_path is None:
            return
        if not isinstance(proxy_param, core.ChunkParamBase):
            return  # a vector row: no array type holds its tuples
        template_name = proxy_param.name
        values = [
            v for i in range(list_group.size)
            if (px := list_group.get_indexed_param(template_name, i)) is not None
            # In the param's own number type: the array's type is taken from its values, and
            # a float row holding a whole number (or wired to an int) would make it int[].
            for v in [px._as_own_number(px.value)] if v is not None
        ]
        if not values:
            return
        sdf_type, usd_value = _sdf_type_and_value(values)
        if sdf_type is None:
            return
        attr_name = f"{_INPUTS_PREFIX}{lg_path.replace('.', ':')}:{template_name}"
        prev_write = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            # Rewritten wholesale on every change (not incrementally) -- if it already
            # exists, keep updating it wherever its spec already lives; only a genuinely
            # new attr needs a target resolved via the chunk's current Edit Target.
            existing = prim.GetAttribute(attr_name)
            home = chunk_utils.get_attr_home_layer(existing) if existing.IsValid() else None
            ctx = Usd.EditContext(self._stage, home) if home else self._chunk_prim_edit_context(chunk)
            with ctx:
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                attr.Set(usd_value)
                _store_row_expressions(attr, list_group, template_name)
        finally:
            chunk_notice._usd_write_active = prev_write

    def _sync_list_arrays(self, chunk: core.ChunkBase) -> None:
        """Write every list group's arrays for the rows a chunk holds as its prim is made.

        _sync_list_proxy_to_usd writes an array when a row changes, and only once the chunk
        has a prim. A paste fills the rows in before the chunk is added, so a pasted Wedge
        showed its ranges and drove its outports until the save, then reopened with none.
        """
        for _, list_group in _iter_list_group_arrays(chunk):
            if list_group.size and list_group.proxy_params:
                for proxy in list_group.proxy_params[0].get_my_params():
                    self._sync_list_proxy_to_usd(proxy)

    def _on_dependency_added(self, notice, sender):
        if not self.initialized or not self._stage:
            return
        if self._reconciling_exec:
            # The edge is being mirrored FROM the stage, so inputs:chunk already says this.
            # Authoring it again writes a second spec into the bookkeeping layer, and
            # reconcile_edit_targets later migrates whichever spec it finds first -- which is
            # how a healed exec chain got silently reverted to the pre-heal connection.
            return
        dependent = sender
        dependency = notice.data
        if not isinstance(dependent, core.ChunkBase) or not isinstance(dependency, core.ChunkBase):
            return
        # get_chunk_prim_path, not root + name: for a chunkgroup the root is the group's own
        # path, so the lookup missed and a scripted group's exec input was never written.
        dep_prim = self._stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dependent)))
        if not dep_prim.IsValid():
            return
        if get_chunk_prim_path(dependency) in implied_sources(dep_prim):
            return  # the layer stack's edge, derived on every read and never authored
        src_path = Sdf.Path(exec_output_path(dependency))
        # Nothing below this is worth doing for an edge USD already has -- and on load that
        # is every edge, since load_chunks mirrors add_dependency FROM the authored
        # inputs:chunk. Checking before opening the edit context matters as much as the
        # skipped write: resolving that context walks the exec chain upstream, as does the
        # reconcile, so a graph restored edge by edge paid two full walks per chunk to
        # re-derive an answer that could not have changed.
        # Every slot, not just inputs:chunk -- otherwise a branch already wired into a merge's
        # slot 2 looks absent here and gets authored a second time.
        existing_conns = exec_input_connections(dep_prim)
        if src_path in existing_conns:
            return
        slots = exec_input_slots_for_prim(dep_prim)
        if has_dynamic_exec_inputs_prim(dep_prim) or slots > 1:
            # A dynamic-slot chunk (Merge, Switch, ...) holds exactly one connection per slot.
            # Stacking a second target onto slot 0 (what the branch below does) would
            # collapse two branches into a single port and destroy the ordering the slots
            # exist to express, so author into the first free slot instead -- appending the
            # new branch as the weakest. Density is guaranteed by normalize_merge_slots, so
            # the free slot is just the count.
            #
            # A fixed-slot chunk (config.exec_inputs > 1) wants the same placement for a
            # different reason: its slots are positional, so add_dependency filling slot 0
            # twice would silently make the second branch mean what the first does. Past the
            # last slot the wire stacks onto slot 0 rather than inventing a port the chunk
            # does not have -- add_dependency has no port to have been dropped on, so
            # refusing outright would lose the edge with nothing to show for it.
            index = len(existing_conns)
            if not has_dynamic_exec_inputs_prim(dep_prim) and index >= slots:
                index = 0
            with self._chunk_prim_edit_context(dependent):
                exec_in = dep_prim.CreateAttribute(
                    exec_input_name(index), Sdf.ValueTypeNames.Token, custom=False
                )
                exec_in.SetConnections(list(exec_in.GetConnections()) + [src_path])
        else:
            with self._chunk_prim_edit_context(dependent):
                exec_in = dep_prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
                existing = list(exec_in.GetConnections())
                if src_path not in existing:
                    exec_in.SetConnections(existing + [src_path])
        # The exec chain the memo walks just gained an edge, under a suppressed-notice
        # edit context.
        invalidate_edit_layer_memo()
        # Wiring a chunk into the exec chain can change the Edit Target that now applies at
        # its position (e.g. it's now downstream of an Edit Target chunk) -- re-home it and
        # everything downstream to honor the target at that place. Nothing above the new edge
        # can have moved, so the pass starts at the chunk that gained the input.
        reconcile_edit_targets(self._stage, self._scene, roots=[dependent])

    def _sync_vector_input(self, chunk: core.ChunkBase, vector: core.ChunkVectorBase) -> None:
        """Create/restore a chunk-prim attr for a vector and wire it.

        If the vector already drives a real prim attr (via connect_usd_attr -- e.g. a
        Vec3f xformOp), the real attr's exact USD type is preserved and its value seeds
        the chunk-prim attr. Otherwise (a standalone vector param, e.g. a compute-chunk
        input/output with no real prim behind it) the type is derived from the vector's
        own default_value/length and lazily authored on first non-default change or
        expression connection, mirroring _sync_param_input's pattern for scalars.
        """
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path or is_transient_param(vector):
            return

        existing = getattr(vector, '_param_attr_path', None)
        if existing and existing.startswith(str(prim_path)):
            return  # already wired to this chunk prim

        raw = getattr(vector, 'raw_value', None)
        is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)

        real_attr_path = existing
        real_attr = self._stage.GetAttributeAtPath(Sdf.Path(real_attr_path)) if real_attr_path else None
        if real_attr and not real_attr.IsValid():
            real_attr = None

        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not (prim and prim.IsValid()):
            return

        vector_path = next((p for p, v in _iter_chunk_vectors(chunk) if v is vector), None)
        if vector_path is None:
            return

        if real_attr:
            sdf_type = real_attr.GetTypeName()
        else:
            type_source = vector.default_value
            if ChunkFlags.ParamType.FLOAT in vector.evaluate_type():
                sdf_type, _ = _VECTOR_TYPE_MAP[(float, vector.length)]
            else:
                sdf_type, _ = _sdf_vector_type(type_source) if type_source else (None, None)
            if sdf_type is None:
                return
            if not is_expr and vector.value == vector.default_value:
                return  # still at default -- don't create attr

        prefix = "outputs:" if vector.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
        attr_name = f"{prefix}{vector_path.replace('.', ':')}"

        saved = prim.GetAttribute(attr_name)
        if saved.IsValid() and saved.Get() is not None:
            attr = saved
        elif real_attr:
            if not real_attr.HasAuthoredValue():
                return  # vector attr is at schema default -- skip
            current_val = real_attr.Get()
            if current_val is None:
                return
            with self._chunk_prim_edit_context(chunk):
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                attr.Set(current_val)
        else:
            # Chunk-prim bookkeeping -- the chunk's currently active Edit Target (session
            # by default), matching _sync_param_input's equivalent branch for scalars.
            with self._chunk_prim_edit_context(chunk):
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                if is_expr:
                    attr.SetConnections(self._expr_connections(prim, raw))
                    chunk_utils.store_expression_text(attr, raw)
                else:
                    attr.Set(chunk_utils.as_attr_value(attr, vector.value))

        vector._connect_param_attr(attr)

        if real_attr_path and real_attr_path != str(attr.GetPath()):
            # Inside the old binding's variant -- see _sync_param_input.
            with chunk_utils.edit_context_for(self._stage, *_variant_scope_of(vector, self._stage)):
                if old_attr := self._stage.GetAttributeAtPath(Sdf.Path(real_attr_path)):
                    if old_attr.IsValid():
                        # Through _drive_from_storage, not hand-rolled: it carries the
                        # muted-layer guard _push_driven_values has (USD raises rather than
                        # no-ops authoring into a muted layer) and threads var_selections
                        # through to connect_usd_attr, so a Vector param show_attribute bound
                        # inside a variant (displayColor, extent, a Vec3f xformOp on a
                        # variant-scoped prim) keeps its edits inside that variant.
                        self._drive_from_storage(vector, old_attr)
                        # See _sync_param_input's matching comment: vector._var_selections
                        # described old_attr, not `attr` (this chunk's own storage attr) that
                        # _param_attr_path is now bound to. _drive_from_storage already read it
                        # above; clear it now so this vector's own future storage-attr writes
                        # don't misfire into a stale variant scope.
                        vector._var_selections = ()

    def _refresh_gizmo_anchors(self, chunk: core.ChunkBase) -> None:
        """Keep every gizmo's anchor rel pointing at the prim the chunk currently targets.

        Hangs off the generic param-changed path rather than watching the anchor param
        specifically: which param is the anchor is per-gizmo and can be inferred, so there is
        no single name to watch. Cheap enough to sit here because chunk_gizmo returns
        immediately for a chunk that declares no gizmos, which is nearly all of them.
        """
        if not chunk_gizmo.get_gizmos(chunk):
            return
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path:
            return
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not (prim and prim.IsValid()):
            return
        home = chunk_utils.get_bookkeeping_layer(self._stage, chunk)
        ctx = Usd.EditContext(self._stage, home) if home else contextlib.nullcontext()
        with ctx:
            chunk_gizmo.refresh_gizmo_anchors(self._stage, prim, chunk)

    def _sync_param_input(self, chunk: core.ChunkBase, param: core.ChunkParamBase, force: bool = False) -> None:
        """Create and wire a single param's chunk-prim input attr on first non-default change.

        Skips immediately if the param is already bound to this chunk's prim attr. With
        force=True the attr is created even while the param is still at its default value
        (used by Loft up, which needs a USD attr to promote to the context interface).
        """
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path:
            return

        # Session state, deliberately never recorded: see ChunkFlags.ParamFlags.TRANSIENT.
        if is_transient_param(param):
            return

        existing = getattr(param, '_param_attr_path', None)
        if existing and existing.startswith(prim_path):
            return  # already wired; data_changed already wrote the value

        raw = getattr(param, 'raw_value', None)
        is_button = ChunkFlags.ParamType.BUTTON in param.evaluate_type()
        if raw is None and not (is_button and force):
            return
        is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
        sdf_type = param_sdf_type(param)
        if sdf_type is None:
            return
        default = getattr(param, 'default_value', None)
        # A chunk with a module gets its params back by running main(), so an attribute for
        # one still at its default is redundant. A chunk built in the interpreter has no
        # main(): the attribute IS the only record that the param exists, so leaving it out
        # means the param is gone on reload -- not merely reset. Author it.
        cfg = chunk.get_config()
        module_less = not (cfg and cfg.module)
        if not force and not module_less and not is_expr and raw == default:
            return  # still at default -- don't create attr

        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not (prim and prim.IsValid()):
            return

        param_path = next((p for p, q in _iter_chunk_params(chunk) if q is param), None)
        if param_path is None:
            # Param not in scalar iteration -- it may be a child of a vector.
            parent_vector = next(
                (v for _, v in _iter_chunk_vectors(chunk)
                 if any(c is param for c in v.get_my_params())),
                None
            )
            if parent_vector:
                self._sync_vector_input(chunk, parent_vector)
            return

        prefix = "outputs:" if param.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
        attr_name = f"{prefix}{param_path.replace('.', ':')}"
        saved = prim.GetAttribute(attr_name)
        if saved.IsValid() and saved.GetTypeName() != sdf_type:
            self._delete_stale_attr_specs(prim, attr_name)
            saved = prim.GetAttribute(attr_name)
        if saved.IsValid() and saved.Get() is not None:
            attr = saved
            # The saved value predates whatever brought this param here -- a change to it,
            # or a chunk unblocking with a value typed in while it was -- and binding reads
            # the attr back into the param. Unwritten, that read undid the change: the first
            # time a Wedge set a range after a network was opened, every param still bound
            # to nothing went straight back to the value saved in the file, and whatever it
            # drove kept the old variant until the next change.
            self._store_in_saved_attr(chunk, param, attr, raw, is_expr, prim)
        else:
            # Chunk-prim bookkeeping -- the chunk's currently active Edit Target (session
            # by default, see chunk_utils.get_bookkeeping_layer).
            with self._chunk_prim_edit_context(chunk):
                attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                if is_expr:
                    attr.SetConnections(self._expr_connections(prim, raw))
                    chunk_utils.store_expression_text(attr, raw)
                elif raw is not None:
                    attr.Set(raw)
        # The attr holds the param's value either way now; nothing to read back.
        param._connect_param_attr(attr, read_back=False)

        if existing and existing != str(attr.GetPath()):
            # Inside the variant the old binding was made in: a prim that only exists in
            # that variant is not on the stage while another one is selected, so looking
            # it up outside found nothing and the param was never promoted to a driven
            # connection -- its edits then went nowhere.
            with chunk_utils.edit_context_for(self._stage, *_variant_scope_of(param, self._stage)):
                old_attr = self._stage.GetAttributeAtPath(Sdf.Path(existing))
                if old_attr and old_attr.IsValid():
                    self._drive_from_storage(param, old_attr)
                    # param._var_selections (still the OLD binding's, since _connect_param_attr
                    # above was called without var_selections and leaves it alone) described
                    # `existing` -- a real prim's attribute, possibly variant-scoped. attr, the
                    # NEW _param_attr_path, is this chunk's own bookkeeping attribute, which is
                    # never inside anyone's variant. Leaving the stale tuple in place misdirects
                    # every future edit's OWN storage-attr write (ChunkUsdAttributeSupport's
                    # data_changed) into `existing`'s variant context for an unrelated path,
                    # which USD silently no-ops -- the edit stops reaching the chunk prim's
                    # saved value at all, so a save/reload restores the pre-edit number.
                    # _drive_from_storage already read the old value above; safe to clear now.
                    param._var_selections = ()

    def _store_in_saved_attr(self, chunk, param, attr, raw, is_expr, prim) -> None:
        """Write param's current value into an attr it is about to be bound to.

        Where update_usd_prop would write it once bound: the attr's home layer, so the edit
        lands where the saved value lives rather than in a stronger layer over it.
        """
        stage = attr.GetStage()
        home = chunk_utils.get_attr_home_layer(attr) or chunk_utils.get_bookkeeping_layer(stage, chunk)
        prev_write = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            with Usd.EditContext(stage, home):
                if is_expr:
                    attr.SetConnections(self._expr_connections(prim, raw))
                    chunk_utils.store_expression_text(attr, raw)
                    value = raw if chunk_utils.attr_holds_text(attr) else param.value
                else:
                    chunk_utils.clear_expression_text(attr)
                    value = raw
                if isinstance(value, str) and not chunk_utils.attr_holds_text(attr):
                    return  # an unresolved expression: the connections carry it
                value = chunk_utils.as_attr_value(attr, value)
                if attr.Get(0) != value:
                    attr.Set(value)
        finally:
            chunk_notice._usd_write_active = prev_write

    def _delete_stale_attr_specs(self, prim: Usd.Prim, attr_name: str) -> None:
        """Remove attr_name's spec from every layer in the stack where it is found.

        The stale spec may live in whichever layer was the edit target when it was first
        authored -- not necessarily the current one -- so the whole stack is scanned rather than
        just one layer.
        """
        for layer in self._stage.GetLayerStack(includeSessionLayers=True):
            prim_spec = layer.GetPrimAtPath(prim.GetPath())
            if prim_spec and attr_name in prim_spec.properties:
                del prim_spec.properties[attr_name]

    def _sync_chunk_inputs(self, chunk: core.ChunkBase) -> None:
        """Wire dynamically-added params to chunk prim inputs: attrs.

        Creates the inputs: attr for each param (so _connect_param_attr can
        bind to it), but only authors a value when the param differs from its
        default.  Also registers any previously-direct real-prim attr as a
        driven attr so USD connections propagate param changes to it.
        """
        prim_path = self._chunk_prim_paths.get(id(chunk))
        if not prim_path:
            return
        prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not (prim and prim.IsValid()):
            return
        for path, param in _iter_chunk_params(chunk):
            raw = getattr(param, 'raw_value', None)
            if raw is None or is_transient_param(param):
                continue
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            type_source = getattr(param, 'default_value', None) if is_expr else raw
            sdf_type = _SDF_TYPE_MAP.get(type(type_source))
            if sdf_type is None:
                continue
            if ChunkFlags.ParamType.FLOAT in param.evaluate_type():
                sdf_type = _SDF_TYPE_MAP[float]
            # See _sync_param_input: a ColorPicker's tuple has no _SDF_TYPE_MAP entry.
            if ChunkFlags.ParamType.COLOR_PICKER in param.evaluate_type():
                sdf_type = Sdf.ValueTypeNames.Color3f
            prefix = "outputs:" if param.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
            attr_name = f"{prefix}{path.replace('.', ':')}"

            # If the prim already has a saved value (from a prior save/load cycle),
            # restore the param from it rather than overwriting with the current default.
            saved = prim.GetAttribute(attr_name)
            if saved.IsValid() and saved.GetTypeName() != sdf_type:
                self._delete_stale_attr_specs(prim, attr_name)
                saved = prim.GetAttribute(attr_name)
            if saved.IsValid() and saved.Get() is not None:
                attr = saved
            else:
                backing_path = getattr(param, '_param_attr_path', None)
                if not is_expr:
                    if backing_path:
                        # param_from_attr: only store if the real USD attr has been authored.
                        # Looked up inside its recorded variant -- see _sync_param_input.
                        with chunk_utils.edit_context_for(self._stage, *_variant_scope_of(param, self._stage)):
                            backing = self._stage.GetAttributeAtPath(Sdf.Path(backing_path))
                            authored = bool(backing and backing.HasAuthoredValue())
                        if not authored:
                            continue
                    else:
                        default = getattr(param, 'default_value', None)
                        # A chunk with a module rebuilds its params by running main(), so an
                        # attribute for one still at its default says nothing the code does
                        # not. A chunk built in the interpreter has no main(): the attribute
                        # IS the only record that the param exists, so skipping it means the
                        # param does not come back reset -- it comes back missing.
                        cfg = chunk.get_config()
                        if raw == default and (cfg and cfg.module):
                            continue  # pure-Python param at its default -- skip
                with self._chunk_prim_edit_context(chunk):
                    attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
                    if is_expr:
                        attr.SetConnections(self._expr_connections(prim, raw))
                        chunk_utils.store_expression_text(attr, raw)
                    else:
                        attr.Set(raw)

            old_storage_path = getattr(param, '_param_attr_path', None)
            param._connect_param_attr(attr)  # reads attr value and updates param

            if old_storage_path and old_storage_path != str(attr.GetPath()):
                # Inside the old binding's variant -- see _sync_param_input.
                with chunk_utils.edit_context_for(self._stage, *_variant_scope_of(param, self._stage)):
                    old_attr = self._stage.GetAttributeAtPath(Sdf.Path(old_storage_path))
                    if old_attr and old_attr.IsValid():
                        self._drive_from_storage(param, old_attr)
                        # See _sync_param_input's matching comment: param._var_selections
                        # described old_attr (a real prim's attribute, possibly variant-scoped),
                        # not `attr` (this chunk's own, never variant-scoped, bookkeeping attr)
                        # that _param_attr_path now points at. _drive_from_storage already read
                        # it above; clear it now so this param's own future storage-attr writes
                        # resolve through get_attr_home_layer instead of a stale variant scope
                        # that silently swallows them (USD no-ops a write mapped to a path
                        # outside the variant's own subtree).
                        param._var_selections = ()

        for _, vector in _iter_chunk_vectors(chunk):
            self._sync_vector_input(chunk, vector)

    def _on_param_added(self, notice, sender):
        if not self.initialized or not self._stage:
            return
        chunk = notice.data
        if not isinstance(chunk, core.ChunkBase):
            if isinstance(chunk, core.ChunkCommon):
                owner_chunk = chunk.get_chunk()
                if owner_chunk:
                    self._sync_chunk_inputs(owner_chunk)
            return
        is_group = chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT)
        if is_group:
            # For CONTEXT group chunks, prim lives at parent_root/name, not under
            # _resolve_chunk_root (which would give prim_path/chunks -- the sub-root).
            parent_ctx = getattr(chunk, "_parent_context", None)
            parent_ctx = parent_ctx() if parent_ctx else None
            if parent_ctx and getattr(parent_ctx, "prim_path", None):
                root = parent_ctx.prim_path
            else:
                root = _CHUNKS_ROOT
        else:
            root = _resolve_chunk_root(chunk)
        prim_path = f"{root}/{chunk.name}"
        # A group restored from the stage already knows where it is -- a network an arc
        # composed in sits at /World/house/chunks/ChunkGroup, not under /chunks -- and its
        # prim path is its identity. Rebuilding it from the parent and name moved it.
        own_path = getattr(chunk, "prim_path", None) if is_group else None
        if own_path and own_path != prim_path and self._stage.GetPrimAtPath(own_path):
            root, prim_path = str(Sdf.Path(own_path).GetParentPath()), own_path
        # If the path is already tracked it's either (a) a load -- load_chunks pre-registers paths
        # before add_chunk -- or (b) a name collision the add_chunk dedup missed. The dedup checks
        # the scene-root container, but a group's siblings live under its parent context, so two
        # same-named groups under one tab collide and this would wrongly skip the second's prim.
        # Distinguish: (b) means the path already belongs to ANOTHER chunk → make the name unique.
        if prim_path in self._loaded_prim_paths:
            taken_by_other = bool(self._chunk_prim_paths.ids_at(prim_path) - {id(chunk)})
            if not taken_by_other:
                # (a) load -- the prim exists; register and skip _define_prim.
                self._chunk_prim_paths.track(chunk, prim_path)
                return
            # (b) collision -- pick the next free sibling name.
            base, i = chunk.name, 1
            while f"{root}/{base}{i}" in self._loaded_prim_paths:
                i += 1
            chunk.name = f"{base}{i}"
            prim_path = f"{root}/{chunk.name}"
        self._loaded_prim_paths.add(prim_path)
        self._chunk_prim_paths.track(chunk, prim_path)
        # Use object.__setattr__ to store the prim path without going through
        # the attrs _wrapped_setattr, which would fire value_changed / notices.
        object.__setattr__(chunk, '_exec_prim_path', prim_path)

        if is_group:
            pc = getattr(chunk, "_parent_context", None)
            tail_context = pc() if pc else None
        else:
            tail_context = chunk.get_context()
        # Auto-chain (prev -> new via a default inputs:chunk dep) only in the Params tab, where
        # creates build a linear chain. In the node graph, new nodes are created UNCONNECTED --
        # the user wires them (or drops onto a wire); only the first node connects, via the
        # view auto-wiring in _ensure_default_view.
        prev = self._find_dag_tail(tail_context, exclude=chunk) if _params_tab_create else None
        prim = self._define_prim(chunk, prev)
        self._wire_inputs(prim, chunk)
        self._sync_list_arrays(chunk)
        # Set prim_path on group contexts so sub-chunks nest correctly under them.
        if is_group:
            chunk.prim_path = prim_path

        # View-follows-tail -- ONLY for chunks created in the Params tab (_params_tab_create).
        # The new chunk was chained after `prev` (prev -> new); if a view was fed by `prev`,
        # move it onto `new` (prev -> new -> view). Never an Output: it stays wherever the
        # user wired it. In the node graph the user wires manually, so we don't auto-repoint
        # the view there.
        if _params_tab_create and prev is not None and not is_terminal_chunk(chunk):
            self._rewire_views_from_source(prev, chunk)

    def _rewire_views_from_source(
        self, old_src: core.ChunkBase, new_src: core.ChunkBase, *, outputs: bool = False
    ) -> None:
        """Repoint any view whose inputs:chunk feeds off old_src onto new_src's output.

        outputs=True moves an Output along too -- for a chunk the framework inserts on the
        user's behalf (a viewport drag), which belongs between the tail and the Output.
        """
        def _path(c):
            return self._chunk_prim_paths.get(id(c)) or f"{_resolve_chunk_root(c)}/{c.name}"
        old_path = _path(old_src)
        new_out = Sdf.Path(f"{_path(new_src)}.{_EXEC_OUTPUT}")
        for c in self._scene.get_chunks():
            if not (is_view_chunk(c) or (outputs and is_output_chunk(c))):
                continue
            vprim = self._stage.GetPrimAtPath(Sdf.Path(_path(c)))
            if not vprim.IsValid():
                continue
            exec_in = vprim.GetAttribute(_EXEC_INPUT)
            if not (exec_in and exec_in.IsValid()):
                continue
            if any(str(p.GetPrimPath()) == old_path for p in exec_in.GetConnections()):
                # exec_in already exists -- update wherever its spec already lives, not
                # wherever c's Edit Target currently resolves to (see get_attr_home_layer).
                home = chunk_utils.get_attr_home_layer(exec_in)
                ctx = Usd.EditContext(self._stage, home) if home else self._chunk_prim_edit_context(c)
                with ctx:
                    exec_in.SetConnections([new_out])

    def _on_chunk_loaded(self, notice, sender):
        """Run a chunk's module execute() hook once on interactive creation.

        We fire on CHUNK_LOADED rather than PARAM_ADDED because add_param
        clears the chunk's edit layer *after* PARAM_ADDED -- running execute earlier would
        author into that layer only for it to be wiped. The load path already calls
        execute() itself, so skip while a stage is loading to avoid a double run.
        """
        if not self.initialized or not self._stage:
            return
        if chunk_notice._loading_stage:
            return
        chunk = notice.data
        if not isinstance(chunk, core.ChunkBase):
            return
        if not chunk.can_cook():
            return
        # A blocked chunk (view-driven: created out of the active view's tree) must not execute --
        # it cooks only once wired in and unblocked by recook.
        if chunk.is_blocked():
            return
        # Not blocked, but nothing drives it either: only what the view or Output reaches
        # cooks, and a new node is wired to nothing yet. Not every frontend blocks a chunk it
        # creates (paste and menu create in the engine do not), and cooked anyway, a pasted
        # Reference composed its whole asset in for a recook to hide again. Left stale -- a
        # chunk that never cooked is -- so it cooks once it is wired in.
        #
        # Not blocked here, though recook_for_view will block it: a blocked chunk sends no
        # value notices, so the values its creator sets next -- a script's, a paste's --
        # would never reach its prim or its widgets. The frontend's recook after a create
        # (chunk_graph.recook_active, Qt's _recook_active_view) blocks it once they are in.
        if not reached_by_graph_driver(chunk, self._stage, self._scene):
            return

        chunk.execute()

    def _find_dag_tail(self, context, exclude=None) -> core.ChunkBase | None:
        """The tail of the exec DAG for this context.

        That is the chunk whose outputs:chunk is not referenced by any other chunk's
        inputs:chunk. Used as prev_chunk when no interactive history exists (e.g. the first add
        after loading a .usda).
        """
        # Regular chunks in this context (never a view -- a view consumes the tail and is
        # terminal, so it must not be treated as the chain tail itself).
        context_chunks = [
            c for c in self._scene.get_chunks()
            if c.get_context() == context and c is not exclude and not is_terminal_chunk(c)
        ]
        # Also include ChunkGroup chunks (CONTEXT with a USD prim) that belong here.
        # Plain tab contexts are excluded -- they have no prim and no exec connections.
        for c in self._scene.get_chunks():
            if not isinstance(c, core.ChunkBase) or c is exclude:
                continue
            if not (c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                    and c.get_config() and c.get_config().module):
                continue
            pc = getattr(c, "_parent_context", None)
            # A Reference/Payload hosting a network turned CONTEXT in place and has no
            # _parent_context: its prim says which graph it is a node of. Missed, the chunk
            # it consumes looked like the tail, and a viewport drag of the hosted prim wired
            # its new Transform in above the Reference.
            if (pc() if pc else None) == context or (pc is None and is_node_in_context(c, context)):
                context_chunks.append(c)

        if not context_chunks:
            return None

        referenced: set[str] = set()
        for c in context_chunks:
            prim_path = (
                c.prim_path if c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                else f"{_resolve_chunk_root(c)}/{c.name}"
            )
            prim = self._stage.GetPrimAtPath(Sdf.Path(prim_path))
            if not prim.IsValid():
                continue
            # Every slot -- anything feeding a merge is referenced, whichever slot it lands in.
            for conn in exec_input_connections(prim):
                referenced.add(str(conn.GetPrimPath()))

        # Last unreferenced chunk in list order is the tail
        tail = None
        for c in context_chunks:
            prim_path = (
                c.prim_path if c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                else f"{_resolve_chunk_root(c)}/{c.name}"
            )
            if prim_path not in referenced:
                tail = c
        return tail

    def _new_chunk_bookkeeping_layer(
        self, chunk: core.ChunkBase, prev_chunk: core.ChunkBase | None
    ) -> Sdf.Layer | None:
        """Where a *brand new* chunk's bookkeeping prim should be authored, from its predecessors.

        The chunk's own prim is not wired into the chain yet, so upstream_edit_target_layer on
        it would find nothing.

        A CONTEXT chunk (container) goes to the stage's edit target, like
        get_bookkeeping_layer. Otherwise: if a predecessor is itself
        an Edit Target chunk, its published layer applies; else recurse up from that
        predecessor. With no Edit Target upstream, the stage's edit target
        (chunk_utils.host_edit_layer) -- so a chunk made while a department's sublayer is the
        target is saved in that department's file.
        """
        if not self._stage:
            return None
        host = chunk_utils.host_edit_layer(self._stage)
        if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            return host
        deps = [d for _, d in chunk.get_dependencies() if isinstance(d, core.ChunkBase)]
        if not deps and prev_chunk is not None:
            deps = [prev_chunk]
        for dep in deps:
            dep_prim = self._stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dep)))
            if not dep_prim.IsValid():
                continue
            layer = _prim_edit_target_layer(self._stage, dep_prim)
            if layer is None:
                layer = upstream_edit_target_layer(self._stage, dep_prim)
            # A layer an asset brings in by arc holds no bookkeeping: /chunks is outside
            # every asset's arc (chunk_utils.get_bookkeeping_layer).
            if layer is not None and self._stage.HasLocalLayer(layer):
                return layer
        return host

    def _define_prim(self, chunk: core.ChunkBase, prev_chunk: core.ChunkBase | None = None) -> Usd.Prim:
        # Author the prim at the exact path _on_param_added registered (chunk._exec_prim_path),
        # so the prim is CREATED where it was REGISTERED. For CONTEXT group chunks these diverge:
        # _on_param_added resolves the parent via _parent_context (e.g. "/MyTab"), but
        # _resolve_chunk_root resolves via get_context() -- None for a root-level group, giving
        # "/chunks". A create/register mismatch defeats load_chunks' dedup and recurses forever
        # (_define_prim → StageContentsChanged → load_chunks re-adds → _define_prim → …).
        exec_path = getattr(chunk, "_exec_prim_path", None)
        if exec_path:
            target_path = str(exec_path)
            chunk_root = str(Sdf.Path(target_path).GetParentPath())
        else:
            chunk_root = _resolve_chunk_root(chunk)
            target_path = f"{chunk_root}/{chunk.name}"

        # Resolve the layer this new chunk's bookkeeping should live in from its exec-chain
        # predecessor, BEFORE authoring -- the chunk isn't wired into the chain yet, so
        # nearest_upstream_edit_layer on its own (still-unconnected) prim would wrongly say
        # "session". Authoring straight into the right layer here avoids a fragile post-hoc
        # migration (and the inputs:chunk it would author would otherwise land in session and
        # need moving). All authoring must land here, never on a chunk's own anonymous execute
        # layer (Clear()ed every run, not saved).
        book_layer = self._new_chunk_bookkeeping_layer(chunk, prev_chunk)

        def book_ctx():
            if book_layer:
                return Usd.EditContext(self._stage, book_layer)
            return contextlib.nullcontext()

        with book_ctx():
            _define_chunk_parent(self._stage, chunk_root)

        # The siblings before this one, counted only when a default position is needed:
        # walking every sibling made defining a prim cost the size of its graph. Asked after
        # the prim is defined, so a prim that is new here is not one of its own siblings.
        existed = self._stage.GetPrimAtPath(Sdf.Path(target_path)).IsValid()

        def index() -> int:
            root_prim = self._stage.GetPrimAtPath(Sdf.Path(chunk_root))
            count = sum(
                1 for child in (root_prim.GetChildren() if root_prim.IsValid() else [])
                if child.GetTypeName() == _CHUNK_PRIM_TYPENAME
            )
            return count if existed else count - 1

        with book_ctx():
            prim = self._stage.DefinePrim(Sdf.Path(target_path), _CHUNK_PRIM_TYPENAME)
            # Not the session layer: nothing opens it on its own, and a def there would make
            # a container composed in from a file look owned (chunk_utils.is_composed_in).
            if book_layer is not None and book_layer != self._stage.GetSessionLayer():
                _define_containers_in(self._stage, book_layer, Sdf.Path(target_path).GetParentPath())

            # The prim's own bookkeeping attributes as one change: each write was a change
            # round of its own, with every stage listener run for it -- most of the notices
            # adding a chunk sends. The prim is defined above, outside the block: USD does
            # not compose a prim made inside one until it closes.
            with Sdf.ChangeBlock():
                ui_api = UsdUI.NodeGraphNodeAPI.Apply(prim)
                if not ui_api.GetPosAttr().HasAuthoredValue():
                    if _create_node_pos is not None:
                        ui_api.CreatePosAttr(Gf.Vec2f(*_create_node_pos))
                    else:
                        ui_api.CreatePosAttr(Gf.Vec2f(index() * _NODE_STEP, 0.0))

                config = chunk.get_config()
                # A chunk built in the interpreter has no module, and info:chunkType is how a
                # load finds what to rebuild a prim from -- so without a fallback its prim was
                # written complete, params and values and all, and then skipped on the way back
                # in. Saved as the generic `runtime` type instead, which rebuilds its params
                # from those same attributes. Never overwrites a real module's own type.
                module_name = config.module if config else ""
                # A module-less CONTEXT is not a runtime chunk: load_chunks recognises a plain
                # tab context by the ABSENCE of a type, and stamping one on would make it try to
                # rebuild the tab as a chunk inside itself.
                fallback = "" if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT) else _RUNTIME_CHUNK_TYPE
                if module_name or fallback:
                    attr = prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, custom=False)
                    attr.SetVariability(Sdf.VariabilityUniform)
                    if not attr.Get():
                        attr.Set(module_name or fallback)

                # Only a pinned chunk writes anything: unpinned is the default (see
                # is_chunk_pinned), and authoring it on every prim put a chunk:pinned = 0 line
                # in every chunk of every saved file for no information at all.
                # Views are never auto-pinned, regardless of which surface/flow created them --
                # see is_chunk_pinned's matching unconditional view special case.
                if bool(_pin_new_chunks) and not is_terminal_chunk(chunk):
                    pinned_attr = prim.CreateAttribute("chunk:pinned", Sdf.ValueTypeNames.Bool, custom=True)
                    pinned_attr.SetVariability(Sdf.VariabilityUniform)
                    if not pinned_attr.HasAuthoredValue():
                        pinned_attr.Set(True)

                config = chunk.get_config()
                if not (config and config.terminal):
                    exec_out = prim.CreateAttribute(_EXEC_OUTPUT, Sdf.ValueTypeNames.Token, custom=False)
                    exec_out.SetVariability(Sdf.VariabilityUniform)

                if config and getattr(config, "outputs", None):
                    for out_name, out_type_name in config.outputs:
                        out_type = getattr(Sdf.ValueTypeNames, out_type_name, None)
                        if out_type is not None:
                            prim.CreateAttribute(f"outputs:{out_name}", out_type, custom=False)

            chunk_gizmo.author_gizmos(self._stage, prim, chunk)

            deps = [dep for _, dep in chunk.get_dependencies() if isinstance(dep, core.ChunkBase)]
            if not deps and prev_chunk:
                deps = [prev_chunk]
            if deps:
                exec_in = prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
                exec_in.SetConnections([
                    Sdf.Path(
                        f"{dep.prim_path}.{_EXEC_OUTPUT}"
                        if dep.has_flag(ChunkFlags.GroupFlags.CONTEXT) and getattr(dep, "prim_path", None)
                        else f"{_resolve_chunk_root(dep)}/{dep.name}.{_EXEC_OUTPUT}"
                    )
                    for dep in deps
                ])
                invalidate_edit_layer_memo()
                if not prim.IsValid():
                    # Wiring this new prim's exec-chain connection just changed exec
                    # topology, and a dependency's own script can react to that reactively
                    # (e.g. layer.py's execute() re-ranks its layer against the
                    # active view on any exec-chain change) -- reentrant, before this
                    # function returns. If that reentrant run reassigns subLayerPaths (it
                    # does, to reposition itself), the resulting stage recomposition can
                    # invalidate THIS prim's own Usd.Prim handle if it composes through
                    # the same layer subtree, even though the underlying prim spec itself
                    # is untouched. Re-fetch by path rather than continuing to hold (and
                    # eventually deref) a dead handle -- chaining several Layer /
                    # Edit Target pairs does exactly this.
                    prim = self._stage.GetPrimAtPath(Sdf.Path(target_path))

            # The chunk's own values, onto its own prim: as a suppressed write, so each one is
            # not read straight back into the params it came from, nor a scan for new chunk
            # prims run per attribute -- most of the cost of adding a chunk to a big graph.
            prev_write = chunk_notice._usd_write_active
            chunk_notice._usd_write_active = True
            try:
                # One change for all of them, and closed while the write is still suppressed:
                # the notice goes out when the block ends, and must be ignored the same way.
                with Sdf.ChangeBlock():
                    self._author_param_values(prim, chunk)
            finally:
                chunk_notice._usd_write_active = prev_write

        return prim

    def _author_param_values(self, prim: Usd.Prim, chunk: core.ChunkBase) -> None:
        """Write the values chunk's params already hold onto its new prim (_define_prim).

        TRANSIENT params are left off, as every other writer leaves them: a Layer chunk's
        anon layer id written here was saved, and a reopen in the same process found the
        closed scene's layer by it -- leftover sublayers and all -- and exported them.
        """
        for path, param in _iter_chunk_params(chunk):
            raw = param.raw_value
            if raw is None or is_transient_param(param):
                continue
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            sdf_type = param_sdf_type(param)
            if sdf_type is None:
                continue
            backing_path = getattr(param, '_param_attr_path', None)
            if not is_expr:
                if backing_path:
                    backing = self._stage.GetAttributeAtPath(Sdf.Path(backing_path))
                    if not (backing and backing.HasAuthoredValue()):
                        continue
                else:
                    default = getattr(param, 'default_value', None)
                    if raw == default:
                        continue
            prefix = "outputs:" if param.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
            attr_name = f"{prefix}{path.replace('.', ':')}"
            attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
            if is_expr:
                attr.SetConnections(self._expr_connections(prim, raw))
                chunk_utils.store_expression_text(attr, raw)
            elif not attr.Get():
                attr.Set(raw)

        for vec_path, vector in _iter_chunk_vectors(chunk):
            real_path = getattr(vector, '_param_attr_path', None)
            if not real_path:
                # Not bound yet, so the vector alone holds a value set before the chunk
                # joined the scene (a script's create(), a menu preset) -- the same as an
                # unbound scalar above.
                if vector.has_flag(ChunkFlags.ParamFlags.OUTPUT) or vector.value == vector.default_value:
                    continue
                sdf_type = param_sdf_type(vector)
                if sdf_type is None:
                    continue
                attr = prim.CreateAttribute(f"{_INPUTS_PREFIX}{vec_path.replace('.', ':')}", sdf_type, custom=False)
                if not attr.Get():
                    attr.Set(chunk_utils.as_attr_value(attr, vector.value))
                continue
            real_attr = self._stage.GetAttributeAtPath(Sdf.Path(real_path))
            if not (real_attr and real_attr.IsValid()):
                continue
            if not real_attr.HasAuthoredValue():
                continue
            current_val = real_attr.Get()
            if current_val is None:
                continue
            attr_name = f"{_INPUTS_PREFIX}{vec_path.replace('.', ':')}"
            attr = prim.CreateAttribute(attr_name, real_attr.GetTypeName(), custom=False)
            if not attr.Get():
                attr.Set(current_val)

    def _wire_inputs(self, prim: Usd.Prim, chunk: core.ChunkBase) -> None:
        _load_wire_inputs(prim, chunk)


# ---------------------------------------------------------------------------
# Authoring on a chunk's own prim -- the public face of ChunkStageSync, for chunk scripts
# ---------------------------------------------------------------------------


def get_chunk_prim(chunk: core.ChunkBase) -> Usd.Prim | None:
    """Chunk's prim on the live stage, or None if the session has none for it yet."""
    sync = ChunkStageSync()
    if not sync.initialized or not sync._stage:
        return None
    prim_path = sync._chunk_prim_paths.get(id(chunk))
    if not prim_path:
        return None
    prim = sync._stage.GetPrimAtPath(Sdf.Path(prim_path))
    return prim if prim and prim.IsValid() else None


def chunk_prim_edit_context(chunk: core.ChunkBase | None = None) -> Usd.EditContext:
    """An edit context for authoring on chunk's prim: ports, connections, new child chunks.

    Targets the layer chunk's bookkeeping belongs in, not the layer its cooked output goes
    to, so what is authored inside it is saved with the scene.

    Args:
        chunk: The chunk whose prim is authored on.

    Returns:
        The edit context.
    """
    return ChunkStageSync()._chunk_prim_edit_context(chunk)


def clear_attr_connections(attr: Usd.Attribute) -> None:
    """Remove attr's connections from every layer that has one, not just the edit target."""
    ChunkStageSync().clear_attr_connections(attr)


def unwire_param(param) -> None:
    """Drop a param's incoming wire and put it back to its default.

    For a param the chunk has just hidden: a wire kept on it would come back, with
    possibly the wrong meaning, when the param is shown again. A param with no wire is
    left alone.

    Args:
        param: The param, or None.
    """
    if param is None:
        return
    raw = getattr(param, "raw_value", None)
    if not (isinstance(raw, str) and chunk_utils.has_expression(raw)):
        return
    param.value = param.default_value
    if attr := param.get_usd_prop():
        clear_attr_connections(attr)


def delete_attr_specs(prim: Usd.Prim, attr_name: str) -> None:
    """Remove attr_name from prim in every layer that has it, not just the edit target."""
    sync = ChunkStageSync()
    if sync.initialized and sync._stage:
        sync._delete_stale_attr_specs(prim, attr_name)


def _patch_expressions(stage: Usd.Stage, old: str, new: str) -> None:
    """Replace all occurrences of old with new in string attributes across the stage.

    Each chunk-prim bookkeeping attr (the expression strings this patches) can now live
    in a different layer -- whichever was the active Edit Target when it was first
    authored -- rather than always the session layer, so there's no single EditContext
    that's correct for every attr in this traversal. Patch each one wherever its
    existing spec already lives instead (see chunk_utils.get_attr_home_layer); writing
    to the stage's ambient edit target (unscoped) would author a weaker opinion that
    gets silently overridden by the original in composition.
    """
    for prim in stage.TraverseAll():
        # Expressions live on chunk prims only. Reading every attribute of every scene prim
        # to find that out made a rename cost a pass over the whole scene's data, and could
        # rewrite a scene string that merely contained "@Name." by coincidence.
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
            continue
        for attr in prim.GetAttributes():
            val = attr.Get()
            # A non-String attr keeps its expression text in customData rather than as its
            # value (chunk_utils.store_expression_text), so patch that too -- otherwise
            # renaming a chunk leaves every numeric param wired to it holding the old name.
            text = attr.GetCustomDataByKey(chunk_utils.EXPR_TEXT_KEY)
            hit_val = isinstance(val, str) and old in val
            hit_text = isinstance(text, str) and old in text
            if not (hit_val or hit_text):
                continue
            home = chunk_utils.get_attr_home_layer(attr)
            if not home:
                continue
            with Usd.EditContext(stage, home):
                if hit_val:
                    attr.Set(val.replace(old, new))
                if hit_text:
                    attr.SetCustomDataByKey(chunk_utils.EXPR_TEXT_KEY, text.replace(old, new))


def _patch_param_expressions(chunks, old: str, new: str) -> None:
    """Replace old with new in the expression text of every loaded param.

    _patch_expressions fixes the USD; this fixes the params already in memory, which
    otherwise keep the old name and stop resolving until the scene is reloaded.

    Args:
        chunks: The chunks to patch, nested ones included.
        old: The text to replace.
        new: The replacement.
    """
    for c in chunks:
        for param in core.iter_params_and_rows(c):
            raw = getattr(param, "raw_value", None)
            if isinstance(raw, str) and old in raw:
                param.value = raw.replace(old, new)


def _define_chunk_parent(stage: Usd.Stage, path: str) -> Usd.Prim:
    """Define the parent prim a chunk hangs under, typed for what it actually is.

    The /chunks root is a Scope; every other parent is a chunkgroup's own Chunk prim.
    """
    typename = _CHUNKS_ROOT_TYPENAME if str(path) == _CHUNKS_ROOT else _CHUNK_PRIM_TYPENAME
    return stage.DefinePrim(Sdf.Path(path), typename)


def _define_containers_in(stage: Usd.Stage, layer: Sdf.Layer, path: Sdf.Path) -> None:
    """Give path and every prim above it a `def` in layer, typed as it composes.

    A chunk made while a department's sublayer is the edit target is defined in that file,
    but its chunkgroup may be defined in another -- and DefinePrim then leaves only bare
    `over`s above it, so the file opened on its own has chunks under nothing. Each
    container is given its identity there too: specifier, type and info:chunkType, which
    never change, so repeating them in several files cannot go stale.

    Args:
        stage: The stage, for what each prim composes to.
        layer: The layer the chunk was just defined in.
        path: The chunk's parent path.
    """
    if path.isEmpty or path == Sdf.Path.absoluteRootPath:
        return
    with Sdf.ChangeBlock():
        for prefix in path.GetPrefixes():
            composed = stage.GetPrimAtPath(prefix)
            if not (composed and composed.IsValid()):
                return
            spec = layer.GetPrimAtPath(prefix) or Sdf.CreatePrimInLayer(layer, prefix)
            if spec.specifier != Sdf.SpecifierDef:
                spec.specifier = Sdf.SpecifierDef
            if not spec.typeName and composed.GetTypeName():
                spec.typeName = composed.GetTypeName()
            type_attr = composed.GetAttribute(_CHUNK_TYPE_ATTR)
            chunk_type = type_attr.Get() if type_attr and type_attr.IsValid() else None
            if chunk_type and _CHUNK_TYPE_ATTR not in spec.attributes:
                attr_spec = Sdf.AttributeSpec(
                    spec, _CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, Sdf.VariabilityUniform
                )
                attr_spec.default = chunk_type


def exec_output_path(chunk: core.ChunkBase) -> str:
    """Return the outputs:chunk attribute path an exec connection to chunk must point at.

    A ChunkGroup is its own prim root, so its exec output lives on the group prim itself
    (/Tab1/Group.outputs:chunk) -- NOT under the root it provides for its children, which is
    what _resolve_chunk_root returns and would give the nonexistent
    /Tab1/Group/Group.outputs:chunk.

    Args:
        chunk (core.ChunkBase): The upstream chunk being connected from

    Returns:
        str: The absolute outputs:chunk attribute path
    """
    if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT) and getattr(chunk, "prim_path", None):
        return f"{chunk.prim_path}.{_EXEC_OUTPUT}"
    return f"{_resolve_chunk_root(chunk)}/{chunk.name}.{_EXEC_OUTPUT}"


def is_node_in_context(chunk: core.ChunkBase, context: core.ChunkBase | None) -> bool:
    """Whether chunk is a NODE in context's graph: its prim is a direct child of the context's root.

    That matches how the graph rebuild picks nodes, and separates 'group as a node' from
    'group as a container': a group is a node in its PARENT graph (its prim's parent is the
    parent root), and the group's inner chunks are nodes in the group's graph. get_context()
    would conflate the two, since it returns self for a context chunk.

    Args:
        chunk: The chunk.
        context: The context, or None for the top level (always True).

    Returns:
        True if chunk is a node of that graph.
    """
    if context is None:
        return True
    root = context.prim_path if getattr(context, "prim_path", None) else _CHUNKS_ROOT
    return str(Sdf.Path(get_chunk_prim_path(chunk)).GetParentPath()) == str(root)


def is_view_chunk(chunk: core.ChunkBase) -> bool:
    """Return True if chunk is a view-type chunk."""
    cfg = chunk.get_config()
    return bool(cfg and cfg.module == "view")


def is_output_chunk(chunk: core.ChunkBase) -> bool:
    """Return True if chunk is a graph's Output: what the graph delivers and cooks to."""
    cfg = chunk.get_config()
    return bool(cfg and cfg.module == "output")


def is_terminal_chunk(chunk: core.ChunkBase) -> bool:
    """Return True for a chain-ending marker (a view or an Output), which has no content.

    Read from the module's config, so any terminal chunk is never taken for a chain's tail,
    cooked, pinned or copied.
    """
    cfg = chunk.get_config()
    return bool(cfg and cfg.terminal)


def find_output_chunk(
    stage: Usd.Stage, scene: chunk_scene.SceneSender, context: core.ChunkBase | None
) -> core.ChunkBase | None:
    """The Output of context's graph, or None if it has none.

    A group's Output is its own, never the parent graph's (prim-path membership). For the
    top level (context None) the shallowest Output wins, so a group's inner Output is never
    taken for the file's.

    Args:
        stage: The stage.
        scene: The scene.
        context: The graph to look in, or None for the top level.

    Returns:
        The Output chunk, or None.
    """
    outputs = [c for c in scene.get_chunks() if is_output_chunk(c)]
    if context is not None:
        return next((c for c in outputs if is_node_in_context(c, context)), None)
    return min(outputs, key=lambda c: Sdf.Path(get_chunk_prim_path(c)).pathElementCount, default=None)


def _layer_strength(stage: Usd.Stage) -> dict[str, int]:
    """Layer identifier -> position in the stage's layer stack, 0 the strongest."""
    return {layer.identifier: i for i, layer in
            enumerate(stage.GetLayerStack(includeSessionLayers=True))}


def graph_outputs(
    stage: Usd.Stage, scene: chunk_scene.SceneSender, context: core.ChunkBase | None
) -> list[core.ChunkBase]:
    """Every Output of a graph, weakest layer first.

    A shot sublayering its departments composes each department's Output into one graph.
    Delivering the graph cooks all of them, weakest first: implied edges (implied_sources)
    usually chain them already, but a stronger layer can wire a weaker chunk after its own
    and cut that chain, and the weaker department still delivers.

    Args:
        stage: The stage.
        scene: The scene.
        context: The graph, or None for the top level (the shallowest Outputs).

    Returns:
        The Outputs, weakest layer first; empty when the graph has none.
    """
    outputs = [c for c in scene.get_chunks() if is_output_chunk(c)]
    if context is not None:
        outputs = [c for c in outputs if is_node_in_context(c, context)]
    elif outputs:
        depth = min(Sdf.Path(get_chunk_prim_path(c)).pathElementCount for c in outputs)
        outputs = [c for c in outputs if Sdf.Path(get_chunk_prim_path(c)).pathElementCount == depth]
    if len(outputs) < 2:
        return outputs
    strength = _layer_strength(stage)

    def rank(output: core.ChunkBase) -> int:
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(output)))
        layer = chunk_utils.chunk_pass(prim) if prim and prim.IsValid() else None
        return strength.get(layer.identifier, len(strength)) if layer is not None else len(strength)

    return sorted(outputs, key=rank, reverse=True)


def reached_by_graph_driver(
    chunk: core.ChunkBase, stage: Usd.Stage, scene: chunk_scene.SceneSender
) -> bool:
    """Whether what drives chunk's graph reaches it, so that it is one the graph cooks.

    The driver is the graph's active view, or with none active its Outputs -- what
    recook_for_view cooks. A graph with neither a view nor an Output is not view-driven at
    all (a script building one, a headless run), and everything in it cooks.

    Args:
        chunk: The chunk.
        stage: The stage.
        scene: The scene.

    Returns:
        True if chunk is in its graph's cook set, or its graph is not view-driven.
    """
    context = chunk.get_owner()
    while context is not None and not (
            isinstance(context, core.ChunkBase) and context.has_flag(ChunkFlags.GroupFlags.CONTEXT)):
        context = context.get_owner()
    views = [c for c in scene.get_chunks() if is_view_chunk(c) and is_node_in_context(c, context)]
    outputs = graph_outputs(stage, scene, context)
    if not views and not outputs:
        return True
    slot = None
    if context is not None and (prim := stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(context)))):
        attr = prim.GetAttribute("chunk:activeView")
        slot = attr.Get() if attr and attr.IsValid() else None
    view = find_view_chunk_for_slot(slot, stage, scene, context) if slot else None
    drivers = [view] if view is not None else outputs
    return any(chunk in view_cook_list(driver, stage, scene) for driver in drivers)


def strongest_output(
    stage: Usd.Stage, scene: chunk_scene.SceneSender, context: core.ChunkBase | None
) -> core.ChunkBase | None:
    """The Output of a graph from its strongest layer, or None when it has none."""
    outputs = graph_outputs(stage, scene, context)
    return outputs[-1] if outputs else None


def _arc_network_outputs(stage: Usd.Stage) -> list[tuple[Usd.Prim, Sdf.Layer]]:
    """The Outputs of networks composed in by an arc, and the layer that brings each in.

    One per arc, whichever layer authors it: a chair the house references inside its own
    network is a network of its own. The shallowest Outputs under each arc, the network's
    own rather than those of groups inside it. A network hosted by a chunk (a Reference
    chunk pointed at a network file) is left out: that chunk is a node in the graph and is
    wired like one. Read from the prim index (chunk_utils.arc_source), so every arc counts.
    """
    registry = chunk_registry.ChunkRegistry()
    scene = registry.scene if registry.initialized else None
    if scene is None:
        return []
    by_arc: dict[Sdf.Path, list[tuple[Usd.Prim, Sdf.Layer]]] = {}
    for chunk in scene.get_chunks():
        if not is_output_chunk(chunk):
            continue
        out = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
        if not (out and out.IsValid()) or (source := chunk_utils.arc_source(out)) is None:
            continue
        home, layer = source
        home_prim = stage.GetPrimAtPath(home)
        if home_prim and home_prim.GetAttribute(_CHUNK_TYPE_ATTR).Get():
            continue
        by_arc.setdefault(home, []).append((out, layer))
    found = []
    for outs in by_arc.values():
        depth = min(o.GetPath().pathElementCount for o, _ in outs)
        found.extend((o, lyr) for o, lyr in outs if o.GetPath().pathElementCount == depth)
    return found


# parent prim path -> {child name: whether it is an Output chunk}, for the stage sync's stage.
# Dropped by ChunkStageSync._forget_output_children as USD reports children resynced.
_output_children_known: dict[str, dict[str, bool]] = {}


def _output_children(parent: Usd.Prim) -> list[Usd.Prim]:
    """The Output chunk prims directly under parent.

    implied_sources asks this of a chunk's parent every time it runs, which is several times
    per chunk wired; reading the chunk type of every sibling made each ask cost the size of
    the graph. Remembered per child for the stage sync's stage, so only children that
    appeared since are read; a child resynced (made, deleted, recomposed) is forgotten by
    the sync as USD reports it.
    """
    stage = parent.GetStage()
    sync = ChunkStageSync()
    if not (sync.initialized and getattr(sync, "_stage", None) == stage):
        return [child for child in parent.GetChildren()
                if child.GetAttribute(_CHUNK_TYPE_ATTR).Get() == "output"]
    known = _output_children_known.setdefault(str(parent.GetPath()), {})
    found = []
    for name in parent.GetChildrenNames():
        is_output = known.get(name)
        child = None
        if is_output is None:
            child = parent.GetChild(name)
            is_output = known[name] = child.GetAttribute(_CHUNK_TYPE_ATTR).Get() == "output"
        if is_output:
            found.append(child or parent.GetChild(name))
    return found


def implied_sources(prim: Usd.Prim) -> list[str]:
    """The chunks a chunk is fed by because of composition, not a wire.

    A chunk with no exec input starts where everything weaker than it ends:

    - A shot sublayers its departments, each a chain ending in its Output, and nothing is
      wired between them. A first chunk in lighting.usda starts where layout.usda's chain
      ends, as if wired to what feeds layout's Output -- the nearest weaker layer beside it
      (its sibling Outputs) that brings one.
    - A network composed in by an arc (a reference, payload, inherit or variant authored in the
      chunk's own layer or a weaker one) cooks before it too, so a shot's chain can read
      the referenced house it builds on.

    These edges are derived from composition on every read and never authored -- the same
    reading gives the cook order (view_cook_list), live change propagation
    (_wired_dependencies) and the load, and nothing is written to any file.

    Args:
        prim: A chunk prim.

    Returns:
        Prim paths of the chunks feeding those Outputs; empty for a chunk with an exec
        input, a terminal chunk, a chunk composed in by an arc itself, or one with nothing
        weaker to follow.
    """
    if not (prim and prim.IsValid()) or exec_input_connections(prim):
        return []
    if prim.GetAttribute(_CHUNK_TYPE_ATTR).Get() in ("view", "output"):
        return []
    # Cheapest first: with fewer than two Outputs beside it and no network brought in by an
    # arc there is nothing to chain -- nearly every graph -- so no layer is resolved at all.
    siblings = _output_children(prim.GetParent())
    stage = prim.GetStage()
    arc_outputs = _arc_network_outputs(stage)
    if len(siblings) < 2 and not arc_outputs:
        return []
    own = chunk_utils.chunk_pass(prim)
    if own is None:
        return []
    strength = _layer_strength(stage)
    own_rank = strength.get(own.identifier)
    if own_rank is None:
        return []
    ends = []
    weaker = []
    for output in siblings:
        layer = chunk_utils.chunk_pass(output)
        rank = strength.get(layer.identifier) if layer is not None else None
        if rank is not None and rank > own_rank:
            weaker.append((rank, output))
    if weaker:
        ends.append(min(weaker, key=lambda pair: pair[0])[1])
    for output, arc_layer in arc_outputs:
        rank = strength.get(arc_layer.identifier)
        if rank is not None and rank >= own_rank:
            ends.append(output)
    # Never one that already follows this chunk through a wire: a stronger layer wiring a
    # weaker chunk after its own (an `over` on its exec input) pushes that chunk later, and
    # waiting for it here would close a loop.
    own_path = str(prim.GetPath())
    sources: list[str] = []
    for end in ends:
        for conn in exec_input_connections(end):
            path = str(conn.GetPrimPath())
            if path not in sources and not _wired_after(stage, path, own_path):
                sources.append(path)
    return sources


def _wired_after(stage: Usd.Stage, path: str, ancestor: str) -> bool:
    """Whether the chunk at path reads, through wires alone, from the one at ancestor."""
    seen: set[str] = set()
    todo = [path]
    while todo:
        current = todo.pop()
        if current == ancestor:
            return True
        if current in seen:
            continue
        seen.add(current)
        prim = stage.GetPrimAtPath(Sdf.Path(current))
        if prim and prim.IsValid():
            todo.extend(_connected_source_paths(prim))
    return False


def is_group_chunk(chunk: core.ChunkBase) -> bool:
    """Whether chunk owns child chunks: a CONTEXT chunk with a module.

    This is about *containment*: whose children are hidden until entered, cook as a unit,
    and share a layer. It is true of a chunkgroup, and equally true of a chunk-in-chunk
    like AMDMaterialXLibrary, whose children are an implementation detail. For the
    narrower "is this a subnet the user may navigate into", see is_chunkgroup.

    Args:
        chunk: The chunk.

    Returns:
        True for a chunk that contains others.
    """
    cfg = chunk.get_config()
    return bool(chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT) and cfg and cfg.module)


def is_chunkgroup(chunk: core.ChunkBase) -> bool:
    """Whether chunk is a chunkgroup proper -- the `chunkgroup` chunk type.

    A chunkgroup is defined in USD and is a subnet you navigate into; a chunk is defined in
    python, and one that owns children (AMDMaterialXLibrary creating `reference` and
    `bind_material`) is reusing them as an implementation detail, not exposing a subnet to
    edit. Both carry GroupFlags.CONTEXT, so CONTEXT alone cannot tell them apart.

    Navigation, and the UI that advertises it, keys on this. Containment and cooking key on
    is_group_chunk, so a chunk-in-chunk keeps hiding its children and running their code.

    Args:
        chunk: The chunk.

    Returns:
        True for a chunkgroup.
    """
    cfg = chunk.get_config()
    return bool(cfg and cfg.module == "chunkgroup")


def is_network_host(chunk: core.ChunkBase) -> bool:
    """Whether chunk brings a chunk network in by reference and hosts it as a subnet.

    A Reference chunk pointed at a file whose target prim is a chunkgroup does not put
    that prim into the scene; it puts the reference arc on its OWN chunk prim
    (host_network), so the group's chunks compose in as its children and the node is the
    group -- dive in, and the network is there, as if it had been expanded in place. The
    chunk carries CONTEXT while it hosts (so containment and cooking treat it as a group,
    see is_group_chunk) and the arc on its prim is what tells it apart from a chunk that
    builds its own children (AMD MaterialX Library), which is CONTEXT too.

    Args:
        chunk: The chunk.

    Returns:
        True for a chunk hosting a network.
    """
    if not is_group_chunk(chunk) or is_chunkgroup(chunk):
        return False
    stage = chunk.get_scene_stage()
    if not stage:
        return False
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    return bool(prim and prim.IsValid() and _hosts_by_arc(prim))


def _hosts_by_arc(prim: Usd.Prim) -> bool:
    """Whether prim carries the arc a host authors: a reference or a payload."""
    return prim.HasAuthoredReferences() or prim.HasAuthoredPayloads()


def is_subnet(chunk: core.ChunkBase) -> bool:
    """Whether chunk is a graph the user may dive into.

    That is a chunkgroup, or a chunk hosting a referenced network (is_network_host). This is
    what navigation keys on; is_group_chunk (containment and cooking) is wider and includes
    chunks whose children are an implementation detail.

    Args:
        chunk: The chunk.

    Returns:
        True for a subnet.
    """
    return is_chunkgroup(chunk) or is_network_host(chunk)


#: The location suggest_location_from_file last put on each chunk -- so it knows whether the
#: location still follows the file, or has been chosen since.
_suggested_locations: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def suggest_location_from_file(param) -> None:
    """Fill a Reference or Payload chunk's primpath_location from its file_path.

    Called when file_path changes: the location becomes /<file name>, made a valid prim
    name and unique on the stage. A location holding an expression is left alone. A
    network file is hosted, not placed: its location field is the namespace the
    network's prim paths resolve under, and the default is none -- the paths as the
    sequence wrote them. Suggesting /<file> there would namespace every hosted network.

    Only a location that still follows the file is replaced: an empty one, or the one this
    last suggested. One typed in, set by a script or loaded from a saved network is a
    choice, and a new file does not move it -- `usdchunks run net.usda
    Reference.file_path=other.usd` (and every run of a batch) swaps the asset, not where
    the network put it. A glob names no one file, so it suggests nothing either.

    Args:
        param: The param that changed (file_path, or another param of the chunk).
    """
    chunk = param.get_chunk()
    if param.name == "file_path" and param.value:
        path = param.value
    else:
        file_param = chunk.get_param("file_path") if chunk else None
        path = file_param.value if file_param else None
    if not path:
        return

    location = chunk.get_param("primpath_location") if chunk else None
    if not location or chunk_utils.has_expression(location.raw_value or ""):
        return

    import_param = chunk.get_param("import_primpath")
    if network_target(path, import_param.value if import_param else ""):
        if location.raw_value:
            location.value = ""
        return

    current = location.raw_value or ""
    if chunk_glob.is_pattern(path) or (current and current != _suggested_locations.get(chunk)):
        return
    name = Tf.MakeValidIdentifier(Path(path).stem)
    location.value = _suggested_locations[chunk] = chunk_utils.get_unique_path(param.get_scene_stage(), f"/{name}")


def network_target(asset_path: str, import_path: str = "") -> str | None:
    """The prim path of the chunkgroup a referenced file offers, or None for scene content.

    Resolved the way the Reference chunk resolves its target: the explicit import path,
    else the layer's defaultPrim, else its single root prim. A target is a network when
    it composes to a Chunk of type chunkgroup -- the shape export_chunks writes. That
    shape puts the group under a /chunks scope, so a target that is the scope itself
    stands for the first chunkgroup in it: pointing a Reference at a saved network file,
    with no import path, hosts the network it holds.

    Composed, not read off the one layer's specs: a file may hold its network only
    through its own sublayers (a sequence file that stacks a layout file and a lighting
    file), and hosting composes the whole of it in anyway. Opened with a population mask
    of just the candidate paths, so asking a heavy asset file whether it is a network
    costs its chunk prims, not its geometry.

    Args:
        asset_path: The referenced file.
        import_path: The prim path the reference targets, or "" for the file's default.

    Returns:
        The chunkgroup's prim path in the file, or None.
    """
    if not asset_path:
        return None
    try:
        layer = Sdf.Layer.FindOrOpen(asset_path)
    except Tf.ErrorException:
        # A file this USD build has no format plugin for (a .mtlx without the MaterialX
        # plugin). Not a network, and not this function's error to raise: the chunk
        # that goes on to reference the file reports it, as it always has.
        return None
    if layer is None:
        return None
    if import_path:
        candidates = [Sdf.Path(import_path)]
    elif layer.defaultPrim:
        candidates = [Sdf.Path.absoluteRootPath.AppendChild(layer.defaultPrim)]
    else:
        # The layer's own roots, plus the scope a network lives under, which the layer
        # may only get from a sublayer.
        candidates = [spec.path for spec in layer.rootPrims]
        if Sdf.Path(_CHUNKS_ROOT) not in candidates:
            candidates.append(Sdf.Path(_CHUNKS_ROOT))

    # The layer's own specs first: free, and the answer for every file export_chunks
    # wrote. Composition only when the layer stacks sublayers the answer could be in --
    # composing a big asset's default prim to learn it is not a network would cost that
    # asset's whole hierarchy on every cook of a plain Reference.
    def spec_is_group(prim_spec) -> bool:
        if prim_spec is None or prim_spec.typeName != _CHUNK_PRIM_TYPENAME:
            return False
        attr = prim_spec.attributes.get(_CHUNK_TYPE_ATTR)
        return attr is not None and attr.default == "chunkgroup"

    for path in candidates:
        spec = layer.GetPrimAtPath(path)
        if spec_is_group(spec):
            return str(path)
        if spec is not None and spec.typeName == _CHUNKS_ROOT_TYPENAME:
            for child in spec.nameChildren.values():
                if spec_is_group(child):
                    return str(child.path)
    if not layer.subLayerPaths:
        return None
    try:
        composed = Usd.Stage.OpenMasked(
            layer, Usd.StagePopulationMask([str(c) for c in candidates]), Usd.Stage.LoadNone
        )
    except Tf.ErrorException:
        return None

    def is_group(prim) -> bool:
        if not prim or not prim.IsValid() or prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
            return False
        return prim.GetAttribute(_CHUNK_TYPE_ATTR).Get() == "chunkgroup"

    for path in candidates:
        prim = composed.GetPrimAtPath(path)
        if is_group(prim):
            return str(path)
        if prim and prim.IsValid() and prim.GetTypeName() == _CHUNKS_ROOT_TYPENAME:
            for child in prim.GetFilteredChildren(_LOAD_INCLUDE_INACTIVE):
                if is_group(child):
                    return str(child.GetPath())
    return None


def host_network(chunk: core.ChunkBase, asset_path: str, prim_path: str,
                 arc: str = "reference", namespace: str = "") -> None:
    """Make chunk the host of the network at prim_path in asset_path. See host_networks."""
    host_networks(chunk, [(asset_path, prim_path)], arc, namespace)


def host_networks(chunk: core.ChunkBase, targets: list[tuple[str, str]],
                  arc: str = "reference", namespace: str = "") -> None:
    """Make chunk the host of these networks, each an (asset path, prim path) pair.

    Authors the arcs on chunk's own prim -- in its bookkeeping layer, since an arc is
    structure that must survive a cook and a save, not content -- and gives the chunk
    the flags a group has, so the composed-in children nest under it and cook as its
    subnet (recook_for_view recurses into a group's inner view). The children are then
    loaded straight away: the arc write lands while a cook has notices suppressed, and
    the rescan that would otherwise pick them up waits for the next unsuppressed stage
    change, which may be a while.

    Several targets compose together under the one prim, first strongest -- the order a
    Sublayer chunk's list has, and what stacking those files as sublayers would do to
    their chunk prims too: a chunk both networks name is one chunk with the stronger
    file's opinions on top.

    Idempotent: re-hosting the same targets rewrites the same arcs and loads nothing new.

    Args:
        chunk: The hosting chunk.
        targets: (asset path, prim path) pairs, strongest first. An empty list unhosts.
        arc: "reference" or "payload": the kind the hosting chunk stands for. A payload
            host's network unloads with the stage's other payloads, and its arc on the prim
            is what a reader sees, so a Payload chunk must not quietly become a reference.
        namespace: The scene root the network's prim paths resolve under; "" means the
            paths as written, which is the sequence-into-shot case. A network's chunks
            author absolute paths -- /Clock_01 -- so two hosts of the same room would fight
            over the same prims; with a namespace each host's chunks resolve their
            prim-path params under its root (ChunkParamBase.network_namespace), and
            /Clock_01 in the file is /bedroom/Clock_01 in the scene. The file and the
            params' stored text are untouched.

    Raises:
        ValueError: `arc` is not "reference" or "payload".
    """
    if arc not in ("reference", "payload"):
        raise ValueError(f"arc must be 'reference' or 'payload', not {arc!r}")
    if not targets:
        unhost_network(chunk)
        return
    stage = chunk.get_scene_stage()
    if not stage:
        return
    # The path first: get_chunk_prim_path answers a CONTEXT chunk from its prim_path, so
    # once the flags are on it needs the path to already be recorded.
    own_path = get_chunk_prim_path(chunk)
    prim = stage.GetPrimAtPath(Sdf.Path(own_path))
    if not prim.IsValid():
        return
    chunk.prim_path = own_path
    object.__setattr__(chunk, "_exec_prim_path", own_path)
    namespace = namespace.rstrip("/") if namespace else ""
    namespace_changed = getattr(chunk, "_network_namespace", "") != namespace
    object.__setattr__(chunk, "_network_namespace", namespace)
    for flag in (ChunkFlags.GroupFlags.CONTEXT, ChunkFlags.GroupFlags.COLLAPSABLE,
                 ChunkFlags.GroupFlags.LABELED):
        chunk.set_flag(flag)
    if arc == "payload":
        arcs = [Sdf.Payload(asset, Sdf.Path(target)) for asset, target in targets]
        current = prim.GetMetadata("payload")
    else:
        arcs = [Sdf.Reference(asset, Sdf.Path(target)) for asset, target in targets]
        current = prim.GetMetadata("references")
    if current is not None and list(current.GetAddedOrExplicitItems()) == arcs:
        if namespace_changed:
            # Same network, new root: every prim path inside resolves somewhere else now,
            # and the content authored under the old root is only cleared by a re-cook.
            scene = _active_scene()
            inside = [c for c in (scene.get_chunks() if scene else [])
                      if get_chunk_prim_path(c).startswith(own_path + "/")]
            cooked = 0
            for c in sorted(inside, key=lambda c: get_chunk_prim_path(c).count("/")):
                if not c.is_blocked() and not is_group_chunk(c):
                    c.execute()
                    cooked += 1
            logger.info(
                "%s: network now under %r; re-cooked %d of %d inner chunks (%d blocked)",
                own_path, namespace or "/", cooked, len(inside),
                sum(1 for c in inside if c.is_blocked()),
            )
        return
    logger.info("%s hosts %s under %r", own_path, [asset for asset, _ in targets], namespace or "/")
    sync = ChunkStageSync()
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        with sync._chunk_prim_edit_context(chunk):
            if arc == "payload":
                prim.GetReferences().ClearReferences()
                prim.GetPayloads().SetPayloads(arcs)
            else:
                prim.GetPayloads().ClearPayloads()
                prim.GetReferences().SetReferences(arcs)
    finally:
        chunk_notice._usd_write_active = prev
    load_chunks(stage, sync._scene or _active_scene(), sync._loaded_prim_paths,
                prims=iter(prim.GetFilteredChildren(_LOAD_INCLUDE_INACTIVE)))


def unhost_network(chunk: core.ChunkBase) -> None:
    """Drop a hosted network: clear the arc from chunk's prim and the group flags from the chunk.

    The composed-in children disappear with the arc and the stage-change path tears their
    chunk objects down, the same as any prim vanishing from the stage.

    Args:
        chunk: The hosting chunk.
    """
    stage = chunk.get_scene_stage()
    if not stage or not is_network_host(chunk):
        return
    own_path = get_chunk_prim_path(chunk)
    prim = stage.GetPrimAtPath(Sdf.Path(own_path))
    sync = ChunkStageSync()
    scene = _active_scene()
    # The children the arc composed in, gathered while their prims still tell us so; the
    # arc clear runs with notices suppressed, so the stage-change path will not see them go.
    inside = [c for c in (scene.get_chunks() if scene else [])
              if get_chunk_prim_path(c).startswith(own_path + "/")]
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        with sync._chunk_prim_edit_context(chunk):
            prim.GetReferences().ClearReferences()
            prim.GetPayloads().ClearPayloads()
    finally:
        chunk_notice._usd_write_active = prev
    was_reloading = sync._reloading
    sync._reloading = True
    try:
        for c in sorted(inside, key=lambda c: get_chunk_prim_path(c).count("/"), reverse=True):
            if not stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c))).IsValid():
                c.request_delete()
    finally:
        sync._reloading = was_reloading
    for flag in (ChunkFlags.GroupFlags.CONTEXT, ChunkFlags.GroupFlags.COLLAPSABLE,
                 ChunkFlags.GroupFlags.LABELED):
        chunk.unset_flag(flag)
    object.__setattr__(chunk, "_network_namespace", "")


def param_override_source(param: core.ChunkParamBase) -> Sdf.Layer | None:
    """The composed-in layer whose opinion this param's value overrides, or None.

    A param on a chunk that arrived by composition (a hosted network, a sublayered
    sequence) shows the file's value until the shot writes its own; then the storage
    attr has an opinion in an owned layer sitting over one from a foreign layer or across
    an arc. That weaker opinion's layer is the answer -- what a UI names in "overrides
    seq.usda" and what revert_param_override puts back. A local opinion with nothing
    composed in beneath it is not an override, just a value.

    Args:
        param: The param.

    Returns:
        The overridden layer, or None.
    """
    attr = _storage_attr(param)
    if attr is None:
        return None
    stage = attr.GetStage()
    local = False
    for spec in attr.GetPropertyStack(Usd.TimeCode.Default()):
        composed_in = spec.path != attr.GetPath() or chunk_utils.is_foreign_layer(spec.layer, stage)
        if composed_in:
            return spec.layer if local else None
        local = True
    return None


def param_locked_by(param: core.ChunkParamBase) -> Sdf.Layer | None:
    """The locked layer an edit of param would land in, or None when it can be edited.

    Where an edit goes is the same answer ChunkParamBase.data_changed acts on
    (chunk_utils.param_storage_layer): with the stage's edit target on a read-only
    department file, a layout chunk's param is locked; set it to a layer you can edit and
    the same edit is an override there.
    """
    try:
        stage = param.get_scene_stage()
        path = getattr(param, "_param_attr_path", None)
        if stage is None or not path or getattr(param, "_var_selections", None):
            return None
        attr = stage.GetAttributeAtPath(Sdf.Path(path))
        if not attr:
            return None
        layer = chunk_utils.param_storage_layer(stage, attr, param.get_chunk())
    except (AttributeError, ReferenceError):
        return None
    return layer if chunk_utils.is_layer_locked(layer) else None


def param_held_by(param: core.ChunkParamBase) -> Sdf.Layer | None:
    """The layer whose opinion on this param is stronger than where the param writes, or None.

    The other side of param_override_source. There the param's own value sits over one
    composed in; here something sits over the param's own: an Override Chunk Prim's layer,
    or a department layer stronger than the one this chunk's prim is written in. An edit
    to the param still lands, in its home (chunk_utils.get_attr_home_layer), but the value
    composed -- and so the one the param shows and cooks with -- is the stronger one's.
    Without saying so the field just springs back.

    Args:
        param: The param, or a vector part (its vector's attribute is the one asked about).

    Returns:
        The stronger layer, or None.
    """
    chunk = param.get_chunk()
    stage = param.get_scene_stage() if chunk else None
    if stage is None:
        return None
    owner = param.get_owner()
    target = owner if isinstance(owner, core.ChunkVectorBase) else param
    names = _param_object_path(chunk, target)
    return _input_held_by(chunk, stage, ":".join(names)) if names else None


def _input_held_by(chunk: core.ChunkBase, stage: Usd.Stage, name: str) -> Sdf.Layer | None:
    """param_held_by for the chunk prim's input of that name (translate_grp:tx)."""
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not prim.IsValid():
        return None
    attr = prim.GetAttribute(_INPUTS_PREFIX + name)
    if not attr:
        return None
    specs = [s for s in attr.GetPropertyStack(Usd.TimeCode.Default()) if s.HasDefaultValue()]
    if not specs or specs[0].path != attr.GetPath():
        return None  # nothing, or the strongest is across an arc: weaker than any local layer
    strongest = specs[0].layer
    home = chunk_utils.get_attr_home_layer(attr) or chunk_utils.get_bookkeeping_layer(stage, chunk)
    if strongest == home:
        return None
    stack = stage.GetLayerStack(includeSessionLayers=True)
    if strongest not in stack or home not in stack:
        return None
    return strongest if stack.index(strongest) < stack.index(home) else None


def layer_label(layer: Sdf.Layer) -> str:
    """A layer as a user knows it: the chunk whose cook layer it is, or its file name.

    A chunk's procedural layer is named anon:<address>:<tag>, which says nothing to anyone;
    the chunk it belongs to is what they put on the graph.

    Args:
        layer: The layer.

    Returns:
        The chunk's name, or the layer's display name (its identifier when it has none).
    """
    lm = chunk_registry.ChunkLayerManager()
    scene = _chunk_mod.get_scene()
    if lm.initialized and scene is not None and chunk_registry.is_chunk_internal_layer(layer):
        for chunk in scene.get_chunks():
            if lm.get_layer(chunk) == layer:
                return chunk.name
    return layer.GetDisplayName() or layer.identifier


def revert_param_override(param: core.ChunkParamBase) -> None:
    """Drop the shot's opinion on this param so the composed-in value shows through again.

    Every owned-layer spec for the storage attr goes (an override can have been written
    under more than one edit target over its life); the param is then set to what now
    composes, which the write path sees as already authored and leaves alone.

    Args:
        param: The param to revert.
    """
    attr = _storage_attr(param)
    if attr is None or param_override_source(param) is None:
        return
    stage = attr.GetStage()
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        for spec in list(attr.GetPropertyStack(Usd.TimeCode.Default())):
            if spec.path != attr.GetPath() or chunk_utils.is_foreign_layer(spec.layer, stage):
                continue
            prim_spec = spec.layer.GetPrimAtPath(spec.path.GetPrimPath())
            if prim_spec is not None and spec.path.name in prim_spec.properties:
                del prim_spec.properties[spec.path.name]
    finally:
        chunk_notice._usd_write_active = prev
    invalidate_edit_layer_memo()
    param.value = attr.Get()


def _adopt_inputs(chunk: core.ChunkBase, names: Iterable[str] | None = None) -> None:
    """Set chunk's params to what its chunk prim composes now, or their defaults.

    The prim is where a param's value lives; the param holds it for the module. Anything
    that authors the prim's inputs: -- an Override Chunk Prim cooking an `over` onto it, a
    layer muted or removed, a hand edit in usdview -- comes back through here. The values set
    are the composed ones, so the write path finds them already authored and writes nothing
    back; a param whose value changes dirties the chunk and it cooks as after any edit. An
    expression is left alone, as in _mirror_changed_params, and so are list-group rows.

    Args:
        chunk: The chunk to bring in line with its prim.
        names: The input names to read (translate_grp:tx), or None for all of them.
    """
    stage = chunk.get_scene_stage()
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk))) if stage else None
    if not (prim and prim.IsValid()):
        return
    wanted = None if names is None else set(names)
    # By name, not _param_attr_path: a param still at its default has no attribute of its
    # own yet, so nothing is bound to the one an override has just made.
    for param_path, param in (*_iter_chunk_params(chunk), *_iter_chunk_vectors(chunk)):
        name = param_path.replace(".", ":")
        if wanted is not None and name not in wanted:
            continue
        if _under_list_group(param, chunk) or param.has_flag(ChunkFlags.ParamFlags.OUTPUT):
            continue
        raw = getattr(param, "raw_value", None)
        if isinstance(raw, str) and chunk_utils.has_expression(raw):
            continue
        attr = prim.GetAttribute(f"{_INPUTS_PREFIX}{name}")
        if attr and attr.IsValid():
            # An attribute with no value yet is one being made -- the write path creates,
            # then sets -- and the set that follows says what it holds.
            value = attr.Get(0)
        else:
            # No layer has it any more: the param is back at its default.
            value = getattr(param, "default_value", None)
        if value is not None and not _values_agree(value, param.value):
            param.value = value


def _storage_attr(param: core.ChunkParamBase) -> Usd.Attribute | None:
    """The chunk-prim attribute holding this param's value, once it has one."""
    path = getattr(param, "_param_attr_path", None)
    if not path:
        return None
    chunk = param.get_chunk()
    stage = chunk.get_scene_stage() if chunk else None
    if not stage:
        return None
    attr = stage.GetAttributeAtPath(Sdf.Path(path))
    return attr if attr and attr.IsValid() else None


def is_merge_chunk(chunk: core.ChunkBase) -> bool:
    """Whether chunk is specifically a Merge chunk (chunks/core/merge.py).

    Narrower than has_dynamic_exec_inputs_prim: Merge's slot order also decides composition
    strength ("first input wins"), which is a Merge-only meaning that a dynamic-slot chunk
    with no such ranking (a Switch) does not share.

    Args:
        chunk: The chunk.

    Returns:
        True for a Merge chunk.
    """
    cfg = chunk.get_config()
    return bool(cfg and cfg.module == "merge")


def has_dynamic_exec_inputs_prim(prim: Usd.Prim) -> bool:
    """Whether a chunk prim's exec input slots grow with what is wired in.

    Such a chunk gets one numbered port (inputs:chunk, inputs:chunk1, ...) per branch wired
    in -- Merge and Switch, and whatever else declares config.dynamic_exec_inputs. See
    exec_input_slots_for_prim for the other kind, where the count is fixed and positional.

    Takes the prim: the node graph builds ports from the stage and may have no chunk
    object. See exec_input_slots_for_prim for why this reads the registry's discovery-time
    config rather than importing the module.

    Args:
        prim: The chunk prim.

    Returns:
        True for a dynamic-slot chunk's prim.
    """
    if not (prim and prim.IsValid()):
        return False
    attr = prim.GetAttribute(_CHUNK_TYPE_ATTR)
    module_name = attr.Get() if attr and attr.IsValid() else None
    if not module_name:
        return False
    module = chunk_registry.ChunkRegistry().get_chunk_module(str(module_name))
    cfg = getattr(module, "config", None) if module else None
    return bool(cfg and getattr(cfg, "dynamic_exec_inputs", False))


def exec_input_slots_for_prim(prim: Usd.Prim) -> int:
    """How many positional exec input ports a chunk prim has.

    1 for almost everything -- one inputs:chunk that several wires may stack on. Above 1
    the slots mean different things and each gets its own port, which is what a Stage
    Expression Switch needs: slot 0 is the normal path and slot 1 the matched one, which is
    not a property a merge ordering can express.

    A dynamic-slot chunk (has_dynamic_exec_inputs_prim) answers 1 here and is handled by
    that predicate instead: its slots grow with what is wired in, so a fixed count is the
    wrong question for it.

    Takes the prim: the node graph builds ports from the stage and may have no chunk
    object. Read off the registry's discovery-time config rather than by importing the
    module: the config is parsed when chunks are found, so this costs a dict lookup and
    never triggers a module load while a scene is being built.

    Args:
        prim: The chunk prim.

    Returns:
        The slot count, at least 1.
    """
    if not (prim and prim.IsValid()):
        return 1
    attr = prim.GetAttribute(_CHUNK_TYPE_ATTR)
    module_name = attr.Get() if attr and attr.IsValid() else None
    if not module_name:
        return 1
    module = chunk_registry.ChunkRegistry().get_chunk_module(str(module_name))
    cfg = getattr(module, "config", None) if module else None
    return max(1, int(getattr(cfg, "exec_inputs", 1) or 1)) if cfg else 1


def set_chunks_muted(stage: Usd.Stage, to_mute, to_unmute) -> tuple[set, set]:
    """Block and unblock many chunks, changing the stage's mute state ONCE.

    Every stage.MuteLayer / UnmuteLayer is a composition change, and composition changes
    are reported to everything watching the stage as a resync of `/` -- the whole stage,
    re-read. One per chunk would make a single shot change in a shot-gated graph cost
    several full-stage resyncs, and far more on the first switch, each one telling Hydra
    that every prim it holds may have changed.

    A live viewport is where that lands: the renderer repopulates again and again for one
    logical change, and the prim browser walks a stage that is being recomposed underneath
    it -- a renderer can even miss content entirely until it is switched away and back,
    which is simply a repopulate done once, cleanly.

    MuteAndUnmuteLayers does both lists in one operation, so the stage recomposes once and
    watchers get one resync however many branches the gate moved.

    Blocking is the engine's own lever, deliberately not the DISABLED state: DISABLED
    belongs to the user, who sets it by hand and expects it to stay. ChunkLayerManager
    guards the two against each other, so a blocked layer is never un-muted by the disable
    path.

    Args:
        stage: The stage whose layer mute state changes.
        to_mute: Chunks to block and mute.
        to_unmute: Chunks to unblock and unmute.

    Returns:
        tuple[set, set]: the chunks actually muted, and those actually un-muted
    """
    manager = chunk_registry.ChunkLayerManager()
    muted, unmuted = set(), set()
    mute_ids, unmute_ids = [], []

    for target in to_mute:
        # Never a context: is_blocked() climbs the owner chain, so blocking a container
        # would block its whole subtree, including branches the caller meant to keep.
        # Containers author nothing of their own; their leaves come through here one by one.
        if target.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            continue
        target.set_blocked(True)
        muted.add(target)
        if stage is not None and (layer := manager.get_layer(target)) is not None:
            if not stage.IsLayerMuted(layer.identifier):
                mute_ids.append(layer.identifier)

    for target in to_unmute:
        if target.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            continue
        target.set_blocked(False)
        unmuted.add(target)
        if stage is not None and (layer := manager.get_layer(target)) is not None:
            if stage.IsLayerMuted(layer.identifier):
                unmute_ids.append(layer.identifier)

    if stage is not None and (mute_ids or unmute_ids):
        # Not layer.SetMuted: that recomposes lazily and expires prims, which is the
        # "chunk layers disappear" bug the rest of the codebase avoids the same way.
        stage.MuteAndUnmuteLayers(mute_ids, unmute_ids)

    # After the recomposition, never during: a chunk coming back needs its prim on the
    # stage before its values have anywhere to go (see push_chunk_values).
    for target in unmuted:
        if stage is not None:
            push_chunk_values(stage, target)

    return muted, unmuted


#: Hidden param a gating chunk keeps its record in: the chunks it muted last time, by prim
#: path, so a branch that stops being upstream is released rather than left blocked forever.
GATE_RECORD_PARAM = "muted"


def _gate_record(node) -> set:
    param = node.get_param(GATE_RECORD_PARAM)
    return {line.strip() for line in (param.value or "").splitlines() if line.strip()} if param else set()


def gate_to_branches(node: core.ChunkBase, live: set[int]) -> tuple[set, set]:
    """Let the branches at `live` reach the stage and suppress every other input's.

    The one implementation behind both gating chunks -- the Switch, picking a branch by
    index, and the Stage Expression Switch, picking it by what a variable says. They differ
    only in how that set is chosen, so the muting lives here once.

    Suppression is a **block** plus a layer mute, never the DISABLED state, which belongs to
    the user: a chunk reaching in to set and clear it on chunks it does not own means every
    flip risks undoing theirs. A branch contributes because its layers are in the stage, not
    because it is wired, so cutting the connection instead would leave its prims composed
    exactly as before and gate nothing.

    Args:
        node: The gating chunk.
        live: Indices into the gate's exec dependencies (exec_dependencies) whose branches
            stay live.

    Returns:
        tuple[set, set]: the chunks kept live, and the chunks muted
    """
    stage = node.get_scene_stage()
    inputs = exec_dependencies(node)

    # The live branches first, and they win: a chunk feeding two branches must stay live if
    # either of the ones it feeds is chosen. Without this a shared upstream -- the part of
    # the thread above the fork -- would be muted along with the branch not taken.
    keep = set()
    for i in live:
        if 0 <= i < len(inputs):
            keep |= exec_upstream(inputs[i])

    # Collected rather than applied one at a time: every mute is a full-stage recomposition,
    # so a gate that moves six branches would make six of them. set_chunks_muted does the
    # lot in one.
    to_mute = []
    for slot, dep in enumerate(inputs):
        if slot in live:
            continue
        for c in exec_upstream(dep):
            if c in keep:
                continue
            to_mute.append(c)

    # Un-mute only what this chunk muted itself. A live branch can hold another gate's
    # suppressed branch, and un-muting everything reachable switched that one back on.
    mine = _gate_record(node)
    to_unmute = [c for c in keep if get_chunk_prim_path(c) in mine]

    # Muted by an earlier evaluation and no longer upstream of this chunk at all: leaving it
    # blocked would strand a branch the user has already disconnected. Resolved against the
    # whole scene, since the record is transitive -- most of what it holds was never a
    # direct input -- and by prim path, since a name is unique only within one context.
    stranded = mine - {get_chunk_prim_path(c) for c in to_mute} - {get_chunk_prim_path(c) for c in keep}
    if stranded:
        by_path = {
            get_chunk_prim_path(c): c
            for c in chunk_registry.ChunkRegistry().scene.get_chunks()
        }
        for path in stranded:
            if (c := by_path.get(path)) is not None:
                to_unmute.append(c)

    muted, _unmuted = set_chunks_muted(stage, to_mute, to_unmute)

    if muted:
        _redefine_orphaned_ancestors(stage)

    if param := node.get_param(GATE_RECORD_PARAM):
        param.value = "\n".join(sorted(get_chunk_prim_path(c) for c in muted))
    return keep, muted


def _redefine_orphaned_ancestors(stage: Usd.Stage) -> None:
    """Re-define the scopes that muting a branch just left defined by nobody.

    UsdStage.DefinePrim gives a prim's ancestors a `def` only when nothing has defined them
    yet, so whichever chunk cooks first owns `def "World"` and every later one holds a bare
    `over`. That answer is right when it is given and wrong a moment later: a gate mutes
    *after* its branches have cooked, so the layer holding the only `def` can leave while
    the layers holding the `over`s stay. Their prims are then valid but not defined, Hydra
    skips them, and a camera and every light in the room quietly leave the scene.

    Nothing else in the engine has this problem. View-driven cooking mutes first and cooks
    afterwards, so DefinePrim sees the final state and picks correctly on its own -- which
    is why this repair belongs here, at the one place that mutes after the fact, rather than
    on every cook of every chunk.

    Only genuinely orphaned ancestors are touched: an ancestor still defined by anything --
    another live chunk, a referenced asset, the file the user opened -- is left exactly as
    USD authored it.
    """
    def still_defined(path) -> bool:
        prim = stage.GetPrimAtPath(path)
        return bool(prim) and prim.IsDefined()

    manager = chunk_registry.ChunkLayerManager()
    for chunk in chunk_registry.ChunkRegistry().scene.get_chunks():
        if chunk.is_blocked():
            continue  # muted: its own prims are out of the scene anyway
        layer = manager.get_layer(chunk)
        if layer is not None:
            chunk_layers.define_ancestors(layer, still_defined)


def is_merge_prim(prim: Usd.Prim) -> bool:
    """is_merge_chunk for a bare prim, when there is no live chunk object to ask.

    Used where only the stage is at hand (slot compaction, cook ordering).

    Args:
        prim: The chunk prim.

    Returns:
        True for a Merge chunk's prim.
    """
    if not (prim and prim.IsValid()):
        return False
    attr = prim.GetAttribute(_CHUNK_TYPE_ATTR)
    return bool(attr and attr.IsValid() and attr.Get() == "merge")


def is_chunk_pinned(chunk: core.ChunkBase) -> bool:
    """Whether chunk is pinned into its context's Params page.

    Reads the persisted ``chunk:pinned`` custom attribute (see ADR-0003), following the
    ``chunk:activeView`` precedent. An explicitly authored value (set by set_chunk_pinned,
    i.e. the Pin/Unpin menu action, or already written by _define_prim for this chunk)
    always wins.

    Otherwise -- no authored value, which can mean the PRIM ITSELF doesn't exist yet, not
    just a missing attribute on an existing one -- it falls back in order:

    1. Views are graph-wiring utility nodes, never meant for Params curation: default
       unpinned, whether genuinely legacy, mid-creation, or racing _define_prim's own write.
    2. _pin_new_chunks, when a create/paste flow is actively deciding this chunk's pin state
       right now. Set by the caller BEFORE the create notice is even sent, so it's correct
       regardless of listener order. This matters because the Params-page widget and
       ChunkStageSync both react to the same PARAM_ADDED notice for a newly-created chunk,
       and delivery order between two independent listeners on one notice isn't guaranteed
       -- the widget's listener can run (and call this) before ChunkStageSync's has even
       started _define_prim, let alone authored the attribute.
    3. Absent means unpinned. Pinning is the exception a user asks for, so it is the only
       state worth a line in the file; writing chunk:pinned = 0 onto every chunk prim would
       say nothing at all. A scene saved without pin state therefore opens with an
       unfiltered Params page rather than everything pinned.

    Args:
        chunk: The chunk.

    Returns:
        True if the chunk is pinned.
    """
    stage = chunk.get_scene_stage()
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk))) if stage else None
    attr = prim.GetAttribute("chunk:pinned") if (prim and prim.IsValid()) else None
    val = attr.Get() if (attr and attr.IsValid()) else None
    if val is not None:
        return bool(val)
    if is_terminal_chunk(chunk):
        return False
    if _pin_new_chunks is not None:
        return _pin_new_chunks
    return False


def set_chunk_pinned(chunk: core.ChunkBase, pinned: bool) -> None:
    """Persist chunk's pinned state as a custom ``chunk:pinned`` attribute on its prim.

    This is chunk-prim bookkeeping like any other inputs:/outputs: attr -- it must not
    rely on the stage's ambient edit target (chunk_utils.get_bookkeeping_layer resolves
    it instead, following Edit Target for a regular chunk / pinned to session for a
    CONTEXT chunk). An existing spec is updated in place (chunk_utils.get_attr_home_layer)
    so toggling this repeatedly never leaves an older, stronger opinion in one layer
    shadowing a newer one authored into a different, weaker layer -- the same rule
    ChunkParamBase.data_changed / update_usd_prop follow.

    Args:
        chunk: The chunk.
        pinned: The new state.
    """
    stage = chunk.get_scene_stage()
    if not stage:
        return
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not prim.IsValid():
        return
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        attr = prim.GetAttribute("chunk:pinned")
        if not pinned:
            # Unpinned is the default, so unpinning removes the opinion rather than writing
            # a false one -- wherever it was authored, since a stale stronger opinion in
            # another layer would otherwise shadow its absence here.
            for layer in stage.GetLayerStack(includeSessionLayers=True):
                spec = layer.GetPrimAtPath(prim.GetPath())
                if spec and "chunk:pinned" in spec.properties:
                    del spec.properties["chunk:pinned"]
            return
        home = chunk_utils.get_attr_home_layer(attr) if attr and attr.IsValid() else None
        target = home or chunk_utils.get_bookkeeping_layer(stage, chunk)
        with Usd.EditContext(stage, target):
            prim.CreateAttribute("chunk:pinned", Sdf.ValueTypeNames.Bool, custom=True).Set(True)
    finally:
        chunk_notice._usd_write_active = prev_w


def _context_own_prim_path(context: core.ChunkBase) -> str | None:
    """A CONTEXT chunk's own prim path -- NOT get_chunk_prim_path(context).

    get_chunk_prim_path is for a chunk resolving its path as a CHILD of some parent context.
    Called on the context itself it double-appends: ChunkBase.get_context() returns self for
    a chunk that already has the CONTEXT flag, so _resolve_chunk_root(context) resolves to
    context.prim_path, and get_chunk_prim_path then appends "/{context.name}" a second time
    (only chunkgroups dodge this, because their prim_path branch short-circuits before
    _resolve_chunk_root runs). Both a plain tab and a chunkgroup already have .prim_path set
    directly at creation (add_context / the group-node prim_path assignment in
    _on_param_added), so that is used as-is.
    """
    return context.prim_path


def is_pin_filter_enabled(context: core.ChunkBase) -> bool:
    """Whether context's Params page applies the pin filter, showing only pinned chunks.

    Reads the persisted ``filter:pinEnabled`` custom attribute on the context's own prim,
    same pattern as ``chunk:pinned``. An absent attribute means enabled, so a tab saved
    without the attribute keeps the pin filter applied.

    Args:
        context: The context.

    Returns:
        True if the pin filter is on.
    """
    stage = context.get_scene_stage()
    path = _context_own_prim_path(context)
    prim = stage.GetPrimAtPath(Sdf.Path(path)) if (stage and path) else None
    attr = prim.GetAttribute("filter:pinEnabled") if (prim and prim.IsValid()) else None
    val = attr.Get() if (attr and attr.IsValid()) else None
    return True if val is None else bool(val)


def set_pin_filter_enabled(context: core.ChunkBase, enabled: bool) -> None:
    """Persist context's pin-filter toggle as ``filter:pinEnabled`` on its own prim.

    That is the context/tab prim itself, not any of its children. Unlike a regular chunk, a
    plain tab context's own prim is never explicitly authored by _define_prim -- a tab
    context is added with send_added=False (see SceneSender.add_chunk), so PARAM_ADDED never
    fires for it and its prim only starts existing as an implicit ancestor once some CHILD
    chunk is added under it. An empty, just-created tab therefore has no prim at all yet,
    so one is defined on demand here rather than silently doing nothing.

    Args:
        context: The context.
        enabled: The new state.
    """
    stage = context.get_scene_stage()
    path = _context_own_prim_path(context)
    if not stage or not path:
        return
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim.IsValid():
            prim = stage.DefinePrim(Sdf.Path(path), _CHUNK_PRIM_TYPENAME)
        # An UPDATE goes where the attribute already lives; only a first write picks a
        # layer. An Edit Target chunk moves the stage's target while the graph is live, so
        # re-resolving it on every write scattered one attribute's opinions across layers
        # and let the older, stronger one go on winning -- the toggle stopped responding
        # mid-session while the widget went on reporting what was clicked.
        #
        # The first write deliberately still follows the ambient target rather than the
        # bookkeeping layer set_chunk_pinned uses. A tab's filter state is part of the
        # document and has to land somewhere that exports; the session layer does not.
        # Pinning both halves to one answer breaks one of them in each direction.
        existing = prim.GetAttribute("filter:pinEnabled")
        home = chunk_utils.get_attr_home_layer(existing) if existing and existing.IsValid() else None
        ctx = Usd.EditContext(stage, home) if home else contextlib.nullcontext()
        with ctx:
            prim.CreateAttribute("filter:pinEnabled", Sdf.ValueTypeNames.Bool, custom=True).Set(bool(enabled))
    finally:
        chunk_notice._usd_write_active = prev_w


_SEARCH_SCOPES = ("both", "name", "type")


def get_search_query(context: core.ChunkBase) -> str:
    """The context's persisted Params-page search query.

    An absent attribute means empty, as if nothing was ever typed.

    Args:
        context: The context.

    Returns:
        The query, "" if none.
    """
    stage = context.get_scene_stage()
    path = _context_own_prim_path(context)
    prim = stage.GetPrimAtPath(Sdf.Path(path)) if (stage and path) else None
    attr = prim.GetAttribute("filter:searchQuery") if (prim and prim.IsValid()) else None
    val = attr.Get() if (attr and attr.IsValid()) else None
    return val or ""


def set_search_query(context: core.ChunkBase, query: str) -> None:
    """Persist context's Params-page search query as ``filter:searchQuery`` on its own prim.

    Defines the prim on demand -- see set_pin_filter_enabled for why a plain tab context may
    not have one yet.

    Args:
        context: The context.
        query: The search text.
    """
    stage = context.get_scene_stage()
    path = _context_own_prim_path(context)
    if not stage or not path:
        return
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim.IsValid():
            prim = stage.DefinePrim(Sdf.Path(path), _CHUNK_PRIM_TYPENAME)
        # An UPDATE goes where the attribute already lives; only a first write picks a
        # layer. An Edit Target chunk moves the stage's target while the graph is live, so
        # re-resolving it on every write scattered one attribute's opinions across layers
        # and let the older, stronger one go on winning -- the toggle stopped responding
        # mid-session while the widget went on reporting what was clicked.
        #
        # The first write deliberately still follows the ambient target rather than the
        # bookkeeping layer set_chunk_pinned uses. A tab's filter state is part of the
        # document and has to land somewhere that exports; the session layer does not.
        # Pinning both halves to one answer breaks one of them in each direction.
        existing = prim.GetAttribute("filter:searchQuery")
        home = chunk_utils.get_attr_home_layer(existing) if existing and existing.IsValid() else None
        ctx = Usd.EditContext(stage, home) if home else contextlib.nullcontext()
        with ctx:
            prim.CreateAttribute("filter:searchQuery", Sdf.ValueTypeNames.String, custom=True).Set(query)
    finally:
        chunk_notice._usd_write_active = prev_w


def get_search_scope(context: core.ChunkBase) -> str:
    """The context's persisted search scope: one of "both", "name", "type".

    An absent attribute, or an unrecognized stored value, means "both" (search name and
    type).

    Args:
        context: The context.

    Returns:
        The scope.
    """
    stage = context.get_scene_stage()
    path = _context_own_prim_path(context)
    prim = stage.GetPrimAtPath(Sdf.Path(path)) if (stage and path) else None
    attr = prim.GetAttribute("filter:searchScope") if (prim and prim.IsValid()) else None
    val = attr.Get() if (attr and attr.IsValid()) else None
    return val if val in _SEARCH_SCOPES else "both"


def set_search_scope(context: core.ChunkBase, scope: str) -> None:
    """Persist context's search scope as ``filter:searchScope`` on its own prim.

    Args:
        context: The context.
        scope: "both", "name" or "type"; anything else is stored as "both".
    """
    if scope not in _SEARCH_SCOPES:
        scope = "both"
    stage = context.get_scene_stage()
    path = _context_own_prim_path(context)
    if not stage or not path:
        return
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim.IsValid():
            prim = stage.DefinePrim(Sdf.Path(path), _CHUNK_PRIM_TYPENAME)
        # An UPDATE goes where the attribute already lives; only a first write picks a
        # layer. An Edit Target chunk moves the stage's target while the graph is live, so
        # re-resolving it on every write scattered one attribute's opinions across layers
        # and let the older, stronger one go on winning -- the toggle stopped responding
        # mid-session while the widget went on reporting what was clicked.
        #
        # The first write deliberately still follows the ambient target rather than the
        # bookkeeping layer set_chunk_pinned uses. A tab's filter state is part of the
        # document and has to land somewhere that exports; the session layer does not.
        # Pinning both halves to one answer breaks one of them in each direction.
        existing = prim.GetAttribute("filter:searchScope")
        home = chunk_utils.get_attr_home_layer(existing) if existing and existing.IsValid() else None
        ctx = Usd.EditContext(stage, home) if home else contextlib.nullcontext()
        with ctx:
            prim.CreateAttribute("filter:searchScope", Sdf.ValueTypeNames.String, custom=True).Set(scope)
    finally:
        chunk_notice._usd_write_active = prev_w


# How many characters a typo may cost. Two covers a doubled letter, a swap and a slip;
# past that the difference is a different name, not a misspelling of this one.
_TYPO_SLACK = 2


def fuzzy_matches(term: str, candidate: str, threshold: float = 0.6) -> bool:
    """Whether term matches candidate, tolerating small typos.

    An exact case-insensitive substring always matches (the common case: the user typed the
    name or type correctly, or a meaningful fragment of it); otherwise difflib's
    typo-tolerant ratio decides.

    Args:
        term: What the user typed; blank matches everything.
        candidate: The text to match against.
        threshold: The minimum similarity ratio for a non-substring match.

    Returns:
        True on a match.
    """
    term = term.strip().lower()
    if not term:
        return True
    candidate = candidate.lower()
    if term in candidate:
        return True
    # SequenceMatcher's ratio is normalised by combined length, so two short names sharing
    # a prefix score on the overlap alone: "haha" comes out 0.73 similar to "haha222" and
    # was being returned by a search for "haha222". Length is what separates the two cases
    # -- a typo is a character or two out of place, never a name three characters shorter.
    # Deliberate fragments ("trans" for "transform_scale") never reach this line; they have
    # already matched as a substring above.
    if abs(len(term) - len(candidate)) > _TYPO_SLACK:
        return False
    return difflib.SequenceMatcher(None, term, candidate).ratio() >= threshold


def chunk_matches_search(chunk: core.ChunkBase, query: str, scope: str = "both") -> bool:
    """Whether chunk's name and/or type (per scope) fuzzy-matches query.

    Args:
        chunk: The chunk.
        query: The search text; an empty query always matches.
        scope: "both", "name" or "type".

    Returns:
        True on a match.
    """
    if not query.strip():
        return True
    candidates = []
    if scope in ("both", "name"):
        candidates.append(chunk.name)
    if scope in ("both", "type"):
        cfg = chunk.get_config()
        if cfg and cfg.module:
            candidates.append(cfg.module)
    return any(fuzzy_matches(query, c) for c in candidates)


def find_group_active_view(
    group: core.ChunkBase, stage: Usd.Stage, scene: chunk_scene.SceneSender
) -> core.ChunkBase | None:
    """The inner active view of a group -- its output driver.

    Reads the group prim's ``chunk:activeView`` slot (written when the view is switched
    inside the group), falling back to the first inner view.

    Args:
        group: The group.
        stage: The stage.
        scene: The scene.

    Returns:
        The view chunk, or None if the group has no inner view.
    """
    inner_views = [
        c for c in scene.get_chunks() if is_view_chunk(c) and is_node_in_context(c, group)
    ]
    if not inner_views:
        return None
    gp = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(group)))
    slot = None
    if gp and gp.IsValid():
        a = gp.GetAttribute("chunk:activeView")
        if a and a.IsValid():
            slot = a.Get()
    if slot:
        for v in inner_views:
            vp = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(v)))
            if vp and vp.IsValid() and vp.GetAttribute("view:slot").Get() == slot:
                return v
    return inner_views[0]


def find_view_chunk_for_slot(
    slot: int,
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
    context: core.ChunkBase | None = None,
) -> core.ChunkBase | None:
    """The view chunk assigned to slot, or None.

    Each graph (tab or group) has its own slots, so a tab view and a group view can both be
    slot 1.

    Args:
        slot: The view slot.
        stage: The stage.
        scene: The scene.
        context: The graph to look in; None looks everywhere.

    Returns:
        The view chunk, or None.
    """
    for c in scene.get_chunks():
        if not is_view_chunk(c):
            continue
        if context is not None and not is_node_in_context(c, context):
            continue
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
        if prim.IsValid() and prim.GetAttribute("view:slot").Get() == slot:
            return c
    return None


def get_view_exec_sources(
    view_chunk: core.ChunkBase,
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
) -> list[core.ChunkBase]:
    """Return the chunks wired into view_chunk's inputs:chunk."""
    prim_to_chunk = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(view_chunk)))
    if not prim.IsValid():
        return []
    exec_in = prim.GetAttribute(_EXEC_INPUT)
    if not exec_in.IsValid():
        return []
    return [
        c for conn in exec_in.GetConnections()
        if (c := prim_to_chunk.get(str(conn.GetPrimPath()))) is not None
    ]


def get_upstream_stage(stage: Usd.Stage, scene: chunk_scene.SceneSender, chunk: core.ChunkBase) -> Usd.Stage:
    """A throwaway stage of every chunk layer except `chunk` itself and its real downstream.

    The raw root layer is included too. It is for a read that must never be influenced by a
    downstream chunk's edit to the same prim -- e.g. Scatter reading a mesh a Transform wired
    *after* it also targets.

    Unlike exec_with_hidden_view, this never mutates the real stage, mutes any real layer, or
    executes anything -- it's a pure composition of already-existing layers into a scratch
    root, discarded once the caller is done reading. That makes it safe to call
    unconditionally from inside a chunk's own execute() (reactive param changes included),
    which is where it's actually needed: a plain param edit never goes through
    recook_for_view at all, so nothing else protects that path. Chunks are included by "not
    downstream of me", not "reachable upstream of me" -- a mesh from an unwired sibling chunk
    (e.g. a Mesh chunk the user picked by path, never wired into the exec chain) still
    resolves correctly; only genuine downstream chunks are excluded.

    Included layers keep their REAL relative strength order (read off the actual graph
    layers' subLayerPaths), not scene.get_chunks()'s iteration order. Two included chunks can
    both touch the same prim without conflicting with each other (e.g. whatever created a
    mesh, and a Transform rotating it, both authoring xformOp:transform on the same prim) --
    the real order already correctly resolves that via the existing sort-key/view-driven
    ranking, and rebuilding it from scratch here would silently pick a different, wrong
    winner.

    Args:
        stage: The live stage.
        scene: The scene.
        chunk: The chunk doing the read.

    Inside a masked cook (skip_cook) the scratch stage is masked the same way, and what the
    chunk looks up on it is watched like the cook stage itself: it is where such a chunk
    does its reading.

    Returns:
        The scratch stage.
    """
    lm = chunk_registry.ChunkLayerManager()
    # Which chunks are downstream is read off the chunk prims, which a cook stage (the
    # stage a module is handed in a masked cook) populates only in part.
    stage = scene.get_stage() or stage
    excluded = exec_downstream(chunk, stage, scene) | {chunk}
    excluded_ids = {layer.identifier for c in excluded if (layer := lm.get_layer(c)) is not None}
    layer_ids: list = []
    for g_layer in lm.graph_layers.values():
        for ident in g_layer.subLayerPaths:
            if ident in excluded_ids or ident in layer_ids:
                continue
            if stage.IsLayerMuted(ident):
                # A muted layer is one the real stage is currently showing nothing of: its
                # chunk is dormant -- blocked (outside the active view's cook set, e.g. just
                # disconnected) or explicitly disabled. Rebuilding the composition from raw
                # subLayerPaths would resurrect it here, so a dormant chunk's stale content
                # would keep driving everyone else's reads while invisible everywhere else.
                #
                # Disconnecting is exactly when this matters: while a chunk is still wired
                # downstream the exclusion above already covers it, and cutting it loose is
                # what would let it back in. A Prune shaken off a chain, say, must not leave
                # its orphaned "active = false" hiding geometry from the chunks it fed.
                continue
            layer_ids.append(ident)
    scratch_root = Sdf.Layer.CreateAnonymous()
    scratch_root.subLayerPaths = layer_ids + [stage.GetRootLayer().identifier]
    # A stage reads its up axis and units off its own root and session layers only, never a
    # sublayer, so the scratch root carries the live stage's values itself.
    for key in (UsdGeom.Tokens.upAxis, UsdGeom.Tokens.metersPerUnit):
        if stage.HasAuthoredMetadata(key):
            scratch_root.pseudoRoot.SetInfo(key, stage.GetMetadata(key))
    return skip_cook.open_for_reading(scratch_root)


def heal_exec_chain_on_delete(stage: Usd.Stage, deleted_path: str) -> None:
    """Rewire around a node about to be deleted so its exec chain survives.

    For A -> X -> B, deleting X must leave A -> B (each successor B's inputs:chunk is
    repointed to X's predecessor A; cleared if X had none). Call while X's prim still exists
    -- its inputs:chunk predecessor is only readable before removal. Exec only
    (inputs/outputs:chunk); data/typed connections to X are left to drop with the prim.

    Args:
        stage: The stage to author on.
        deleted_path: Prim path of the node being deleted.
    """
    deleted = Sdf.Path(deleted_path)
    del_prim = stage.GetPrimAtPath(deleted)
    if not (del_prim and del_prim.IsValid()):
        return
    # The deleted node's PREDECESSOR is read from slot 0 only, on purpose: a successor has a
    # single input to inherit into, so it can be handed exactly one branch, and slot 0 is the
    # primary/strongest one. Deleting a merge therefore grafts its top branch onto whatever
    # came after it, rather than fanning all of them into a chunk that cannot express fan-in.
    exec_in = del_prim.GetAttribute(_EXEC_INPUT)
    pred_conns = list(exec_in.GetConnections()) if (exec_in and exec_in.IsValid()) else []
    out_path = f"{deleted_path}.{_EXEC_OUTPUT}"
    touched_dynamic = []
    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        for prim in stage.Traverse():
            if prim.GetPath() == deleted or prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
                continue
            # SUCCESSORS, by contrast, must be found across every slot. A merge fed through
            # slot 2 is just as much a successor as one fed through slot 0, and skipping it
            # would leave that slot connected to a prim that is about to stop existing.
            for succ in iter_exec_inputs(prim):
                if not any(str(c) == out_path for c in succ.GetConnections()):
                    continue
                # Write to succ's OWN home layer (or the session layer as a last resort),
                # not wherever the stage's ambient edit target happens to be (the root
                # layer, since nothing here ever calls SetEditTarget) -- an interactively
                # wired connection almost always lives on the session layer, which is
                # stronger than root. Healing on root left the original, still-composed
                # session-layer opinion (pointing at the just-deleted node) in charge, so
                # the "healed" A->B edge was silently shadowed away by composition and
                # never actually took effect. Same pattern as _author_connection /
                # _author_raw_connection.
                home = chunk_utils.get_attr_home_layer(succ) or chunk_utils.get_bookkeeping_layer(stage, None)
                with Usd.EditContext(stage, home):
                    if pred_conns:
                        succ.SetConnections(pred_conns)
                    else:
                        succ.ClearConnections()
                if has_dynamic_exec_inputs_prim(prim):
                    touched_dynamic.append(prim)
    finally:
        chunk_notice._usd_write_active = prev_w
    # Suppressing notices above means opting out of the choke point that normally keeps the
    # dynamic-slot invariant (_reconcile_changed_exec_inputs), so restore it here instead: a
    # slot cleared because its branch was deleted has to compact like any other.
    for prim in touched_dynamic:
        normalize_merge_slots(stage, prim)
    # Healing rewrites inputs:chunk across the graph with notices suppressed -- every
    # memoised answer below the deleted node is now measured against an edge that is gone.
    invalidate_edit_layer_memo()


def _exec_successor_paths(stage: Usd.Stage, node_path: str) -> set[str]:
    """Prim paths of every chunk whose exec input (any slot) is wired to node_path."""
    out_path = f"{node_path}.{_EXEC_OUTPUT}"
    found: set[str] = set()
    for prim in stage.Traverse():
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME or str(prim.GetPath()) == node_path:
            continue
        for attr in iter_exec_inputs(prim):
            if any(str(c) == out_path for c in attr.GetConnections()):
                found.add(str(prim.GetPath()))
                break
    return found


def _remove_attr_spec(stage: Usd.Stage, prim: Usd.Prim, name: str) -> None:
    """Remove an attribute wherever it is actually authored.

    Usd.Prim.RemoveProperty only unauthors from the stage's *current* edit target, which for
    chunk bookkeeping is routinely not where the spec lives -- an interactively wired
    connection sits on the session layer, a loaded one in the file. Removing from the wrong
    layer leaves the real opinion composed and in charge, the same failure mode
    heal_exec_chain_on_delete documents. So sweep the layer stack and drop every spec.
    """
    prim_path = prim.GetPath()
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        prim_spec = layer.GetPrimAtPath(prim_path)
        if prim_spec and name in prim_spec.properties:
            del prim_spec.properties[name]


def normalize_merge_slots(stage: Usd.Stage, prim: Usd.Prim) -> bool:
    """Keep a dynamic-slot chunk's connected exec inputs in slots 0..N-1 with no gaps.

    Applies to Merge, Switch and any other dynamic-slot chunk. Clearing a middle slot
    compacts the rest up, so slot order (which is what decides whose Merge opinion wins, or
    which branch a Switch's index N means) always matches the port order the user sees on
    the node, rather than drifting into a question about invisible attribute names.

    Judged on the CONNECTED slots alone -- an exec attribute that exists but connects nothing
    is deliberately left in place. It has to be: this runs synchronously from the stage
    notice that CreateAttribute itself fires, so an authoring path which creates the
    attribute and then connects it (_author_connection does exactly that, and so does the
    numbered-slot branch of _on_dependency_added) would otherwise have the attribute deleted
    underneath it between the two statements, and would go on to call SetConnections on an
    invalid handle. The cost is that clearing the LAST slot can leave a connectionless
    inputs:chunkN behind; it is inert -- it carries no connection, `_build_ports` counts
    connections rather than attributes, and the next compaction sweeps it -- so it is
    preferable to breaking the authoring paths.

    No-ops for anything without dynamic exec inputs, and for one already in shape, so it is
    safe to call on every exec-input change.

    Args:
        stage: The stage to author on.
        prim: The chunk prim.

    Returns:
        bool: True if any slot was rewritten
    """
    if not has_dynamic_exec_inputs_prim(prim):
        return False
    current = [(attr, list(attr.GetConnections())) for attr in iter_exec_inputs(prim)]
    live = [(attr, conns) for attr, conns in current if conns]
    desired_names = [exec_input_name(i) for i in range(len(live))]
    if [attr.GetName() for attr, _ in live] == desired_names:
        return False

    # Resolve each surviving branch's home layer BEFORE unauthoring anything, so a moved
    # connection is re-authored in the layer it was already living in rather than being
    # silently relocated (which could put it somewhere weaker than a spec left elsewhere).
    moves = [
        (
            desired_names[i],
            conns,
            chunk_utils.get_attr_home_layer(attr) or chunk_utils.get_bookkeeping_layer(stage, None),
        )
        for i, (attr, conns) in enumerate(live)
    ]

    prev_w = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        # Clear every slot first, then re-author. Renaming in place would have to worry about
        # a move landing on a slot that is still occupied by the branch below it.
        for attr, _ in current:
            _remove_attr_spec(stage, prim, attr.GetName())
        for name, conns, home in moves:
            with Usd.EditContext(stage, home):
                attr = prim.CreateAttribute(name, Sdf.ValueTypeNames.Token, custom=False)
                attr.SetConnections(conns)
    finally:
        chunk_notice._usd_write_active = prev_w
    # Slots moved with notices suppressed, so nothing else will drop the memoised
    # nearest-upstream-Edit-Target answers measured against the old wiring.
    invalidate_edit_layer_memo()
    return True


def is_edit_target_chunk(chunk: core.ChunkBase) -> bool:
    """Return True if chunk is an Edit Target chunk (chunks/usd/edit_target.py)."""
    cfg = chunk.get_config()
    return bool(cfg and cfg.module == "edit_target")


def _promote_prim_spec(dst_spec: Sdf.PrimSpec, holders: list[Sdf.Layer], sdf_path: Sdf.Path) -> None:
    """Give dst_spec the specifier/typeName the composed prim currently has, if it lacks them.

    Only fills gaps: an existing `def` or an already-authored typeName on dst_spec is left
    alone, so a destination that already carries the real spec keeps its own prim metadata.
    Resolution matches composition -- `def` anywhere in the stack wins over `over` regardless
    of strength, while typeName is strongest-wins (holders is strong -> weak).
    """
    if dst_spec.specifier != Sdf.SpecifierDef and any(
        layer.GetPrimAtPath(sdf_path).specifier == Sdf.SpecifierDef for layer in holders
    ):
        dst_spec.specifier = Sdf.SpecifierDef
    if not dst_spec.typeName:
        dst_spec.typeName = next(
            (tn for layer in holders if (tn := layer.GetPrimAtPath(sdf_path).typeName)), ""
        )


def _apply_connection_list_op(spec: Sdf.PropertySpec, op: Sdf.PathListOp) -> None:
    """Author op onto spec's connections, replacing whatever it had.

    connectionPaths is a read-only field for SetInfo, so the list op has to be replayed
    through the connectionPathList proxy one edit list at a time.
    """
    proxy = spec.connectionPathList
    proxy.ClearEdits()
    if op.isExplicit:
        proxy.explicitItems = list(op.explicitItems)
        return
    proxy.addedItems = list(op.addedItems)
    proxy.prependedItems = list(op.prependedItems)
    proxy.appendedItems = list(op.appendedItems)
    proxy.deletedItems = list(op.deletedItems)
    proxy.orderedItems = list(op.orderedItems)


def _migratable_layers(stage: Usd.Stage) -> list[Sdf.Layer]:
    """The layers a chunk's bookkeeping may be moved between: the stack, minus internal and foreign layers.

    Strong -> weak, without the chunk graph's own internal procedural layers.

    Its own function because it is loop-invariant across a reconcile pass but costs the
    size of the layer stack, and the layer stack grows with the graph: every chunk carries
    a procedural layer. Recomputed per chunk, it makes a whole-scene pass quadratic on
    exactly the graphs that pass exists to keep correct.

    Each layer is asked directly whether the chunk system owns it, rather than matched
    against a set collected by walking every tracked layer's sublayers. Collecting that set
    is itself proportional to the graph, and this runs once per pass, of which there is one
    per wire -- so that walk would be most of the cost of building a graph.

    Foreign layers (chunk_utils.is_foreign_layer) are left out as well, from the other
    side: a sublayered sequence file is not somewhere a chunk's bookkeeping may be moved
    FROM. Migration moves a spec by deleting the original, and with the sequence in the
    list, opening a shot would consolidate every sequence chunk into the session layer and
    gut the sequence in the Sdf layer cache -- the same damage protect_root_layer guards
    the root against, for a layer the shot does not even own. A chunk whose only spec is
    foreign simply has nothing to migrate; the shot's overrides on it, which live in owned
    layers, still consolidate.
    """
    return [
        layer for layer in stage.GetLayerStack(includeSessionLayers=True)
        if not chunk_registry.is_chunk_internal_layer(layer)
        and not chunk_utils.is_foreign_layer(layer, stage)
    ]


def _migrate_chunk_bookkeeping(
    stage: Usd.Stage,
    prim_path: str,
    dst_layer: Sdf.Layer,
    migratable: list[Sdf.Layer] | None = None,
) -> bool:
    """Consolidate a chunk prim's bookkeeping spec into dst_layer. Returns True if anything moved.

    The spec is removed from any other migratable layer it currently sits in. "Migratable"
    excludes the chunk graph's own internal per-chunk procedural layers -- those hold driven
    content that is Clear()ed and regenerated on execute (and re-nested under the right
    target by ChunkLayerManager), never hand-moved.

    Consolidation is per-property, resolved strong -> weak across every holder INCLUDING
    dst_layer, so what survives is exactly what was composing before the move. A whole-spec
    Sdf.CopySpec cannot do this: it REPLACES the destination, so it drops every field the
    copy source happens not to carry. Both directions of that actually bite here, because a
    chunk's bookkeeping is routinely split -- the real spec (info:chunkType, view:slot,
    ui:nodegraph:node:pos) in the session layer, plus a bare `over` holding just inputs:chunk
    left in the root layer by a write that went through the stage's ambient edit target:

    - copying the root `over` over the session spec loses view:slot, and without it
      find_view_chunk_for_slot finds no view at all, collapsing the cook set to
      "nothing cooks";
    - keeping the session spec and merely pruning the root `over` loses inputs:chunk,
      which unwires the chunk from its exec chain.

    dst_layer keeps its own prim-level metadata (specifier, typeName) when it already has a
    spec; only a dst with nothing there yet takes those from the strongest holder. The one
    exception is a dst spec that is a bare `over` with no typeName -- the shape left behind by
    an ordinary attr write that happened to target dst while this chunk's `def` still lived
    elsewhere. Keeping that as-is while the sources (holding the only `def`) are pruned would
    leave the chunk prim undefined and typeless across the whole stack, so it vanishes
    from any Usd.PrimIsDefined traversal -- the node graph stops drawing a node for it.
    """
    sdf_path = Sdf.Path(prim_path)
    # migratable is the same list for every chunk in a pass; a caller doing a pass hands it
    # in once instead of paying for it per chunk. It must be re-read after anything that can
    # open or sublayer a layer -- see _migratable_layers.
    if migratable is None:
        migratable = _migratable_layers(stage)
    holders = [layer for layer in migratable if layer.GetPrimAtPath(sdf_path)]
    sources = [layer for layer in holders if layer != dst_layer]
    if not sources:
        return False
    winners: dict[str, Sdf.Layer] = {}
    # Connections resolve as their own field, not with the property spec that holds them.
    # A bare attr spec (a CreateAttribute that authored no connection) can sit in a stronger
    # layer than the spec actually carrying the wire -- it wins `winners` below, its
    # connection-less spec is what gets copied, and pruning the sources then drops the
    # connection for good: a view loaded from file loses the inputs:chunk feeding it. So
    # snapshot the strongest *authored* connection opinion per property up front -- before
    # any copying rewrites dst -- and re-apply it after the copies.
    conn_ops: dict[str, Sdf.PathListOp] = {}
    for layer in holders:  # GetLayerStack is strong -> weak, so first seen is strongest
        for name, prop in layer.GetPrimAtPath(sdf_path).properties.items():
            winners.setdefault(name, layer)
            if name not in conn_ops and prop.HasInfo("connectionPaths"):
                conn_ops[name] = prop.GetInfo("connectionPaths")
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        if dst_layer.GetPrimAtPath(sdf_path) is None:
            Sdf.CreatePrimInLayer(dst_layer, sdf_path)
            Sdf.CopySpec(holders[0], sdf_path, dst_layer, sdf_path)
        else:
            _promote_prim_spec(dst_layer.GetPrimAtPath(sdf_path), holders, sdf_path)
        for name, layer in winners.items():
            if layer == dst_layer:
                continue  # already the winning opinion, and copying onto itself is a no-op
            prop_path = sdf_path.AppendProperty(name)
            Sdf.CopySpec(layer, prop_path, dst_layer, prop_path)
        for name, op in conn_ops.items():
            dst_prop = dst_layer.GetPropertyAtPath(sdf_path.AppendProperty(name))
            if dst_prop is not None:
                _apply_connection_list_op(dst_prop, op)
        for layer in sources:
            parent = layer.GetPrimAtPath(sdf_path.GetParentPath())
            if parent and sdf_path.name in parent.nameChildren:
                del parent.nameChildren[sdf_path.name]
    finally:
        chunk_notice._usd_write_active = prev
    return True


_reconciling = False

#: True while load_chunks is rebuilding a scene from a file.
#:
#: Opening a file must not rewrite it. load_chunks already passes protect_root_layer=True to
#: its own reconcile for that reason, but a load also COOKS, and cooking an Edit Target chunk
#: calls publish_edit_target -- which reconciles again, and did so unguarded.
#:
#: That second reconcile fires on every open, not rarely: the marker holds a layer
#: identifier, and an anonymous layer gets a NEW identifier in each process, so
#: `old_ident != ident` is true on reload even when nothing about the file changed. The
#: reconcile then migrates bookkeeping out of the root layer, and _migrate_chunk_bookkeeping
#: moves a spec by DELETING the original -- gutting the file that was just opened. The
#: damage is invisible until something saves, because the composed stage still resolves
#: every chunk from the layer they were moved into.
#:
#: Deliberately not a blanket protection inside reconcile_edit_targets: re-homing out of the
#: root layer is exactly right when a user re-points an Edit Target at runtime. It is wrong
#: only while loading, where the file already records where each chunk's bookkeeping lives
#: and is therefore the answer rather than the question.
_loading = False


@contextlib.contextmanager
def _loading_from_file():
    """Mark a load in progress. Re-entrant: a nested load restores, never clears."""
    global _loading
    previous = _loading
    _loading = True
    try:
        yield
    finally:
        _loading = previous


def _chunks_downstream_of(roots: list[core.ChunkBase]) -> set[int]:
    """Ids of every chunk that solves at or after one of roots.

    Read off the in-memory DAG rather than by traversing the stage for inputs:chunk
    connections: the exec chain is mirrored into that graph as it is built, so the answer is
    already there, and getting it costs the size of the answer instead of the size of the
    scene. Held as ids because ChunkBase's __hash__ is by chunk hash, and this only ever
    needs identity.

    A superset of the exec-only downstream -- the DAG also carries param wiring and ownership
    -- which is what makes it safe to select on: reconciling a chunk whose target has not
    moved does nothing, so over-including costs a no-op while under-including would leave a
    chunk homed against a target that no longer applies to it.
    """
    found: set[int] = set()
    for root in roots:
        for node in chunk_scheduler.downstream_closure(root.get_dag_node()):
            if isinstance(node.propagator, core.ChunkBase):
                found.add(id(node.propagator))
    return found


def connect_exec(
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
    src_prim_path: str,
    dst_prim_path: str,
    input_name: str = _EXEC_INPUT,
    append: bool = False,
) -> Usd.Attribute | None:
    """Author a plain outputs:chunk -> inputs:chunk exec connection between two chunk prims.

    For a caller that creates chunks directly (a chunk module wiring its own children, a
    test, a CLI) rather than via the node graph's drag-to-connect: view-driven execution
    (recook_for_view) mutes any chunk not reachable from its context's active view, so a
    programmatically created chunk that's never wired into the exec chain gets hidden the
    first time its context recooks, even though it authored real content. Mirrors the
    exec-only path of ChunkGraphView._author_connection; does not handle typed param
    connections.

    Deliberately does NOT also trigger a recook the way _author_connection does for a
    live drag-to-connect: figuring out which view (if any) is safe to recook through from
    inside a headless caller, without touching mute/block state anywhere it doesn't have
    full information about, risks muting unrelated scene content, or the chunk just wired
    itself. Simpler and safer to author the connection correctly here and let the caller's
    own next real recook (a view switch, another wire, diving into the context) pick it up.

    Args:
        stage: The stage to author on.
        scene: The scene the chunks live in.
        src_prim_path: Prim path of the upstream chunk.
        dst_prim_path: Prim path of the downstream chunk.
        input_name: The exec slot on dst -- a merge's inputs:chunkN rather than the plain
            inputs:chunk, so rewiring something into the slot it came out of does not
            replace whatever branch is already in slot 0.
        append: Add src to the slot's existing wires instead of replacing them, for a slot
            that several wires stack on.

    Returns:
        The authored inputs:chunk attribute, or None if either prim couldn't be resolved.
    """
    dst_prim = stage.GetPrimAtPath(Sdf.Path(dst_prim_path))
    if not dst_prim.IsValid():
        return None
    src_conn_path = Sdf.Path(f"{src_prim_path}.{_EXEC_OUTPUT}")
    dst_attr = dst_prim.GetAttribute(input_name)
    existing = list(dst_attr.GetConnections()) if dst_attr and dst_attr.IsValid() else []
    if append:
        wanted = existing if src_conn_path in existing else existing + [src_conn_path]
    else:
        wanted = [src_conn_path]
    if existing == wanted:
        # Already wired exactly this way: nothing changed, so there is nothing to reconcile.
        # A chunk module wiring its own children (AMD MaterialX Library) calls this on every
        # cook, and the reconcile below is not free. On a scene just opened from disk it
        # re-homes every chunk out of the file's root layer -- deleting the originals, from
        # the layer Sdf hands to the next open or save of that path -- and re-executes each
        # one it moves, which cascades a solve over everything downstream of it. That turned
        # a 60-chunk load into 566 module runs and left 7 chunk definitions in the file.
        # load_chunks guards its own reconcile against both (cook=False,
        # protect_root_layer=True); a re-entrant one from inside a cook bypassed them.
        return dst_attr
    if not (dst_attr and dst_attr.IsValid()):
        dst_attr = dst_prim.CreateAttribute(input_name, Sdf.ValueTypeNames.Token, custom=False)
        if not dst_attr.IsValid():
            return None
    dst_chunk = next((c for c in scene.get_chunks() if get_chunk_prim_path(c) == dst_prim_path), None)
    home = chunk_utils.get_attr_home_layer(dst_attr)
    target = home or chunk_utils.get_bookkeeping_layer(stage, dst_chunk)
    with Usd.EditContext(stage, target):
        dst_attr.SetConnections(wanted)
    reconcile_edit_targets(stage, scene)
    return dst_attr


def _reconcile_destination(stage: Usd.Stage, chunk: core.ChunkBase, prim: Usd.Prim) -> Sdf.Layer | None:
    """Where reconcile_edit_targets should move a chunk's bookkeeping, or None to leave it.

    A chunk with an Edit Target chunk upstream belongs in the layer it publishes. One
    without stays where it is defined -- it was put
    there by the stage's edit target when it was made, and a later change of that target
    must not drag it along -- unless it was defined in a layer an Edit Target chunk routed
    it to and no longer does (an anonymous layer that is neither the session nor the root
    layer). Then it goes where a new chunk would (chunk_utils.host_edit_layer).
    """
    routed = upstream_edit_target_layer(stage, prim)
    # Not into a layer an asset brings in by arc: /chunks is outside every asset's arc, so
    # the prim would leave the stage (chunk_utils.get_bookkeeping_layer).
    if routed is not None and stage.HasLocalLayer(routed):
        return routed
    home = chunk_utils.chunk_def_layer(prim)
    if home is None or not home.anonymous or home in (stage.GetSessionLayer(), stage.GetRootLayer()):
        return None
    return chunk_utils.host_edit_layer(stage)


def _arc_cook_target_stale(stage: Usd.Stage, chunk: core.ChunkBase, prim: Usd.Prim) -> bool:
    """Whether chunk is routed to a layer an asset brings in by arc but cooks somewhere else.

    Such a chunk was placed before the arc existed -- the Payload or Reference bringing the
    layer in cooks upstream of it -- so it went where it would without the Edit Target.
    Only arc layers: a local one is re-homed by moving the chunk's bookkeeping, which
    re-runs it.
    """
    routed = upstream_edit_target_layer(stage, prim)
    if routed is None or stage.HasLocalLayer(routed):
        return False
    lm = ChunkLayerManager()
    return lm.initialized and lm.get_chunk_target(chunk.get_hash()) is not routed


def reconcile_edit_targets(
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
    roots: list[core.ChunkBase] | None = None,
    cook: bool = True,
    protect_root_layer: bool = False,
) -> None:
    """Move every chunk's bookkeeping to the Edit Target resolved for its exec-chain position.

    For each regular (non-CONTEXT) chunk whose effective edit layer
    (_reconcile_destination) no longer matches where its bookkeeping prim lives, move
    the prim spec there and re-execute it so ChunkLayerManager.get_chunk_layer re-nests the
    chunk's driven content under the new target too (its graph layer reparents -- see
    chunk_registry.get_chunk_layer). A chunk already homed correctly moves nothing and is
    NOT re-executed, so on the common paths (creation authors bookkeeping into the right
    layer up front via _new_chunk_bookkeeping_layer) this is a cheap no-op and never
    oscillates. It does real work only when a target actually changes -- which is exactly
    when publish_edit_target gates its call on the marker having changed.

    Idempotent, so safe after any structural change (attach/detach/delete of an exec wire,
    or an Edit Target changing its layer). CONTEXT chunks (Tab containers, groups) are
    structural scaffolding pinned to the session layer, not content, so they are left alone.

    Args:
        stage: The stage.
        scene: The scene.
        roots: Narrows the pass to those chunks and everything downstream of them. A change
            at one place in the graph can only move the target of chunks at or below it, so
            a caller that knows where the change happened (wiring an edge, an Edit Target
            picking a new layer) can say so and skip the rest. None visits the whole scene,
            which is what a load or a delete -- where the change is not localised to one
            chunk -- still wants. This matters because the pass is O(chunks) and every wire
            fires one: on a graph of any size, visiting the whole scene per edge would make
            building it quadratic.
        cook: False migrates without re-running anything, for the one caller that is about
            to cook the whole graph anyway: loading. There the re-run is not just redundant
            but quadratic -- each migration would cascade a solve over everything downstream
            of it, once per chunk.
        protect_root_layer: Never delete specs from the stage's root layer while migrating,
            so opening a file cannot empty the file's own layer.
    """
    global _reconciling
    if _reconciling or not (stage and scene):
        return
    _reconciling = True
    try:
        affected = _chunks_downstream_of(roots) if roots is not None else None
        migratable = _migratable_layers(stage)
        if protect_root_layer:
            # Opening a file must not rewrite it. _migrate_chunk_bookkeeping moves a spec by
            # DELETING the original, and on a freshly opened file the original lives in that
            # file's own root layer -- so a load re-homing anything gutted the layer it had
            # just read. Sdf caches layers per process, so the emptied layer is what the next
            # Usd.Stage.Open of the same path gets: loading a network twice in one process
            # restored only the context the second time, while the file on disk stayed
            # correct. Excluding just the root layer keeps re-homing between the session and
            # anon layers, which is what settles the graph during a load.
            root = stage.GetRootLayer()
            migratable = [layer for layer in migratable if layer != root]
        for chunk in scene.get_chunks():
            if affected is not None and id(chunk) not in affected:
                continue
            if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                continue
            prim_path = get_chunk_prim_path(chunk)
            prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
            if not prim.IsValid():
                continue
            # A chunk composed in from a sequence file keeps its overrides in the host's
            # edit target (chunk_utils.host_edit_layer), not down its own -- the
            # sequence's -- Edit Target chain, so there is nothing of its to re-home.
            # Migrating would have carried a shot's override out of the shot file.
            if chunk_utils.is_composed_in(prim):
                continue
            if cook and _arc_cook_target_stale(stage, chunk, prim):
                # Its bookkeeping stays put (_reconcile_destination), so the move below never
                # re-runs it -- re-run here, so its cook re-nests in the arc layer.
                chunk.execute()
                migratable = _migratable_layers(stage)
                if protect_root_layer:
                    migratable = [layer for layer in migratable if layer != stage.GetRootLayer()]
            dst = _reconcile_destination(stage, chunk, prim)
            if dst is None:
                continue
            # Migration only moves opinions between layers, never changes what they
            # compose to, so the pass keeps the answers it has computed instead of
            # dropping them at the first move and re-walking the whole chain above the
            # chunk after it. The re-run below is outside the freeze -- that one can
            # author anything.
            with freeze_edit_layer_memo():
                moved = _migrate_chunk_bookkeeping(stage, prim_path, dst, migratable)
            if moved and cook:
                # The chunk moved layers -- re-run it so its driven content re-nests
                # under the new target (bookkeeping is hand-moved above; driven
                # content follows via ChunkLayerManager on execute).
                chunk.execute()
                # A cook can create and sublayer this chunk's procedural layer, so the
                # migratable set is stale from here on.
                migratable = _migratable_layers(stage)
                if protect_root_layer:
                    migratable = [layer for layer in migratable if layer != stage.GetRootLayer()]
    finally:
        _reconciling = False


def snapshot_edit_target_selections(stage: Usd.Stage) -> dict[str, str]:
    """Every Edit Target prim's stored selection, read straight off the stage before loading.

    Must be called BEFORE load_chunks -- restore_edit_target_selections needs it.

    Loading is itself destructive to these values. When a chunk.Menu is handed a value that
    isn't (yet) one of its menu_items, edit_target.py's refresh handler resets it to the
    default, and that reset is a param write, so it lands back in USD -- overwriting the saved
    selection with "Session Layer". Mid-load that happens routinely: an Edit Target pointing
    at "A" is rebuilt before the upstream Layer chunk has recreated layer A, so "A"
    isn't a menu item yet and the selection is destroyed on the way in.

    Reading afterwards therefore can't recover it, however carefully -- by then USD holds
    "Session Layer" and the real answer exists only in the file on disk. This runs first,
    while the stage still has exactly what was saved, and needs no chunks to exist yet since
    it reads raw attrs off prims.

    Args:
        stage: The stage about to be loaded.

    Returns:
        {prim path: selection}.
    """
    saved: dict[str, str] = {}
    if not stage:
        return saved
    for prim in stage.Traverse():
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
            continue
        type_attr = prim.GetAttribute(_CHUNK_TYPE_ATTR)
        if not (type_attr and type_attr.IsValid()) or type_attr.Get() != "edit_target":
            continue
        attr = prim.GetAttribute("inputs:edit_target")
        stored = attr.Get() if (attr and attr.IsValid()) else None
        if stored:
            saved[str(prim.GetPath())] = stored
    return saved


def restore_edit_target_selections(
    stage: Usd.Stage, scene: chunk_scene.SceneSender, saved: dict[str, str] | None = None
) -> None:
    """After a load, re-apply each Edit Target chunk's stored selection.

    A chunk.Menu rejects a value not in its menu_items, and on load the stored selection
    ("shot") is restored before the anon layer it names is recreated by the upstream Create
    Layer chunk -- so the Menu silently drops it and keeps the default ("Session Layer"). By
    the time the whole graph has loaded, every Layer chunk has run, so the layers (and
    therefore the menu items) exist; re-applying the stored value here restores the selection.
    The resulting value change re-publishes the marker and reconciles downstream via the
    normal path.

    Runs strictly post-load -- NOT from the refresh callback mid-load -- so a chain of Edit
    Targets settles deterministically. Applied UPSTREAM-FIRST: re-applying a downstream Edit
    Target before an upstream one leaves the upstream still on its (rejected) default while
    the downstream's execute/reconcile cascade runs, which corrupts the upstream's selection.
    Ordering by how many Edit Targets sit upstream keeps each chain settling from its root out.

    Args:
        stage: The loaded stage.
        scene: The scene.
        saved: The pre-load snapshot from snapshot_edit_target_selections, and the only
            trustworthy source for these values: the Menu's rejection does not merely drop
            the value in memory, it writes the default back to USD, so a selection whose
            layer didn't exist yet at the moment its chunk was rebuilt is already gone from
            the stage by the time this runs. None falls back to reading the stage, which
            still covers any selection that happened to survive load.
    """
    if not (stage and scene):
        return
    edit_targets = [c for c in scene.get_chunks() if is_edit_target_chunk(c)]
    if not edit_targets:
        return
    et_paths = {get_chunk_prim_path(c) for c in edit_targets}

    def _upstream_et_count(chunk) -> int:
        start = Sdf.Path(get_chunk_prim_path(chunk))
        return sum(
            1 for path, _p, _unbranched in _iter_exec_upstream(stage, start)
            if str(path) in et_paths
        )

    # Resolve each stored selection BEFORE applying any of them. Applying one target's value
    # triggers an execute/reconcile cascade that can overwrite a *different* target's stored
    # inputs:edit_target attr, so re-reading per-iteration would pick up a corrupted value.
    # Resolve first, then apply.
    snapshot = []
    for chunk in edit_targets:
        prim_path = get_chunk_prim_path(chunk)
        prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not prim.IsValid():
            continue
        stored = (saved or {}).get(prim_path)
        if not stored:
            attr = prim.GetAttribute("inputs:edit_target")
            stored = attr.Get() if (attr and attr.IsValid()) else None
        param = chunk.get_param("edit_target")
        if param is not None and stored:
            snapshot.append((chunk, param, stored))

    # Apply upstream-first: each set's own publish_edit_target reconcile re-homes the
    # downstream part of the chain before the next (more-downstream) selection is applied,
    # so every target settles against an already-correct upstream.
    for chunk, param, stored in sorted(snapshot, key=lambda t: _upstream_et_count(t[0])):
        # Only re-apply once the stored selection is actually a valid menu item now (its
        # layer exists); a genuinely missing layer is left at whatever the Menu settled on.
        if param.value != stored and stored in getattr(param, "menu_items", []):
            param.value = stored


def _active_scene() -> chunk_scene.SceneSender | None:
    """The SceneSender to reconcile against, or None if there is no scene yet.

    ChunkStageSync is the usual holder, but it only gains a _scene once initialize() has run,
    and the headless CLI (usdchunks run) deliberately never initializes it -- it loads chunks
    straight onto a stage with no live sync. The registry's scene is set by every host
    including that one, so it's the reliable fallback.
    """
    sync = ChunkStageSync()
    if sync.initialized and getattr(sync, "_scene", None):
        return sync._scene
    registry = chunk_registry.ChunkRegistry()
    return registry.scene if registry.initialized else None


def publish_edit_target(chunk: core.ChunkBase, stage: Usd.Stage, layer: Sdf.Layer | None) -> None:
    """Record the layer an Edit Target chunk targets, then re-home everything downstream to it.

    The record is the _EDIT_TARGET_MARKER attr on the chunk's prim, read by
    nearest_upstream_edit_layer. This is the single entry point the Edit Target chunk's
    execute() calls, which keeps the marker format and the downstream migration owned by
    core, not the script.

    The marker is updated in place at its existing home (get_attr_home_layer) so re-pointing
    the Edit Target repeatedly never leaves a stale stronger opinion shadowing the new one --
    same discipline as set_chunk_pinned.

    Args:
        chunk: The Edit Target chunk.
        stage: The stage.
        layer: The layer it targets, or None for none.
    """
    if not (stage and chunk):
        return
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not prim.IsValid():
        return
    ident = layer.identifier if layer else ""
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        attr = prim.GetAttribute(_EDIT_TARGET_MARKER)
        old_ident = attr.Get() if attr and attr.IsValid() else None
        home = chunk_utils.get_attr_home_layer(attr) if attr and attr.IsValid() else None
        target = home or chunk_utils.get_bookkeeping_layer(stage, chunk)
        with Usd.EditContext(stage, target):
            prim.CreateAttribute(_EDIT_TARGET_MARKER, Sdf.ValueTypeNames.String, custom=True).Set(ident)
    finally:
        chunk_notice._usd_write_active = prev
    # The marker this whole memo is a search for just moved, and the write above ran with
    # notices suppressed, so nothing else will drop it.
    invalidate_edit_layer_memo()
    # Only re-home downstream when the target actually changed. The Edit Target chunk
    # re-executes on every stage content change (its refresh callback), so reconciling
    # unconditionally here would run -- and thrash -- on every unrelated edit.
    # Or when the layer is one an asset brings in by arc and has only now become reachable:
    # the Payload or Reference bringing it in cooks upstream of this, so what is routed to
    # it was placed before it could be, with the marker unchanged since. Once, not on every
    # run -- reachable it stays, and the reconcile then has nothing to do.
    reachable = bool(layer is not None and not stage.HasLocalLayer(layer)
                     and chunk_utils.arc_edit_target(stage, layer) is not None)
    became_reachable = reachable and not getattr(chunk, "_arc_layer_reachable", False)
    chunk._arc_layer_reachable = reachable
    if old_ident != ident or became_reachable:
        # protect_root_layer while loading -- see _loading. This branch is taken on EVERY
        # open, because an anonymous layer's identifier is new in every process.
        reconcile_edit_targets(stage, _active_scene(), protect_root_layer=_loading)


# ---------------------------------------------------------------------------
# Exec graph: what is wired to what on the stage, and view-driven cooking
#
# Two readings of "upstream", on purpose, and they must not be merged:
#   view_cook_list  every wire (exec, param, loft), ordered -- what cooks for a view
#   exec_upstream   exec wires only, unordered -- which chunks make up a branch
# Their answers differ exactly where it matters: a param-wired or lofted chunk is cooked
# for the view but is not part of the exec branch.
# ---------------------------------------------------------------------------


def view_cook_list(
    view_chunk: core.ChunkBase,
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
) -> list[core.ChunkBase]:
    """The chunks feeding view_chunk, following ALL input connections backward.

    This is the *cook set* for a view: view-driven execution runs only these chunks.
    For the exec-only branch, see exec_upstream. Follows both the exec wire (inputs:chunk)
    AND data connections (any inputs:* to a source prim), so a node feeding another purely
    via data (e.g. a Transform between a Scatter and an RBD solver that reads its points) is
    still cooked -- otherwise it would be blocked and its downstream would read empty/stale
    input.

    Args:
        view_chunk: The view.
        stage: The stage.
        scene: The scene.

    Returns:
        The chunks, roots first (a node's sources appear before it). The view chunk itself
        is excluded.
    """
    prim_to_chunk = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
    view_path = get_chunk_prim_path(view_chunk)

    def sources_of(path: str) -> list[str]:
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        if not prim.IsValid():
            return []
        srcs: list[str] = []
        for attr in prim.GetAttributes():
            if not attr.GetName().startswith(_INPUTS_PREFIX):
                continue
            for conn in attr.GetConnections():
                src = str(conn.GetPrimPath())
                if src in prim_to_chunk:
                    srcs.append(src)
        # A weaker department's chain feeds this one's first chunk through the stack.
        srcs.extend(src for src in implied_sources(prim) if src in prim_to_chunk)
        if not is_merge_prim(prim):
            return srcs
        # A merge's slot order is what decides which branch wins, and visit order is what
        # produces that ranking: visit() appends a node only after its sources, so whichever
        # branch is visited FIRST lands earliest in cook_list -- and recook_for_view ranks
        # later-in-cook_list as STRONGER. Visiting the slots in descending order therefore
        # leaves slot 0 visited last, ranked highest, and strongest, which is the documented
        # "first input wins". iter_exec_inputs is strongest-first, hence the reverse.
        #
        # Branches are disjoint, so each stays contiguous in cook_list whatever its length.
        # Where two slots share an upstream node, visit()'s `seen` keeps it at its FIRST
        # visit -- i.e. with the weakest of the branches that reach it. Well-defined, and the
        # only stable choice: a shared node cannot sit at two strengths at once.
        exec_srcs = [
            src
            for attr in reversed(iter_exec_inputs(prim))
            for conn in attr.GetConnections()
            if (src := str(conn.GetPrimPath())) in prim_to_chunk
        ]
        exec_set = set(exec_srcs)
        return [s for s in srcs if s not in exec_set] + exec_srcs

    order: list[core.ChunkBase] = []
    seen: set = set()

    def visit(path: str) -> None:
        if path in seen:
            return
        seen.add(path)
        for src in sources_of(path):
            visit(src)
        if path != view_path and (c := prim_to_chunk.get(path)) is not None:
            order.append(c)

    visit(view_path)
    return order


def recook_for_view(
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
    context: core.ChunkBase | None,
    view_chunk: core.ChunkBase | None,
    _synthetic_tail: bool = False,
    _force: bool = False,
    _hidden: bool = False,
) -> None:
    """Apply view-driven execution for one context.

    With a view_chunk: reveal only its upstream tree (roots-first); every other non-view
    chunk in the context is blocked + muted (dormant, hidden). With view_chunk=None (no active
    view) the context cooks to its Output: a view is for looking, the Output is what the
    graph delivers. With neither, block + mute everything -- nothing cooks.

    Blocking a chunk both hides its result (its layer is muted) and stops it reacting to
    param changes (see ChunkBase.value_changed) -- so being outside the active view's tree
    means "does not execute", live.

    Of the revealed tree, only the chunks that are stale (chunk_cook.is_cook_stale) cook.
    Muting keeps a layer's content, so a chunk that already cooked on its current inputs
    comes back with that result, and running it again would redo the same work -- for an
    import that parses a mesh file, on every view switch and every wire. A chunk that cooks
    reaches everything downstream of it through the DAG while those are still muted, which
    marks them stale in turn, so a change upstream still recooks the whole chain below it.

    Args:
        stage: The stage.
        scene: The scene.
        context: The context to recook, or None for the top level.
        view_chunk: The active view, or None for none.
        _synthetic_tail: True when view_chunk is a stand-in exec tail rather than a real
            view chunk (see the is_group_chunk branch). Real view chunks are markers excluded
            from their own cook_list by view_cook_list, but a synthetic tail is real content
            that must cook too, so it's appended back in rather than excluded.
        _force: Cook every revealed chunk, stale or not. Set for the inside of a group
            that is itself stale: a change reached the group, and its inner chunks read
            what it brings in.
        _hidden: The context is outside its parent's cook set, so it cooks nothing at all,
            Output or not.
    """
    lm = chunk_registry.ChunkLayerManager()
    disabled = ChunkFlags.State.DISABLED
    # No active view: cook to the Output the graph delivers -- with departments sublayered,
    # the strongest layer's, which reaches the weaker ones through implied edges. Handed an
    # Output (a run, a sweep, a group's driver) means the same: deliver.
    delivering = (view_chunk is None and not _hidden) or (
        view_chunk is not None and is_output_chunk(view_chunk))
    outputs = graph_outputs(stage, scene, context) if delivering else []
    if delivering:
        view_chunk = outputs[-1] if outputs else None

    def in_context(c: core.ChunkBase) -> bool:
        # Node membership (prim-path based) so a group is managed by its PARENT graph's recook
        # and the group's inner chunks by the group's own recook -- not conflated by get_context().
        return context is None or is_node_in_context(c, context)

    def mute(layer, muted: bool) -> None:
        # Use stage.Mute/UnmuteLayer, NOT layer.SetMuted -- SetMuted recomposes lazily and
        # clears geometry / expires prims (the "chunk layers disappear" bug). stage muting is
        # the pattern the disable path already uses.
        if layer is None:
            return
        # Already so: a stage mute sends its notices and recomposes even when it changes
        # nothing, and every listener -- the host's viewport among them -- hears it.
        if stage.IsLayerMuted(layer.identifier) == muted:
            return
        if muted:
            stage.MuteLayer(layer.identifier)
        else:
            stage.UnmuteLayer(layer.identifier)
        # Muting drops the layer out of GetLayerStack, so an Edit Target marker naming it
        # stops resolving. _on_contents_changed also catches this today, but only because it
        # invalidates on everything: USD reports a mute with no changed paths at all, so if
        # that handler is ever narrowed to the changes it can actually identify, this is the
        # only place the mute is still visible from.
        invalidate_edit_layer_memo()

    if view_chunk is None:
        cook_list: list[core.ChunkBase] = []
        cook_set: set = set()
    elif delivering:
        # Every Output, weakest layer first; implied edges order them already, and a chunk
        # two Outputs reach cooks in the earlier one's turn.
        cook_list = []
        cook_set = set()
        for output in outputs:
            for c in view_cook_list(output, stage, scene):
                if c not in cook_set:
                    cook_set.add(c)
                    cook_list.append(c)
    else:
        cook_list = view_cook_list(view_chunk, stage, scene)
        if _synthetic_tail:
            cook_list.append(view_chunk)
        cook_set = set(cook_list)

    def will_cook(c: core.ChunkBase) -> bool:
        # The execute loop's own test, below, read ahead of it.
        if is_group_chunk(c):
            return _force or chunk_cook.is_cook_stale(c)
        return (not c.has_state(disabled) and c.can_cook()
                and ((_force and in_context(c)) or chunk_cook.is_cook_stale(c)))

    # The chunks ahead of the first one that cooks: nothing above them changes in this pass,
    # so the result their layers hold is the one they would show at their turn anyway. They
    # stay visible instead of being muted and revealed again, which recomposed the stage --
    # and had the viewport rebuild everything they author -- once per chunk on every wire,
    # view switch and drop, however little changed. Appending a node to the end of a long
    # chain cooks that node alone.
    fresh: set = set()
    for c in cook_list:
        if will_cook(c):
            break
        if not is_group_chunk(c) and not c.has_state(disabled):
            fresh.add(id(c))

    # Block/mute leaf nodes by cook membership (blocking non-cook chunks first means the
    # ObjectsChanged from muting can't re-execute them). Groups are NEVER blocked as a node -- a
    # group is its own cook domain, so its parent-visibility block must not propagate to its
    # children via the owner chain (that left the inner view unable to cook unless the group was
    # wired to a parent view). Instead recurse: a cooked group cooks its inner active view's tree;
    # a hidden group mutes its whole inner tree. Prim-path nesting prevents cycles.
    #
    # Cook-set members are unblocked here but stay MUTED for now -- revealed one at a time in the
    # execute loop below, immediately before each one's own turn. Unmuting the whole upstream
    # tree upfront (the previous behaviour) meant a chunk that reads composed state during its
    # own execute() (e.g. Scatter reading a mesh's transform) could see a downstream chunk's
    # stale content that hadn't re-executed yet in this pass -- the final "closer to view wins"
    # layer-strength ranking below is a real, correct rule for the settled result once everyone
    # has run, but it has nothing to do with what should be visible mid-pass while a chunk is
    # still reading its own upstream.
    mute_idents: list = []
    seen_idents: set = set()
    # scene.get_chunks() is a DFS via stack.pop() (LIFO) over insertion order -- roughly
    # reverse creation order, with no relation to exec/view proximity. That's harmless for
    # blocking/muting a chunk on its own. Groups outside the cook set are muted from here
    # (their own recook_for_view with no view); groups inside it are cooked later, in the
    # execute loop, in cook_list order -- roots-first, so the group closest to the view runs
    # last and a Layer-style chunk inside it, which reinserts itself at the front of a
    # SHARED parent layer's subLayerPaths on every execute, ends up strongest. Visited in
    # cook_list order here too; anything outside the cook set isn't competing for layer
    # strength at all, so its relative order doesn't matter.
    cook_rank = {id(c): i for i, c in enumerate(cook_list)}
    deferred_groups: set = set()
    for c in sorted(scene.get_chunks(), key=lambda c: cook_rank.get(id(c), -1)):
        if is_terminal_chunk(c) or not in_context(c):
            continue
        if is_group_chunk(c):
            if c in cook_set:
                # Cooked at its turn in the loop below, not here. Here is before any of
                # the context's own chunks have been revealed and cooked, so a group cooked
                # now would read a stage its upstream is not on yet, and a chunk inside it
                # would find none of the prims it acts on.
                deferred_groups.add(id(c))
            else:
                recook_for_view(stage, scene, c, None, _hidden=True)
            continue
        was_blocked = c.is_blocked()
        c.set_blocked(c not in cook_set)
        if was_blocked and not c.is_blocked():
            # A blocked chunk's param value-changed notices are suppressed (see this
            # function's own docstring: blocking "stops it reacting to param changes"),
            # which was meant to stop it from re-executing while hidden -- but the same
            # suppression also silently drops _sync_param_input, the one-time step that
            # first authors a param's value into a USD attr at all. A freshly created,
            # not-yet-wired chunk is blocked (nothing reachable from any view yet), so a
            # value typed into it before wiring never got an attr; nothing later ever
            # retried, since _sync_param_input only runs reactively off that same
            # suppressed notice. Catch up here, the one place a chunk's blocked state
            # actually flips off -- _sync_param_input is already idempotent (skips a
            # param already wired, or still at its default), so this is a no-op for
            # every param that didn't hit the gap.
            # Per chunk and caught: this is a catch-up, and one chunk failing it must not
            # abort the pass. It runs mid-pass, after the blocks and before the unmute and
            # execute loop, so a raise here left every chunk in the context blocked and
            # muted -- the whole scene gone -- over a param on one chunk.
            sync = ChunkStageSync()
            try:
                for _, param in _iter_chunk_params(c):
                    sync._sync_param_input(c, param)
                for _, vector in _iter_chunk_vectors(c):
                    sync._sync_vector_input(c, vector)
            except Exception:
                logger.exception("could not sync the inputs of %s", get_chunk_prim_path(c))
        layer = lm.get_layer(c)
        if layer is not None and layer.identifier not in seen_idents:
            seen_idents.add(layer.identifier)
            # Visited in cook_list order, so a layer a fresh chunk shares with one further
            # down stays visible -- as it would have from the fresh chunk's turn anyway.
            if id(c) not in fresh:
                mute_idents.append(layer.identifier)

    # One recomposition for the whole "hide every non-view leaf" pass -- each stage.MuteLayer
    # recomposes the stage, so muting N leaves one at a time was N recompositions on every view
    # switch. All set_blocked() calls above run first, so the block-before-mute ordering the
    # comment relies on holds even more strictly (every block precedes the single mute).
    # Only the ones not muted already: a call that changes nothing still recomposes.
    mute_idents = [ident for ident in mute_idents if not stage.IsLayerMuted(ident)]
    if mute_idents:
        stage.MuteAndUnmuteLayers(mute_idents, [])

    for c in cook_list:
        if is_group_chunk(c):
            if id(c) in deferred_groups:
                inner_view, synthetic = _group_output_driver(stage, scene, c)
                recook_for_view(
                    stage, scene, c, inner_view, _synthetic_tail=synthetic,
                    _force=_force or chunk_cook.is_cook_stale(c),
                )
                chunk_cook.clear_cook_stale(c)
            continue
        if not c.has_state(disabled):
            mute(lm.get_layer(c), False)
            # A chunk outside this graph that it still reads -- a referenced network's, through
            # an implied edge -- was left out of the unblocking above, which goes by graph,
            # and a focus elsewhere may have blocked it. Blocked, it would cook here once and
            # then ignore every edit.
            if not in_context(c) and c.is_blocked():
                c.set_blocked(False)
            if not c.can_cook():
                # Nothing to cook -- a chunk that works through callbacks (Property Editor).
                # Nothing ever clears its stale mark either, so executing it would push a
                # DAG solve to everything downstream on every recook. Whatever a change
                # upstream of it means for those, their own marks already say.
                chunk_cook.clear_cook_stale(c)
            # _force is for the group's own inside. A group's cook_list also reaches back
            # through its input into the parent graph, and those chunks are the parent's
            # to cook -- forcing them re-ran the whole upstream chain on every stale group.
            elif (_force and in_context(c)) or chunk_cook.is_cook_stale(c):
                c.execute()

    if not cook_list:
        return

    # Layer-win order follows the exec traversal: the chunk nearest the view (last in
    # cook_list) must win, so reorder each graph layer's sublayers to put the most
    # downstream layer strongest (subLayerPaths[0]). This is what makes rewiring change
    # who-wins for chunks that author to the same target. (Safe re: prim expiry -- this
    # reorders the result layers under the graph root, not the chunk prims in the edit layer.)
    rank = {id(c): i for i, c in enumerate(cook_list)}  # higher = more downstream = stronger
    # Tuple ranks: (cook_list position, 0-for-the-chunk-itself-or-1-for-a-descendant).
    # The second element only matters for ties on the first -- see the descendant loop
    # below for why those ties exist and must break in the descendant's favor.
    ident_rank: dict = {}
    for c in cook_list:
        layer = lm.get_layer(c)
        if layer is not None:
            ident_rank[layer.identifier] = (rank[id(c)], 0)
    # A ranked group's own children are ranked by a completely separate, incomparable
    # numbering scheme -- their own inner recook's cook_list -- so from THIS call's
    # perspective they're simply invisible descendants, not competitors for a tier of
    # their own. Left alone they would sit wherever they were first inserted instead of
    # ranking anywhere near the group that actually produced them. Give every descendant of a ranked group the group's own
    # rank tier, strictly above the group's own layer within that tier (a group's
    # actual produced content should win over its own generic bookkeeping, and a plain
    # equal rank would leave that ambiguous -- tie-broken arbitrarily by whatever the
    # two happened to be in before this sort, not something to depend on). The stable
    # sort in _reorder_graph_layers_by_rank then keeps whatever relative order the
    # group's own inner recook already established among the descendants themselves
    # (their own ranks tie 1-vs-1, so stay untouched relative to each other), while
    # moving the whole cluster to sit where the group belongs among its outer siblings.
    for c in cook_list:
        if not is_group_chunk(c):
            continue
        prefix = get_chunk_prim_path(c) + "/"
        for other in scene.get_chunks():
            if id(other) in rank:
                continue
            if get_chunk_prim_path(other).startswith(prefix):
                layer = lm.get_layer(other)
                if layer is not None:
                    ident_rank.setdefault(layer.identifier, (rank[id(c)], 1))
    _reorder_graph_layers_by_rank(lm.graph_layers.values(), ident_rank)
    _reorder_graph_layers_in_targets(lm, ident_rank)


def _group_output_driver(stage: Usd.Stage, scene: chunk_scene.SceneSender, group: core.ChunkBase):
    """(what drives group's output, whether it is a stand-in exec tail rather than a view).

    The group's Output, when it has one wired to something: what a group delivers is its
    Output, and its inner views are only for looking while inside it. Older groups with no
    Output fall back to the inner active view, then the exec tail.
    """
    output = find_output_chunk(stage, scene, group)
    if output is not None and view_cook_list(output, stage, scene):
        return output, False
    inner_view = find_group_active_view(group, stage, scene)
    synthetic = False
    if inner_view is not None and not view_cook_list(inner_view, stage, scene):
        # A view wired to NOTHING is not an answer to "what does this group
        # output" -- it is a default view that never got pointed at anything,
        # or one left dangling when the chunk it watched was deleted (an exec
        # heal clears the connection when the deleted node had no predecessor
        # to inherit). Taken literally it means the group cooks nothing at all:
        # its contents go blocked and muted, it stops feeding everything
        # downstream of it, and there is no sign of why.
        #
        # Fall through to the exec-tail fallback below, which is the same
        # question ("what is the end of this group's chain") asked of a group
        # whose view is missing rather than empty.
        inner_view = None
    if inner_view is None:
        # No real inner view chunk -- the common case for a group whose
        # contents were never explicitly given one (view chunks are
        # deliberately excluded from group copy/paste, see
        # _iter_group_children). Without this fallback, a reachable group's
        # contents never actually cook via view-driven execution at all --
        # they only ever ran as an accidental side effect of the user diving
        # into the group, which auto-wires a default view as a UI action
        # (ChunkGraphView._ensure_default_view). That made execution order
        # (and therefore which sibling's self-managed layer, e.g. Create
        # Layer's, ends up strongest) depend on dive/rename order instead of
        # actual graph position. Cook through the group's own exec tail
        # instead, exactly as if a real inner view were pointed at it.
        tails = exec_tails(stage, scene, group)
        inner_view = tails[0] if tails else None
        synthetic = inner_view is not None
    return inner_view, synthetic


def _reorder_graph_layers_by_rank(graph_layers: Iterable[Sdf.Layer], ident_rank: dict) -> None:
    """Reposition each graph layer's sublayers so higher-ranked idents come first.

    subLayerPaths[0] is strongest. A chunkgroup's own children share their parent's physical
    graph layer -- "chunkgroups are subnets, not spatial containers" -- so the SAME graph
    layer gets this treatment once per context, recursively: a group's own inner recook runs
    first and ranks its children by proximity to ITS view, then this same step runs again
    for the OUTER context, whose ident_rank knows nothing about those children at all. A
    global sort keyed on ident_rank.get(ident, -1) would shove every ident a given call
    doesn't recognize to the single weakest position, silently destroying a more specific
    call's ordering underneath it -- a chunkgroup's own content would rank weaker than its
    unrelated siblings in the same tab.

    Only idents this call actually has a rank for are repositioned, among the slots they
    collectively occupy -- everything else (including a nested group's own children) stays
    exactly where a more specific call already put it.
    """
    for g_layer in graph_layers:
        _reorder_sublayers(g_layer, ident_rank)


def sort_chunk_layers(lm: ChunkLayerManager, chunks: Iterable[core.ChunkBase]) -> None:
    """Order chunk layers by chain depth (layer_sort_key), for a graph with no View to do it.

    Each chunk layer is placed when it is made, before it is wired, so placement alone
    leaves creation order and an upstream chunk's opinions win. A View re-sorts on every
    recook; this does the same for a load, or a script still building.

    Args:
        lm: The layer manager.
        chunks: The chunks whose layers to sort.
    """
    rank = {
        layer.identifier: chunk_scheduler.layer_sort_key(c.get_dag_node())
        for c in chunks
        if (layer := lm.get_layer(c)) is not None
    }
    # A snapshot: writing subLayerPaths can cascade into get_or_create_layer, which edits
    # graph_layers while this loop would still be reading it.
    _reorder_graph_layers_by_rank(list(lm.graph_layers.values()), rank)


def _reorder_sublayers(layer: Sdf.Layer, ident_rank: dict) -> None:
    """Sort layer's ranked sublayers strongest first, within the slots they already hold."""
    current = list(layer.subLayerPaths)
    slots = [i for i, ident in enumerate(current) if ident in ident_rank]
    if not slots:
        return
    ranked = sorted((current[i] for i in slots), key=lambda ident: ident_rank[ident], reverse=True)
    ordered = list(current)
    for slot, ident in zip(slots, ranked):
        ordered[slot] = ident
    if ordered != current:
        layer.subLayerPaths = ordered  # type: ignore


def _reorder_graph_layers_in_targets(lm: ChunkLayerManager, ident_rank: dict) -> None:
    """Order the graph layers themselves by the strongest chunk each one holds.

    Ranking chunks only within their own graph layer would leave the graph layers in the
    order they were made, newest on top. A graph layer is one DAG component, and a chunk
    that builds its own children (AMD MaterialX Library) puts them in a component of their
    own -- which would then outrank the last chunk before the view.

    By the strongest member because a graph layer moves as a whole: one holding both the
    chunk next to the view and one far up the chain cannot be split around another, and
    the chunk next to the view is the one the rule is about. Only graph layers holding a
    ranked chunk move, among the slots they already hold -- the same rule as within a graph
    layer, so real sublayers on the target stay put and a nested group's order survives.
    """
    by_target: dict = {}
    for gkey, g_layer in lm.graph_layers.items():
        target = lm._graph_layer_target.get(gkey)
        if target is None:
            continue
        ranks = [ident_rank[i] for i in g_layer.subLayerPaths if i in ident_rank]
        if ranks:
            by_target.setdefault(target.identifier, (target, {}))[1][g_layer.identifier] = max(ranks)
    if not by_target:
        return
    # Graph layers are internal: moving them changes no chunk's Edit Target, and a sublayer
    # edit on the session layer must not send the stage through a rescan for new chunks.
    prev_w = chunk_notice._usd_write_active
    prev_internal = chunk_notice._internal_layer_write_active
    chunk_notice._usd_write_active = True
    chunk_notice._internal_layer_write_active = True
    try:
        for target, g_rank in by_target.values():
            _reorder_sublayers(target, g_rank)
    finally:
        chunk_notice._usd_write_active = prev_w
        chunk_notice._internal_layer_write_active = prev_internal


def recook_focus(
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
    context: core.ChunkBase | None,
    view_chunk: core.ChunkBase | None,
) -> None:
    """Establish the GLOBAL stage for the tab/graph the user is looking at.

    The stage is shared by every context, but only the focused one should drive it. Root
    (context is None) is already a full global establish -- recook_for_view(None) touches
    every chunk and recurses into all groups. But a GROUP focus only manages its own
    children, so the parent tab's active view (and sibling groups) would stay unmuted and
    bleed through ("switch tabs, still see the previous view"). So when focusing a group,
    first mute + block everything OUTSIDE the group's subtree, then recook inside it --
    leaving only the group's inner active view driving the stage.

    Args:
        stage: The stage.
        scene: The scene.
        context: The focused context, or None for the top level.
        view_chunk: The focused context's active view, or None.
    """
    if context is not None:
        lm = chunk_registry.ChunkLayerManager()
        group_path = get_chunk_prim_path(context)
        inside_prefix = group_path + "/"
        # A focused group's own UPSTREAM must stay composed. The group's inner chunks
        # author on top of that content -- BindMaterial inside AMD MaterialX Library
        # binds a material onto a mesh that a Sublayer several nodes upstream is what
        # brings onto the stage at all. Muting it (as this loop otherwise does for
        # everything outside the group) means those inner chunks execute against a stage
        # where their target prims simply don't exist, so they author nothing -- and
        # since a chunk's execute() Clear()s its own layer first, whatever it authored
        # last time is destroyed in the process, and nothing re-executes it on the way
        # back out once its params haven't changed.
        #
        # Muting the group's upstream is not the point of this pass anyway -- it
        # exists to stop the parent tab's active view and UNRELATED sibling branches
        # bleeding through, and the group's own upstream is neither.
        # Every ANCESTOR's upstream, not just the focused group's. A group nested inside
        # another is fed by its parent's chain: focusing it, the parent's own upstream --
        # the Sublayer bringing the scene onto the stage two levels up -- must stay in this
        # set, or the inner chunks cook against a stage their targets are not on.
        upstream_keep: set = set()
        by_path = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
        node: core.ChunkBase | None = context
        seen_ancestors: set = set()
        while node is not None:
            node_path = get_chunk_prim_path(node)
            if node_path in seen_ancestors:
                break  # a cycle would hang the focus; prim nesting should prevent it
            seen_ancestors.add(node_path)
            upstream_keep.update(
                get_chunk_prim_path(u) for u in view_cook_list(node, stage, scene)
            )
            parent_path = str(Sdf.Path(node_path).GetParentPath())
            node = by_path.get(parent_path)
        # An upstream GROUP authors nothing of its own -- all of its content lives in its
        # children's layers -- so keeping only the group's own (empty) layer keeps nothing at
        # all: with chunkgroup A -> chunkgroup B, diving into B would drop A's prims from
        # the stage, so B's inner chunks would cook against a stage their input isn't on.
        # A data wire hides this, because lofting/connecting the group's Description
        # authors a connection from the group prim DOWN to an inner chunk, which puts those
        # inner chunks into view_cook_list's node-level answer.
        keep_prefixes = tuple(u + "/" for u in upstream_keep)
        for c in scene.get_chunks():
            if is_terminal_chunk(c):
                continue
            cp = get_chunk_prim_path(c)
            # Keep the focused group itself and its descendants -- recook_for_view handles those.
            if cp == group_path or cp.startswith(inside_prefix):
                continue
            if cp in upstream_keep or (keep_prefixes and cp.startswith(keep_prefixes)):
                # Leave an upstream group's internals exactly as the parent context's cook
                # left them: whichever of its children that cook muted (outside the group's
                # own active view) stay muted, and the ones driving its output stay composed.
                continue
            # NEVER block a context/container (a tab or group). is_blocked() climbs the owner
            # chain, so blocking the focused group's ANCESTOR would propagate down and block the
            # focused inner tree itself -- nothing would cook. Container chunks author nothing of
            # their own; their leaf children are muted individually by this same loop.
            if c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                continue
            c.set_blocked(True)
            layer = lm.get_layer(c)
            if layer is not None:
                stage.MuteLayer(layer.identifier)
    recook_for_view(stage, scene, context, view_chunk)


def _exec_consumers(stage: Usd.Stage) -> dict[str, set[str]]:
    """Source prim path -> the prim paths whose exec inputs take it, over the whole stage.

    Every slot: a chunk feeding a merge's slot 2 is as consumed as one feeding slot 0, and
    calling it a tail would wire a view to the middle of a live branch. One traversal,
    shared by exec_downstream and exec_tails.

    Only Chunk prims carry exec inputs, so everything else is skipped on its type name:
    reading the attributes of every scene prim (a landscape's thousands) on each call was
    seconds of a scatter-heavy cook, as every Scatter asks for its upstream stage.
    """
    consumers: dict[str, set[str]] = {}
    for prim in stage.Traverse():
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
            continue
        path = str(prim.GetPath())
        for conn in exec_input_connections(prim):
            consumers.setdefault(str(conn.GetPrimPath()), set()).add(path)
    return consumers


def exec_tails(
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
    context: core.ChunkBase | None = None,
) -> list[core.ChunkBase]:
    """Non-view chunks in context whose exec output nothing takes: the ends of the chains.

    Wiring a view to a tail makes the view cook that tail's whole upstream chain. Used to
    auto-wire a default view.

    Args:
        stage: The stage.
        scene: The scene.
        context: The context, or None for the top level.

    Returns:
        The tail chunks.
    """
    consumed = _exec_consumers(stage)
    return [
        c for c in scene.get_chunks()
        if not is_terminal_chunk(c) and is_node_in_context(c, context)
        and get_chunk_prim_path(c) not in consumed
    ]


def _connected_source_paths(prim: Usd.Prim) -> set[str]:
    """Prim paths this chunk prim reads from: every exec slot plus every typed input.

    Both kinds of wire count as a dependency for ordering. A param connection is authored
    on the *destination* attr pointing at the source (see ChunkStageSync._expr_connections),
    so reading this prim's own inputs: connections gives the chunks it depends on.
    """
    my_path = prim.GetPath()
    sources = {str(conn.GetPrimPath()) for conn in exec_input_connections(prim)}
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith(_INPUTS_PREFIX) or is_exec_input_name(name):
            continue
        for conn in attr.GetConnections():
            target = Sdf.Path(str(conn)).GetPrimPath()
            if target == my_path:
                continue
            if my_path.HasPrefix(target):
                # An ancestor target is a loft (this param promoted to its group's
                # interface), not a sibling dependency -- the group contains this chunk, so
                # treating it as an input would invert the real order and can cycle.
                continue
            sources.add(str(target))
    return sources


def _is_placeholder_prim(prim: Usd.Prim, has_chunk: Callable[[str], bool]) -> bool:
    """Whether prim is a chunk prim no module backs -- a type missing, renamed or off
    USD_CHUNK_DIRS. load_chunks leaves it without a chunk, so the graph shows it by its
    USD type alone and nothing in the DAG stands where it is.

    Both halves are needed. A chunk whose config names an unregistered module still has
    a prim carrying that type, and it is a real node: walking past it would drop it from
    its downstream's dependencies. So the registry is asked first, being cheap, and only
    a type it does not know is checked against has_chunk.
    """
    if not prim or not prim.IsValid() or prim.GetTypeName() != _CHUNK_PRIM_TYPENAME:
        return False
    chunk_type = prim.GetAttribute(_CHUNK_TYPE_ATTR).Get()
    if not chunk_type or chunk_registry.ChunkRegistry().get_chunk_module(str(chunk_type)):
        return False
    return not has_chunk(str(prim.GetPath()))


def _through_placeholders(
    stage: Usd.Stage | None, paths: Iterable[str], has_chunk: Callable[[str], bool]
) -> list[str]:
    """paths, with every placeholder chunk prim replaced by what feeds its exec slots.

    A placeholder has no chunk, so an edge to it resolves to nothing and is dropped -- and
    the chunk downstream of it came out with no dependency at all. It became a root: the
    load cooked it first, before the chain it is wired to, against a stage that did not
    hold that chain's output yet. A wired node must never be a root. Wired through the
    placeholder, it waits for everything the placeholder was waiting for, which is the
    order the file says -- only minus whatever the missing chunk would have authored.

    Transitive, since placeholders can be chained, and in order, strongest branch first.
    has_chunk says whether a chunk stands at a prim path (see _is_placeholder_prim).
    """
    out: list[str] = []
    seen: set[str] = set()

    def visit(path: str) -> None:
        if path in seen:
            return
        seen.add(path)
        prim = stage.GetPrimAtPath(Sdf.Path(path)) if stage else None
        if prim is not None and _is_placeholder_prim(prim, has_chunk):
            for conn in exec_input_connections(prim):
                visit(str(conn.GetPrimPath()))
        else:
            out.append(path)

    for path in paths:
        visit(path)
    return out


def commit_order_from_stage(
    chunks: list[core.ChunkBase], stage: Usd.Stage | None
) -> list[core.ChunkBase]:
    """Order chunks for commit by what the stage says they read, not by DAG depth.

    chunk_scheduler.commit_order sorts on DagNode depth, which only sees edges someone
    created with add_dependency. That covers exec wiring (load_chunks mirrors inputs:chunk
    into add_dependency) but NOT a param port: drag-to-connect authors an @Node.param@
    expression, whose live link is a NoticeConnection rather than a DAG edge, so two
    chunks joined only by a param wire both sit at depth 0 and commit in arbitrary order.

    The stage has both kinds of wire, and is already the authority the rest of the wiring
    code trusts (see exec_dependencies), so order from it instead.

    A cycle can't be topologically ordered and must not drop anyone: the first chunk still
    waiting is emitted and the sort continues, so the result always contains every input
    chunk exactly once.

    Args:
        chunks (list[core.ChunkBase]): The chunks to order
        stage (Usd.Stage | None): The stage holding their chunk prims; None falls back to
            the depth-based ordering

    Returns:
        list[core.ChunkBase]: The chunks, every chunk preceded by everything it reads from
    """
    ordered = list(chunks)
    if stage is None:
        return chunk_scheduler.commit_order(ordered)

    by_path = {get_chunk_prim_path(c): c for c in ordered}
    deps: dict[int, set[int]] = {}
    for c in ordered:
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
        if not (prim and prim.IsValid()):
            deps[id(c)] = set()
            continue
        # Only chunks in this batch can be waited on -- a connection to something outside it
        # would otherwise never be satisfied and stall every dependant into the cycle path.
        deps[id(c)] = {
            id(src) for path in _connected_source_paths(prim)
            if (src := by_path.get(path)) is not None and src is not c
        }

    result: list[core.ChunkBase] = []
    done: set[int] = set()
    waiting = ordered
    while waiting:
        ready = [c for c in waiting if deps[id(c)] <= done]
        if not ready:
            ready = [waiting[0]]  # cycle -- break it at the first chunk still waiting
        result.extend(ready)
        done.update(id(c) for c in ready)
        ready_ids = {id(c) for c in ready}
        waiting = [c for c in waiting if id(c) not in ready_ids]
    return result


def nested_cook_keys(chunks: Iterable[core.ChunkBase], stage: Usd.Stage) -> dict[int, tuple]:
    """A sort key per chunk (by id) that orders a nested network upstream-first.

    The key is the chunk's ancestor chain, outermost first, each level standing for how far
    down its own siblings' wiring that ancestor sits (its longest path from a sibling with no
    input, read from the stage's wires as in commit_order_from_stage). Siblings wired to
    nothing tie, and a tie goes to the shorter chain, so a chunk still comes before what is
    nested deeper beside it -- the outermost-first order load_chunks always cooked in.

    What a plain depth sort missed is two groups wired one after the other: the downstream
    group's inner chunks are shallower than the upstream group's deepest ones, so they cooked
    first, against a stage without what the upstream group makes, and nothing cooked them
    again.

    A wire from inside another group counts against the sibling holding its source. A cycle
    is broken rather than dropped: whatever is left takes the level after the last.

    Args:
        chunks: The chunks to key; ancestors outside this set end the chain.
        stage: The stage holding their chunk prims.

    Returns:
        id(chunk) -> key tuple.
    """
    chunks = list(chunks)
    families: dict[int, list[core.ChunkBase]] = {}
    for c in chunks:
        families.setdefault(id(c.get_context()), []).append(c)

    level: dict[int, int] = {}
    for family in families.values():
        by_path = {get_chunk_prim_path(c): c for c in family}
        inputs: dict[int, set[int]] = {}
        outputs: dict[int, list[core.ChunkBase]] = {id(c): [] for c in family}
        for c in family:
            prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
            found: set[int] = set()
            if prim and prim.IsValid():
                for source in _connected_source_paths(prim):
                    path = Sdf.Path(source)
                    while path not in (Sdf.Path.absoluteRootPath, Sdf.Path.emptyPath):
                        if (sibling := by_path.get(str(path))) is not None:
                            if sibling is not c and id(sibling) not in found:
                                found.add(id(sibling))
                                outputs[id(sibling)].append(c)
                            break
                        path = path.GetParentPath()
            inputs[id(c)] = found
        waiting = {id(c): len(inputs[id(c)]) for c in family}
        ready = [c for c in family if not waiting[id(c)]]
        for c in ready:
            level[id(c)] = 0
        while ready:
            c = ready.pop()
            for down in outputs[id(c)]:
                level[id(down)] = max(level.get(id(down), 0), level[id(c)] + 1)
                waiting[id(down)] -= 1
                if not waiting[id(down)]:
                    ready.append(down)
        last = max((level[id(c)] for c in family if id(c) in level), default=-1)
        for c in family:
            if waiting[id(c)]:
                level[id(c)] = last + 1

    keys: dict[int, tuple] = {}
    for c in chunks:
        chain = []
        node = c
        while node is not None and id(node) in level:
            chain.append(level[id(node)])
            node = node.get_context()
        keys[id(c)] = tuple(reversed(chain))
    return keys


def exec_upstream(chunk: core.ChunkBase, seen: set | None = None) -> set:
    """`chunk` and everything upstream of it, following exec wires only.

    Not view_cook_list, which follows param wires and lofts as well and answers "what has
    to cook for this view". This answers "which chunks make up this branch": suppressing a
    branch has to suppress the whole of it, not merely the chunk plugged into the slot --
    the rest would carry on authoring into the composed stage.

    Guarded against revisiting: a diamond (two branches sharing a source) would otherwise
    walk the shared part once per path, and a cycle would not terminate at all.

    Args:
        chunk: The chunk to start from.
        seen: Chunks already visited; filled in and returned.

    Returns:
        The chunks in the branch, `chunk` included.
    """
    seen = set() if seen is None else seen
    if any(c is chunk for c in seen):
        return seen
    seen.add(chunk)
    for dep in exec_dependencies(chunk):
        exec_upstream(dep, seen)
    return seen


def exec_downstream(
    start: core.ChunkBase,
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
) -> set[core.ChunkBase]:
    """Every chunk start's exec output reaches, following exec wires forward.

    Args:
        start: The chunk to start from.
        stage: The stage.
        scene: The scene.

    Returns:
        The downstream chunks. start itself is excluded, even when a cycle leads back to it.
    """
    consumers = _exec_consumers(stage)
    prim_to_chunk = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
    start_path = get_chunk_prim_path(start)
    seen = {start_path}
    frontier = [start_path]
    downstream: set = set()
    while frontier:
        for path in consumers.get(frontier.pop(), ()):
            if path in seen:
                continue
            seen.add(path)
            frontier.append(path)
            if (c := prim_to_chunk.get(path)) is not None:
                downstream.add(c)
    return downstream


def _live_prim(chunk: core.ChunkBase) -> Usd.Prim | None:
    """Chunk's prim on the live session's stage, if it has one."""
    stage = chunk.get_scene_stage()
    if not stage:
        return None
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    return prim if prim.IsValid() else None


def _chunks_at(paths: Iterable[str]) -> list[core.ChunkBase]:
    """The live chunks at paths, in order; a path that is not a chunk is skipped.

    Resolved by prim path, not chunk name: names are scene-wide and can be uniquified or
    renamed, so a name lookup can bind to the wrong chunk (same rationale as load_chunks'
    dependency restore).
    """
    paths = list(paths)
    if not paths:
        return []
    # Only the chunks that can be at one of paths get their prim path worked out. A chunk's
    # prim is named after it, so its name is the path's last element -- except a context
    # with a prim_path of its own, which goes anywhere and so is always a candidate. Working
    # out every chunk's path on each call was most of what a load spent here: this is asked
    # once per notice by listeners that hear every chunk.
    names = {path.rsplit("/", 1)[-1] for path in paths}
    by_path = {
        get_chunk_prim_path(c): c
        for c in chunk_registry.ChunkRegistry().scene.get_chunks()
        if c.name in names or c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
    }
    return [c for path in paths if (c := by_path.get(path)) is not None]


def exec_dependencies(chunk: core.ChunkBase) -> list:
    """The chunks feeding this one's *exec* slots, read from the live stage.

    The public way for a chunk module to ask "what is immediately upstream of me on the
    exec chain". chunk.get_dependencies() is not that question: the Python DAG also
    mirrors param wires, which mean a value flows rather than that one chunk precedes
    another.

    The stage is the authority here, not the DAG: the node graph's drag-to-connect authors
    the USD connection directly and never calls add_dependency(). _author_prim relies on
    this, or export would chain chunks in scene.get_chunks() order instead of their real
    exec order. Every slot is read -- a merge's branches past the first are real inputs,
    and reconcile_dag_dependencies would otherwise remove edges it cannot see.

    Args:
        chunk (core.ChunkBase): The chunk to read the exec inputs of

    Returns:
        list: The upstream chunks, one per connected exec slot
    """
    prim = _live_prim(chunk)
    if prim is None:
        return []
    return _chunks_at(str(conn.GetPrimPath()) for conn in exec_input_connections(prim))


def _wired_dependencies(chunk: core.ChunkBase) -> list:
    """Every chunk this one reads from on the live stage -- exec slots *and* param wires.

    exec_dependencies answers the narrower question (which chunks feed my exec slots) and
    stays that way: it is what authors inputs:chunk, and a param wire must never be written
    out as an exec edge.

    This is the wider one, for the Python DAG. A param wire already propagates change --
    _on_ref_changed executes the destination param's own sender, which dirties its chunk
    through the ownership chain -- but it carries no dependency edge, so two chunks joined
    only that way both sit at depth 0: no INPUT_CHANGED between them, arbitrary commit
    order (hence commit_order_from_stage), and no guarantee the downstream one solves after
    the upstream one when a single propagation dirties both.

    Source of truth is _connected_source_paths, the same reader commit_order_from_stage
    orders by, so the two cannot drift -- and its carve-outs (a self-reference, and an
    ancestor target, which is a loft into a group's interface rather than a sibling
    dependency) apply here unchanged.
    """
    prim = _live_prim(chunk)
    if prim is None:
        return []
    # And the edge the layer stack implies (implied_sources): a department's first chunk
    # depends on the weaker department's chain, so a change there reaches it like a wire.
    return _chunks_at(_through_placeholders(
        prim.GetStage(), [*_connected_source_paths(prim), *implied_sources(prim)],
        lambda path: bool(_chunks_at([path]))
    ))


def _would_cycle(chunk: core.ChunkBase, dep: core.ChunkBase) -> bool:
    """Whether making chunk depend on dep would close a loop.

    Adding the edge means dep must solve before chunk, so it cycles exactly when dep is
    already downstream of chunk.

    Worth the check rather than letting topo_order raise: a param wire cycle currently
    settles-and-warns (see _on_ref_changed, and _resolve_expression_string, both of which
    run during widget paint where a raise is far worse than a stale value), and CONTEXT.md
    records that tolerance as deliberate. Skipping the edge keeps cyclic wiring behaving
    exactly as it does today while acyclic wiring -- the overwhelming majority -- gains the
    ordering it was missing.
    """
    return dep.get_dag_node() in chunk_scheduler.downstream_closure(chunk.get_dag_node())


def reconcile_dag_dependencies(chunk: core.ChunkBase) -> bool:
    """Make chunk's Python DAG dependencies match its live inputs:chunk connections.

    The stage is the authority for chunk-level exec wiring: the node-graph UI's
    drag-to-connect authors inputs:chunk directly and never calls add_dependency(), so
    without this the Python DAG holds no edge at all for an interactively built graph and
    INPUT_CHANGED never fires between two chunks the user visibly wired together.

    Adding the edge here also dirties the dependent, so an edge change propagates downstream
    exactly like a param change does.

    Only chunk-level dependencies are touched. Param-level dependencies (added via the
    c_param.add_dependency(a_param) API) have no inputs:chunk counterpart and are left alone.

    Args:
        chunk (core.ChunkBase): The chunk whose inputs:chunk connections changed

    Returns:
        bool: True if any dependency was added or removed
    """
    live = _wired_dependencies(chunk)
    current = [dep for _, dep in chunk.get_dependencies() if isinstance(dep, core.ChunkBase)]

    live_ids = {id(dep) for dep in live}
    current_ids = {id(dep) for dep in current}

    changed = False
    for dep in current:
        if id(dep) not in live_ids:
            changed = chunk.remove_dependency(dep) or changed
    for dep in live:
        if id(dep) in current_ids:
            continue
        if _would_cycle(chunk, dep):
            logger.debug(
                "Skipping dependency %s -> %s: it would close a cycle, leaving the wire to "
                "settle as it does today", dep.name, chunk.name,
            )
            continue
        chunk.add_dependency(dep)
        changed = True
    return changed


_chunk_clipboard: list[str] = []


def copy_selected_chunks() -> None:
    """Store prim paths of selected non-view chunks for later paste."""
    global _chunk_clipboard
    registry = chunk_registry.ChunkRegistry()
    if not (scene := registry.scene):
        return
    _chunk_clipboard = [
        get_chunk_prim_path(c)
        for c in scene.get_selected_chunks()
        if not is_terminal_chunk(c)
    ]


def paste_chunks() -> None:
    """Create duplicates of clipboard chunks, copying their input attribute values."""
    if not _chunk_clipboard:
        return
    registry = chunk_registry.ChunkRegistry()
    scene = registry.scene
    if not scene:
        return
    stage = scene.get_stage()
    if not stage:
        return

    prim_to_chunk = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
    for prim_path in _chunk_clipboard:
        src_chunk = prim_to_chunk.get(prim_path)
        if src_chunk is None:
            continue
        cfg = src_chunk.get_config()
        if not cfg or not cfg.module:
            continue
        src_prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
        if not src_prim.IsValid():
            continue
        new_chunks = _chunk_mod.create_chunks(cfg.module)
        if not new_chunks:
            continue
        new_chunk = new_chunks[0]
        new_prim_path = get_chunk_prim_path(new_chunk)
        new_prim = stage.GetPrimAtPath(Sdf.Path(new_prim_path))
        if not new_prim.IsValid():
            continue
        for attr in src_prim.GetAttributes():
            name = attr.GetName()
            if not name.startswith(_INPUTS_PREFIX) or name == _EXEC_INPUT:
                continue
            dst_attr = new_prim.GetAttribute(name)
            # dst_attr, when it exists, was just authored by create_chunks() -> _define_prim,
            # via the chunk's own current Edit Target -- update it there, not wherever
            # ambient happens to point. A fresh chunk only has attrs for values off their
            # default, so every value the user changed is missing here: skipping those
            # pasted a copy that had none of them. Made where a new bookkeeping attr goes.
            home = chunk_utils.get_attr_home_layer(dst_attr) if dst_attr.IsValid() else None
            with Usd.EditContext(stage, home or chunk_utils.get_bookkeeping_layer(stage, new_chunk)):
                if not dst_attr.IsValid():
                    dst_attr = new_prim.CreateAttribute(name, attr.GetTypeName(), custom=False)
                val = attr.Get()
                if val is not None:
                    dst_attr.Set(val)
                if attr.HasAuthoredConnections():
                    dst_attr.SetConnections(attr.GetConnections())
                # A wire into a number keeps its text beside the value, not in it.
                if text := chunk_utils.read_expression_text(attr):
                    chunk_utils.store_expression_text(dst_attr, text)
        _load_wire_inputs(new_prim, new_chunk)


def _under_list_group(param: core.ChunkCommon, chunk: core.ChunkBase) -> bool:
    """Whether a ChunkListGroupBase sits between param and its chunk on the owner chain."""
    owner = param.get_owner()
    while owner is not None and owner is not chunk:
        if isinstance(owner, core.ChunkListGroupBase):
            return True
        owner = owner.get_owner()
    return False


def is_transient_param(param) -> bool:
    """Whether param is session state that is never recorded (ParamFlags.TRANSIENT).

    A vector is transient when all of its components are: its own flags are group flags.
    """
    if isinstance(param, core.ChunkVectorBase):
        parts = param.get_my_params()
        return bool(parts) and all(is_transient_param(p) for p in parts)
    return isinstance(param, core.ChunkParamBase) and param.has_flag(ChunkFlags.ParamFlags.TRANSIENT)


def _iter_chunk_params(
    obj: core.ChunkCommon, prefix: str = ""
) -> Generator[tuple[str, core.ChunkParamBase], None, None]:
    for param in obj.get_my_params():
        if isinstance(param, core.ChunkBase):
            continue  # a nested chunk's params are its own
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkParamBase):
            yield path, param
        elif isinstance(param, (core.ChunkListGroupBase, core.ChunkVectorBase)):
            pass  # list groups handled by _iter_list_group_arrays; vectors by _iter_chunk_vectors
        else:
            yield from _iter_chunk_params(param, path)


def _param_object_path(obj: core.ChunkCommon, target, prefix: tuple = ()) -> list[str] | None:
    """The names from obj down to target, or None when target is not in obj's own param tree.

    target is a param, vector or group. Walks get_my_params(), the logical tree -- a param's
    owner chain runs through layout containers that are not part of its path.
    """
    for child in obj.get_my_params():
        path = prefix + (child.name,)
        if child is target:
            return list(path)
        if isinstance(child, core.ChunkBase):
            continue  # a nested chunk's params are its own
        if isinstance(child, core.ChunkGroupBase) and not isinstance(child, core.ChunkListGroupBase):
            if (found := _param_object_path(child, target, path)) is not None:
                return found
    return None


def _iter_chunk_vectors(
    obj: core.ChunkCommon, prefix: str = ""
) -> Generator[tuple[str, core.ChunkVectorBase], None, None]:
    """Yield (path, vector) for every recorded ChunkVectorBase under obj. TRANSIENT ones are left out."""
    for param in obj.get_my_params():
        if isinstance(param, core.ChunkBase):
            continue  # a nested chunk's params are its own
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkVectorBase):
            if not is_transient_param(param):
                yield path, param
        elif not isinstance(param, (core.ChunkParamBase, core.ChunkListGroupBase)):
            yield from _iter_chunk_vectors(param, path)


def _iter_list_rows(obj: core.ChunkCommon) -> Generator[tuple[str, core.ProxyParam], None, None]:
    """Yield (path, row) for every list-group row, which _iter_chunk_params skips."""
    for lg_path, list_group in _iter_list_group_arrays(obj):
        for i, row_group in enumerate(list_group.proxy_params):
            for row in row_group.get_my_params():
                yield f"{lg_path}.{i}.{row.name}", row


def _iter_list_group_arrays(
    obj: core.ChunkCommon, prefix: str = ""
) -> Generator[tuple[str, core.ChunkListGroupBase], None, None]:
    """Yield (path, list_group) for every ChunkListGroupBase in the hierarchy."""
    for param in obj.get_my_params():
        if isinstance(param, core.ChunkBase):
            continue  # a nested chunk's params are its own
        path = f"{prefix}.{param.name}" if prefix else param.name
        if isinstance(param, core.ChunkListGroupBase):
            yield path, param
        elif not isinstance(param, core.ChunkParamBase):
            yield from _iter_list_group_arrays(param, path)


def initialize(stage: Usd.Stage, scene: chunk_scene.SceneSender) -> None:
    """Start the layer manager and stage sync for a stage and scene, and follow stage replacement.

    Args:
        stage: The stage.
        scene: The scene.
    """
    ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, scene)
    scene.add_stage_replaced_listener(ChunkStageSync().reload_for_stage)


def _live_context_at(scene: chunk_scene.SceneSender, path: str) -> core.ChunkBase | None:
    """The live container whose prim is at path, or None.

    Searches get_contexts, not get_chunks: the outermost container is a node in nothing and
    get_chunks leaves it out, but a prim reloaded in place under it still has to find it.
    """
    for context in scene.get_contexts():
        if getattr(context, "prim_path", None) == path:
            return context
    return None


def _find_prim_layer(prim: Usd.Prim) -> Sdf.Layer | None:
    """Return the non-root, non-anonymous layer that authors this prim."""
    root_id = prim.GetStage().GetRootLayer().identifier
    for spec in prim.GetPrimStack():
        layer = spec.layer
        if not layer.anonymous and layer.identifier != root_id:
            return layer
    return None


def _iter_scoped_prims(
    stage: Usd.Stage, resynced: set[Sdf.Path], info_changed: set[Sdf.Path]
):
    """The prims a change round could have made loadable, once each.

    Each resynced path contributes its whole subtree, since a structural change can bring
    in prims anywhere below it -- a chunk arriving in a sublayer resyncs the *parent* of the
    new prim, not the prim. Each info-changed path contributes only the prim it names.

    Paths may be property paths, and may name a prim that has since gone (a resync reports
    a deletion the same way it reports an arrival); both resolve to the owning prim or to
    nothing. Overlapping subtrees are normal, so prims are de-duplicated.
    """
    seen: set[str] = set()

    def _emit(prim):
        key = str(prim.GetPath())
        if key in seen:
            return False
        seen.add(key)
        return True

    for path in resynced:
        if path == Sdf.Path.absoluteRootPath:
            for prim in stage.Traverse():
                if _emit(prim):
                    yield prim
            continue
        prim = stage.GetPrimAtPath(path.GetPrimPath())
        if not (prim and prim.IsValid()):
            continue
        for descendant in Usd.PrimRange(prim):
            if _emit(descendant):
                yield descendant

    for path in info_changed:
        prim = stage.GetPrimAtPath(path.GetPrimPath())
        if prim and prim.IsValid() and _emit(prim):
            yield prim


def _unseen_by_active_views(
    stage: Usd.Stage, scene: chunk_scene.SceneSender
) -> Callable[[core.ChunkBase], bool]:
    """A test for the chunks a load can leave uncooked: those no active view reaches.

    What each outermost context's active view cooks, worked out the way recook_for_view
    walks it -- the view's cook_list, and for every group on it, whatever drives that
    group's output, recursively. The recook the graph runs straight after a load mutes
    everything outside that, so a load that cooks it too does work nobody sees. What is
    skipped stays stale, and recook_for_view cooks it when its view is activated.

    Only for a host that opts in (SceneSender.load_cooks_active_views_only), and only in a
    context that has an active view or an Output: one with neither cooks as before, since
    nothing says yet what matters. Views and Outputs themselves are never skipped.

    Args:
        stage: The stage.
        scene: The scene.

    Returns:
        A test, True for a chunk the load need not cook.
    """
    if not scene.load_cooks_active_views_only:
        return lambda chunk: False

    contexts = [c for c in scene.get_contexts() if getattr(c, "prim_path", None)]
    paths = [c.prim_path for c in contexts]
    reached: set[int] = set()
    gated: list[str] = []

    def walk(view: core.ChunkBase, synthetic: bool) -> None:
        cook_list = view_cook_list(view, stage, scene)
        if synthetic:
            cook_list.append(view)
        for c in cook_list:
            if id(c) in reached:
                continue
            reached.add(id(c))
            if is_group_chunk(c):
                inner, inner_synthetic = _group_output_driver(stage, scene, c)
                if inner is not None:
                    walk(inner, inner_synthetic)

    for context in contexts:
        path = context.prim_path
        if any(path.startswith(p + "/") for p in paths):
            continue  # a group -- walked from the outermost context that holds it
        prim = stage.GetPrimAtPath(Sdf.Path(path))
        attr = prim.GetAttribute("chunk:activeView") if prim and prim.IsValid() else None
        slot = attr.Get() if attr and attr.IsValid() else None
        view = find_view_chunk_for_slot(slot, stage, scene, context) if slot else None
        # No active view: the context cooks to its Outputs, as recook_for_view does.
        ends = [view] if view is not None else graph_outputs(stage, scene, context)
        if not ends:
            continue
        gated.append(path + "/")
        for end in ends:
            walk(end, False)

    def unseen(chunk: core.ChunkBase) -> bool:
        if id(chunk) in reached or is_terminal_chunk(chunk):
            return False
        return get_chunk_prim_path(chunk).startswith(tuple(gated)) if gated else False

    return unseen


def _clear_cooked_groups(chunks: list[core.ChunkBase]) -> None:
    """Clear the stale mark of each group whose inside a load has just cooked.

    A group with no execute() of its own stays stale through a solve -- that mark is what
    tells recook_for_view to force its inside -- and only recook_for_view clears it. A load
    cooks through the solve instead, so every such group came out of the load still stale,
    and the first recook forced its whole inside again for nothing.

    Only when everything inside that can cook came out clean: a chunk that could not run
    (blocked, no layer yet) is left stale by its own dispatch, and the group stays stale
    with it. Views are markers that never cook, and a disabled chunk does not cook either.

    Args:
        chunks: Every chunk in the scene, groups and their insides together. A chunkgroup
            is recreated as a context before the load cooks, so it is not among the chunks
            the load itself created.
    """
    disabled = ChunkFlags.State.DISABLED
    for group in chunks:
        if not is_group_chunk(group) or group.can_cook() or not chunk_cook.is_cook_stale(group):
            continue
        prefix = get_chunk_prim_path(group) + "/"
        if all(
            not chunk_cook.is_cook_stale(c)
            for c in chunks
            if get_chunk_prim_path(c).startswith(prefix)
            and c.can_cook() and not is_terminal_chunk(c) and not c.has_state(disabled)
        ):
            chunk_cook.clear_cook_stale(group)


def _settle_cooked_params(chunks: list[core.ChunkBase]) -> None:
    """Leave each chunk the load cooked with nothing pending from the load itself.

    Restoring a saved value dirties its param and records why, and the load cooks with
    solves held back -- so nothing marked those params clean again. The first edit after
    opening then solved them along with it, and a chunk reported its own restored params
    as changed: one that is never skipped. Rotating a car cooked the material, the lights
    and the camera below it, and the renderer re-synced them all, once.

    Only a chunk that did cook: it read every restored value, so those changes are spent.
    One that could not (blocked, out of view, no layer yet) stays as it was, and so does
    whatever is nested in it as a chunk of its own, which is settled as its own entry.

    Args:
        chunks: Every chunk in the scene, as for _clear_cooked_groups.
    """
    for chunk in chunks:
        if not chunk.can_cook() or chunk_cook.is_cook_stale(chunk):
            continue
        stack: list[core.ChunkCommon] = [chunk]
        while stack:
            obj = stack.pop()
            node = obj.get_dag_node()
            node._dirty = False
            node._changes.clear()
            node._index = 0
            if isinstance(obj, core.ChunkGroupBase):
                stack.extend(
                    p for p in obj.get_my_params()
                    if not isinstance(p, core.ChunkBase) and hasattr(p, "get_dag_node")
                )


def _restore_disabled_state(prim: Usd.Prim, chunk: core.ChunkBase) -> None:
    """Carry a prim's authored deactivation back onto the chunk it belongs to.

    Disabling a chunk authors active=false on its prim, so the file records the state
    correctly -- but a freshly created chunk starts enabled, and nothing on the load path
    reads it back. The chunk returned switched ON, quietly re-running work the scene was
    saved with switched off.

    Authored, not composed: a chunk inside a disabled group composes inactive without
    anything being authored on it, and it is not itself disabled.
    """
    if prim.HasAuthoredActive() and prim.GetMetadata("active") is False:
        chunk.set_state(ChunkFlags.State.DISABLED)


def _restore_collapsed_state(prim: Usd.Prim, chunk: core.ChunkBase) -> None:
    """Carry a node's authored expansionState back onto the chunk it belongs to.

    Collapsing a node authors ui:nodegraph:node:expansionState = closed, so the file records
    it -- but a freshly built chunk starts open, and without this the graph came back fully
    expanded however it was saved.
    """
    attr = prim.GetAttribute(UsdUI.Tokens.uiNodegraphNodeExpansionState)
    if attr and attr.IsValid() and attr.Get() == UsdUI.Tokens.closed:
        chunk.set_state(ChunkFlags.State.COLLAPSED)


def load_chunks(
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
    loaded_prim_paths: set[str] | None = None,
    prims: Generator | None = None,
) -> None:
    """Turn Chunk prims into chunks.

    Args:
        stage: The stage to read.
        scene: The scene to add the chunks to.
        loaded_prim_paths: Prim paths already loaded, skipped and updated.
        prims: Narrows which prims are considered -- see ChunkStageSync._scan_for_new_chunks,
            which derives it from what USD reported changed. None considers the whole stage,
            which is what a file being opened wants.
    """
    if not stage:
        return

    with _loading_from_file():
        _load_chunks(stage, scene, loaded_prim_paths, prims)
    # Where each chunk now composes from, so a later resync can tell which ones changed. A
    # scan scoped to what just changed only made the new chunks: fingerprinting every chunk
    # on each one made building a network cost the square of its size.
    sync = ChunkStageSync()
    if sync.initialized and getattr(sync, "_stage", None) is stage:
        sync.record_sources(scene.get_chunks(), only_new=prims is not None)


def _load_chunks(
    stage: Usd.Stage,
    scene: chunk_scene.SceneSender,
    loaded_prim_paths: set[str] | None = None,
    prims: Generator | None = None,
):
    """The body of load_chunks.

    Split out only so the _loading window covers all of it, including the cook loop at the
    end -- which is where an Edit Target chunk executes and reconciles.
    """
    to_load = []
    # Top-level (direct child of the pseudo-root) Chunk-typed prims with no chunkType --
    # a structural context prim (a Tab/ChunkGroup saved via export_chunks), not a
    # module-backed chunk. Nested ChunkGroups use a separate parent-context mechanism
    # (_parent_context) not covered by this reload path yet.
    context_prims = []
    # Where a network composed in by an arc sits on the stage: the non-chunk parent of its
    # top chunk (a referenced house's /World/house/chunks). Kept as a home like /chunks, so
    # its chunks load where USD composed them rather than being re-made under a tab named
    # after the file they came from.
    network_homes: set[str] = set()
    changed_code: dict[str, dict[str, int]] = {}  # chunkType -> {saved hash: prim count}
    for prim in (stage.Traverse(_LOAD_INCLUDE_INACTIVE) if prims is None else prims):
        # The /chunks scope is structural, not a chunk: it holds the graph but is not one.
        # It is typed Scope, so it must be recognised here explicitly: with the saved
        # container unrecognised every chunk would fall through to the root-layer branch
        # and be rehomed under a fresh tab named after the file, moving prim paths on every
        # reload and breaking anything that addresses a path (a binding, a sublayer target).
        is_chunks_scope = (
            str(prim.GetPath()) == _CHUNKS_ROOT
            and prim.GetTypeName() == _CHUNKS_ROOT_TYPENAME
        )
        if prim.GetTypeName() != _CHUNK_PRIM_TYPENAME and not is_chunks_scope:
            continue
        if loaded_prim_paths is not None and str(prim.GetPath()) in loaded_prim_paths:
            continue
        chunk_type = prim.GetAttribute(_CHUNK_TYPE_ATTR).Get()
        if not chunk_type:
            if prim.GetParent().IsPseudoRoot():
                context_prims.append(prim)
            continue
        mod = chunk_registry.ChunkRegistry().get_chunk_module(str(chunk_type))
        if not mod:
            # Names the prim, not just the type: a type can back several prims, and
            # without a path there is no way to tell which node in the graph this warning
            # is even about. The prim still loads as a placeholder (resolve_graph_nodes
            # finds it by USD type alone, independent of whether a module backs it) with
            # exec ports but none of its real params -- said here so that is not a
            # surprise discovered later by clicking the node.
            logger.warning(
                "load_chunks: %s wants chunk type %r, but no module registers it "
                "(missing/renamed/not on USD_CHUNK_DIRS?) -- loading as an empty placeholder",
                prim.GetPath(), chunk_type)
            continue
        if chunk_utils.module_changed_since_cook(prim, mod):
            saved = chunk_utils.recorded_module_hash(prim)
            counts = changed_code.setdefault(str(chunk_type), {})
            counts[saved] = counts.get(saved, 0) + 1
        parent = prim.GetParent()
        if (parent and not parent.IsPseudoRoot() and str(parent.GetPath()) != _CHUNKS_ROOT
                and parent.GetTypeName() != _CHUNK_PRIM_TYPENAME
                and chunk_utils.arc_source(prim) is not None):
            network_homes.add(str(parent.GetPath()))
        to_load.append((prim, mod))

    # One line per type, not per prim: a network of a hundred of one chunk is one change.
    for chunk_type, saved_counts in sorted(changed_code.items()):
        module = chunk_registry.ChunkRegistry().get_chunk_module(chunk_type)
        logger.warning(
            "load_chunks: %d %r chunk(s) were saved with different code than is loaded now "
            "-- saved with %s, loaded %s (%s from %s); their results may not be what it "
            "would produce",
            sum(saved_counts.values()), chunk_type,
            ", ".join(h[:19] for h in sorted(saved_counts)),
            module.source_hash[:19], chunk_naming.MODULE_HASH_ATTR, module.module_file)

    if not to_load:
        return

    # Pre-register all prim paths before processing to prevent StageContentsChanged
    # callbacks (from add_layer subLayerPaths inserts) from triggering recursive loads
    # that find unregistered paths and create duplicate contexts.
    if loaded_prim_paths is not None:
        for prim, _ in to_load:
            loaded_prim_paths.add(str(prim.GetPath()))

    # Suppress premature reactive cascades for the whole load, not just each
    # _add_prim_mods call below: recreating a context (scene.add_context, right below)
    # already triggers CHUNK_LOADED -> get_edit_context() -> get_or_create_layer(),
    # whose subLayerPaths.insert() fires a global STAGE_CONTENTS_CHANGED that an Edit
    # Target chunk's globally-listening refresh handler reacts to -- reactively
    # re-executing everything downstream of it before a single chunk has even been
    # restored yet. _add_prim_mods's own old_loading_stage/finally below still runs,
    # now just re-affirming this same value and restoring back to it -- harmless.
    old_loading_stage = chunk_notice._loading_stage
    chunk_notice._loading_stage = stage
    loaded: list[list] = []  # every _add_prim_mods batch, for the post-load pass below
    try:
        # Recreate each existing named top-level context under its own saved name/path --
        # before grouping below, so its children are recognized as already having a home
        # instead of falling through as "no source layer" orphans wrapped in a brand-new
        # tab named after whatever file happens to be open right now.
        group_chunk_by_path: dict[str, core.ChunkBase] = {}
        context_paths: set[str] = set()
        # A network an arc composed in: its home routes its chunks to _EXISTING_CONTEXT, so
        # they keep the paths USD gave them. No context is minted for the home itself, as
        # for the /chunks scope.
        context_paths.update(network_homes)
        for prim in context_prims:
            prim_path = str(prim.GetPath())
            if loaded_prim_paths is not None:
                loaded_prim_paths.add(prim_path)
            if prim_path == _CHUNKS_ROOT:
                # A scope holds chunks without being a context itself. Recording the path is still what routes its children
                # to _EXISTING_CONTEXT, so they keep their saved home instead of being
                # wrapped in a tab named after the file -- we just do not mint a context
                # for the scope to be that home.
                context_paths.add(prim_path)
                continue
            ctx = scene.add_context(prim.GetName(), prim_path)
            if ctx is not None:
                group_chunk_by_path[prim_path] = ctx
                context_paths.add(prim_path)

        def _has_recreated_context_ancestor(prim) -> bool:
            p = prim.GetParent()
            while p and not p.IsPseudoRoot():
                path = str(p.GetPath())
                # A context recreated above, or one already live in the scene: a host
                # chunk (host_network) whose children are arriving now, or the enclosing
                # group of a prim being reloaded in place (localise_network).
                if path in context_paths or _live_context_at(scene, path) is not None:
                    return True
                p = p.GetParent()
            return False

        # Group prims by their source layer so each sublayer file gets its own context --
        # except ones already homed under a context just recreated above.
        #
        # A prim nested under a chunk prim in this same pass goes in that prim's group,
        # whatever layer it came from, and after it (to_load is in traversal order, so the
        # ancestor was keyed first). A network referenced onto a host chunk's prim composes
        # its children in from the referenced file, and grouping those by their own source
        # layer wrapped them in a tab named after that file instead of nesting them under
        # the host -- which had not even been loaded yet when they were.
        _EXISTING_CONTEXT = object()
        groups = {}
        key_of_path: dict[str, object] = {}
        for prim, mod in to_load:
            src = None
            key = None
            ancestor = prim.GetParent()
            while ancestor and not ancestor.IsPseudoRoot():
                if str(ancestor.GetPath()) in key_of_path:
                    key = key_of_path[str(ancestor.GetPath())]
                    break
                ancestor = ancestor.GetParent()
            if key is None:
                if _has_recreated_context_ancestor(prim):
                    key = _EXISTING_CONTEXT
                else:
                    src = _find_prim_layer(prim)
                    key = src.identifier if src else None
            key_of_path[str(prim.GetPath())] = key
            if key not in groups:
                groups[key] = (src, [])
            groups[key][1].append((prim, mod))

        def _add_prim_mods(prim_mods):
            old_loading_stage = chunk_notice._loading_stage
            chunk_notice._loading_stage = stage
            # Layers made while the chunks below are built and wired stay off the stage
            # until the wiring is done; see ChunkLayerManager.defer_placement.
            placing = ChunkLayerManager().defer_placement()
            try:
                deferred = []  # (prim, chunk)
                loaded.append(deferred)
                top_level_chunk_ids: set[int] = set()
                # Building and wiring the chunks adds every dependency edge one at a time,
                # and each edge dirtied -- and walked -- everything below it: quadratic in
                # the size of the network, and most of a large network's load. Nothing
                # below cooks, so the walk waits for the end of this block and runs once.
                with chunk_scheduler.deferred_dirty():
                    for prim, mod in prim_mods:
                        # Walk up the prim's parent chain to find the nearest loaded ChunkGroup.
                        ancestor_group = None
                        p = prim.GetParent()
                        while p and not p.IsPseudoRoot():
                            # This pass's groups first, then the live scene: a host chunk
                            # loaded earlier (or made live) whose children are arriving now.
                            g = (group_chunk_by_path.get(str(p.GetPath()))
                                 or _live_context_at(scene, str(p.GetPath())))
                            if g is not None:
                                ancestor_group = g
                                break
                            p = p.GetParent()

                        # A chunk prim carrying a reference arc with Chunk children is a host
                        # (host_network): the arc is in the file, so the children are already
                        # composed, and the chunk takes the group flags before it is added so
                        # the CONTEXT branch below nests them under it.
                        hosts = _hosts_by_arc(prim) and any(
                            child.GetTypeName() == _CHUNK_PRIM_TYPENAME
                            for child in prim.GetFilteredChildren(_LOAD_INCLUDE_INACTIVE)
                        )
                        chunks = mod.create() if mod.loaded else mod.load()
                        for c in chunks:
                            if hosts and not c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                                for flag in (ChunkFlags.GroupFlags.CONTEXT,
                                             ChunkFlags.GroupFlags.COLLAPSABLE,
                                             ChunkFlags.GroupFlags.LABELED):
                                    c.set_flag(flag)
                            # Restore the saved chunk name from its prim (prims are authored at
                            # <root>/<chunk.name>, so this is a lossless round-trip). Freshly
                            # created chunks otherwise get default names uniquified in
                            # traversal order, which need not match the prims -- two chunks of
                            # the same type could swap identities, scrambling the name-keyed
                            # wiring below into a cyclic DAG from a perfectly linear file.
                            c.name = prim.GetName()
                            if c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                                # Restore saved param values before scene.add_chunk, same as the
                                # other two branches below -- a CONTEXT chunk is usually pure
                                # structural scaffolding (a Tab/ChunkGroup) with nothing of its
                                # own worth restoring, but one that ALSO has a real backing module
                                # (e.g. AMD MaterialX Library, which uses CONTEXT purely for its
                                # collapsible/embedded-widget node styling) can carry a full set of
                                # its own inputs: attrs, which must be read back here or every
                                # param reverts to its default on reload.
                                _load_wire_inputs(prim, c)
                                _restore_disabled_state(prim, c)
                                _restore_collapsed_state(prim, c)
                                c.prim_path = str(prim.GetPath())
                                object.__setattr__(c, '_exec_prim_path', str(prim.GetPath()))
                                if ancestor_group is not None:
                                    # A chunkgroup inside another chunkgroup. scene.add_chunk
                                    # is the scene ROOT, so a nested group must not go there:
                                    # it would come back as a SIBLING of the group it was saved
                                    # in, lose the name collision with it, and be renamed,
                                    # taking its contents to a different address.
                                    #
                                    # Cook order depends on it too: a group's inner chunks cook
                                    # in prim-path depth order, so an upstream Sublayer runs
                                    # before a BindMaterial nested deeper that reads what it
                                    # brings onto the stage.
                                    #
                                    # The file holds the nesting; this is what reads it back.
                                    #
                                    # _parent_context is what _on_param_added derives a group's
                                    # prim root from; without it the group falls back to the
                                    # /chunks scope, computes the same path as the default group
                                    # and loses the collision, which is the rename itself.
                                    # _parent_context, then the SCENE container -- which is
                                    # where a nested group lives when one is made live, so a
                                    # reload rebuilds the same shape rather than a different one.
                                    #
                                    # Not ancestor_group.add_param: that applies the param naming
                                    # rules, and get_reserved_names() includes the group's OWN
                                    # name, so a group named the same as its parent came back
                                    # renamed with a "1" suffix. USD is perfectly happy with
                                    # /chunks/ChunkGroup/ChunkGroup, the file says that is where
                                    # it lives, and anything addressing it by name breaks when we
                                    # decide otherwise on load.
                                    # Enter the ancestor for the duration of the add, which is
                                    # exactly how a group nests when one is made live: add_chunk
                                    # reads _current_context and overwrites _parent_context from
                                    # it, so setting the latter directly is undone on the way in.
                                    #
                                    # Not ancestor_group.add_param either: that applies the param
                                    # naming rules, and get_reserved_names() includes the group's
                                    # OWN name, so a group named the same as its parent came back
                                    # renamed with a "1" suffix. USD is happy with
                                    # /chunks/ChunkGroup/ChunkGroup, the file says that is where it
                                    # lives, and anything addressing it by name breaks if we decide
                                    # otherwise on load.
                                    prev_ctx = scene._current_context
                                    scene._current_context = weakref.ref(ancestor_group)
                                    try:
                                        scene.add_chunk(c)
                                    finally:
                                        scene._current_context = prev_ctx
                                else:
                                    scene.add_chunk(c)
                                if c.get_config() and c.get_config().module:
                                    group_chunk_by_path[str(prim.GetPath())] = c
                            elif ancestor_group is not None:
                                # Sub-chunk of a ChunkGroup: restore saved values before
                                # add_param so PARAM_ADDED widgets see the correct values.
                                _load_wire_inputs(prim, c)
                                _restore_disabled_state(prim, c)
                                _restore_collapsed_state(prim, c)
                                # A composed-in child keeps the name its file gave it, whatever
                                # the naming rules say: get_reserved_names() includes the host's
                                # own name, so a Reference hosting a network with a Reference in
                                # it renamed that child, and the renamed chunk authored a fresh
                                # prim of its own beside the one it was loaded from.
                                ancestor_group.add_param(
                                    c, force_unique=not chunk_utils.is_composed_in(prim), notify=False
                                )
                                object.__setattr__(c, '_exec_prim_path', str(prim.GetPath()))
                                deferred.append((prim, c))
                            else:
                                # Restore saved param values before scene.add_chunk so the
                                # PARAM_ADDED widget sees the correct values on creation.
                                _load_wire_inputs(prim, c)
                                _restore_disabled_state(prim, c)
                                _restore_collapsed_state(prim, c)
                                if added := scene.add_chunk(c, notify=False):
                                    object.__setattr__(added, '_exec_prim_path', str(prim.GetPath()))
                                    deferred.append((prim, added))
                                    top_level_chunk_ids.add(id(added))

                    # Restore DAG dependencies from inputs:chunk connections so execution
                    # order is driven by the explicit graph, not just prim appearance order.
                    # Resolve connection targets by prim path -- names are scene-wide and can
                    # be uniquified/renamed, so a name lookup can bind to the wrong chunk.
                    #
                    # Containers are wire endpoints like any other node: a chunkgroup is a NODE
                    # in its parent graph, so `upstream -> group -> downstream` is two ordinary
                    # exec edges. A container never lands in `deferred` though -- it takes the
                    # CONTEXT branch above (or was recreated as a context before this loop) and
                    # is registered in group_chunk_by_path instead -- so a map built from
                    # `deferred` alone would resolve every edge with a group at either end to
                    # None and drop it, in BOTH directions.
                    #
                    # The Python DAG is what carries a change downstream, so such a gap would
                    # stop dirt at the group: on a reopened file nothing downstream of a
                    # chunkgroup would ever re-run. (Wiring a group interactively goes through
                    # reconcile_dag_dependencies, which reads the live scene and sees groups.)
                    chunk_by_path = {str(p.GetPath()): c for p, c in deferred}
                    chunk_by_path.update(group_chunk_by_path)
                    chunks_with_dependencies: set[int] = set()
                    # A container carries no chunk of its own to cook, so it stays out of
                    # `deferred` (the cook/notify passes below are leaves only) and is walked
                    # here purely for the edges on its own prim. A tab context has no exec
                    # connections at all and falls straight through the `continue` below.
                    wired = deferred + [
                        (prim, c)
                        for path, c in group_chunk_by_path.items()
                        if (prim := stage.GetPrimAtPath(Sdf.Path(path))).IsValid()
                    ]
                    for prim, chunk in wired:
                        # Every slot, in slot order -- a reloaded merge must regain ALL its branches,
                        # and regain them strongest-first so the restored DAG matches the saved file.
                        exec_conns = exec_input_connections(prim)
                        # No wire in: the layer stack may still say what comes before it.
                        sources = ([str(conn.GetPrimPath()) for conn in exec_conns]
                                   or implied_sources(prim))
                        if not sources:
                            continue
                        existing_deps = {id(d) for _, d in chunk.get_dependencies()}
                        # Through any placeholder: a wire into one must still order this
                        # chunk after the chain behind it (see _through_placeholders).
                        for source in _through_placeholders(
                            stage,
                            sources,
                            lambda path: path in chunk_by_path or bool(_chunks_at([path])),
                        ):
                            dep = chunk_by_path.get(source)
                            if dep is not None and id(dep) not in existing_deps:
                                chunk.add_dependency(dep)
                                chunks_with_dependencies.add(id(chunk))

                    _restore_lofted_params(stage, deferred)

                    # _load_wire_inputs (via _connect_param_attr) resolves @node.param@
                    # expressions to set up the live-update listener (_expression_connections),
                    # but the referenced chunk may not have been registered yet at that point
                    # in this same loop -- resolution fails and no listener gets attached, so
                    # later edits to the referenced chunk never notify this param/widget even
                    # though a fresh .value read is correct. Once every chunk in this batch
                    # is registered and every lofted value restored, retry once for anything
                    # still empty. This runs after _restore_lofted_params so a lofted param
                    # carrying an expression is covered by the same retry.
                    #
                    # This is the first of two passes. A param lofted inside a CHUNK_LOADED
                    # callback does not exist yet here -- that notice fires much further down --
                    # so an expression referring to one still resolves to nothing. The second
                    # pass runs after those notices and carries the warning; nothing is reported
                    # dangling until both have had a go.
                    _resolve_pending_expressions(deferred, warn=False)

                    # Wire the context (ChunkGroup) as DagNode owner of its terminal sub-chunks.
                    # Terminal nodes have no outputs so DagNode.should_solve() returns False by
                    # default, skipping them in the cascade. Setting the owner makes them solve.
                    chunks_that_are_deps: set[int] = set()
                    for _, c in deferred:
                        for _, dep in c.get_dependencies():
                            chunks_that_are_deps.add(id(dep))
                    for _, c in deferred:
                        if id(c) not in chunks_that_are_deps:
                            context = c.get_context()
                            if context and isinstance(context, core.ChunkBase):
                                c.get_dag_node().set_owner(context)

                # Trigger the DAG solve for root nodes (no upstream dependencies).
                # The solve cascades to dependents in topological order via add_dependency.
                # Top-level chunks always execute. Sub-chunks inside ChunkGroups only
                # execute when ChunkLayerManager owns this specific stage -- a stale
                # singleton from a different test/session must not add layers to the
                # wrong stage stack or call execute prematurely.
                lm = ChunkLayerManager()
                can_execute_sub_chunks = lm.initialized and lm.stage is stage

                # The wiring is final, so every layer made so far can go straight into the
                # graph layer it belongs in -- in the order it would have had if nothing had
                # been deferred, because a new graph layer goes on top of the ones before it
                # and the order of chunks at the same depth is kept by the re-sort below.
                # Layers the loop below leaves alone (a chunkgroup's own) were placed the
                # moment they were asked for, so before it; the loop places the rest.
                held = lm.end_deferral() if placing else []
                placing = False
                looped = {
                    id(c) for _, c in deferred
                    if lm.initialized and (id(c) in top_level_chunk_ids or can_execute_sub_chunks)
                }
                # Each graph layer's sublayers are written once for all its chunks, not once
                # per chunk: every write recomposes the layer stack. See
                # ChunkLayerManager.batched_placement.
                with lm.batched_placement() if lm.initialized else contextlib.nullcontext():
                    for c in held:
                        if id(c) not in looped:
                            lm.get_or_create_layer(c)

                    # Ensure every chunk eligible to execute below already has its own layer
                    # registered before the re-sort pass. Most chunks' layers get created as a
                    # side effect of restoring their values in _load_wire_inputs (via
                    # _connect_param_attr) -- but a chunk whose only restored value is a
                    # list-group row (ProxyParam's own __setattr__, e.g. Sublayer's
                    # imports:sublayer_path) never goes through that path, so without this its
                    # layer wouldn't exist until the execute loop below -- by which point the
                    # re-sort pass has already run and can never include it. The layer then
                    # never gets spliced into the stage even though get_or_create_layer() is
                    # called there too, and a saved Sublayer comes back empty on reload.
                    # get_or_create_layer() is idempotent, so this is a no-op for every
                    # chunk that already got one the normal way.
                    if lm.initialized:
                        for _, c in deferred:
                            if id(c) in top_level_chunk_ids or can_execute_sub_chunks:
                                lm.get_or_create_layer(c)

                # Re-sort graph sublayers so downstream chunks (higher sort_key after
                # wiring) end up with stronger layers than their upstream dependencies.
                # Layers are created early (during _load_wire_inputs, and now unconditionally
                # just above) when sort_keys are all 0, so the initial insertion order needs
                # correction here.
                if lm.initialized:
                    sort_chunk_layers(lm, (c for _, c in deferred))

                # Home every chunk's bookkeeping now, while the exec chain is final (it came
                # from the file and the dependency restore above only mirrored it) and before
                # anything cooks. Left to the reconcile after load_chunks, this same work is
                # quadratic: each chunk that moves is re-run, and by then a re-run cascades a
                # solve over the entire graph below it. Done here, cook=False, the cook loop
                # immediately below is the one and only pass over the graph.
                # protect_root_layer: the file already records which layer holds each chunk's
                # bookkeeping, so on open the file IS the answer and there is nothing to
                # re-home. Migrating here does active harm -- _migrate_chunk_bookkeeping
                # moves a spec by deleting the original, and on a freshly opened file the
                # original lives in that file's own root layer. Sdf caches layers per
                # process, so the emptied layer is what the NEXT Usd.Stage.Open of the same
                # path receives: loading a network twice in one process restored only the
                # context the second time, while the file on disk stayed perfectly correct.
                # Re-homing still happens on every later topology change, which is what the
                # reconcile is actually for.
                reconcile_edit_targets(stage, scene, cook=False, protect_root_layer=True)

                # Cook exec roots OUTERMOST-FIRST. Only roots are run here; everything with
                # a dependency cooks by cascade from one. But a chunkgroup's inner chunks
                # are roots of their OWN subgraph, and nothing connects them to the group's
                # upstream -- only the GROUP itself is wired into the outer chain -- so the
                # DAG imposes no order between them and the outer chunks whose content they
                # act on. In `deferred` order a group's inner root could therefore cook
                # first, against a stage that did not yet hold what it needs -- an inner
                # BindMaterial running before the Sublayer feeding the group has brought the
                # mesh onto the stage finds no target and authors nothing. Nesting is half of
                # the order (/chunks/Tab1/Sublayer before
                # /chunks/Tab1/AMDMaterialXLibrary/BindMaterial); the wiring between the groups
                # is the other half -- by depth alone, a group's inner chunks cooked before
                # those of a deeper group wired upstream of it. nested_cook_keys orders by
                # both, and sorted() is stable, so ties keep their existing relative order.
                #
                # A root an earlier root's solve already cooked is not run again. Everything is
                # dirty after the build, so the first solve takes in every dirty node connected
                # to it, the other roots of a shared downstream included -- a Merge of six
                # References pulls in all six, and the Scatter below cooks once, with every
                # input in place. Running the next five roots anyway re-dirtied and re-cooked
                # that whole chain five more times, each identical to the last. Only a cook in
                # this loop counts: a root that was already clean before it runs as it always
                # did. Not batched_solve: a solve does not walk from an owner back down to its
                # dirty children, so a batch let one group's queued inner roots be reached late
                # and cooked their group before them -- a car configurator's glass binding came
                # out empty. Run in turn, each root still dirties its group itself.
                unseen = _unseen_by_active_views(stage, scene)
                stale_before = {id(c) for _, c in deferred if chunk_cook.is_cook_stale(c)}
                cook_keys = nested_cook_keys((c for _, c in deferred), stage)
                for prim, added in sorted(deferred, key=lambda item: cook_keys[id(item[1])]):
                    if id(added) not in chunks_with_dependencies and not unseen(added):
                        if id(added) in stale_before and not chunk_cook.is_cook_stale(added):
                            continue
                        is_top_level = id(added) in top_level_chunk_ids
                        if is_top_level or can_execute_sub_chunks:
                            if lm.initialized:
                                lm.get_or_create_layer(added)
                            added.execute()

                # Fire CHUNK_LOADED for all loaded chunks (UI notification) and restore
                # params that were created dynamically inside execute (e.g. show_attribute).
                for prim, added in deferred:
                    chunk_notice.send_notice(
                        chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, added, data=added
                    )
                    _restore_dynamic_params(prim, added)

                # Second and last expression pass. Chunks that loft their params in the
                # CHUNK_LOADED callback above -- mtlx_shader from its nodedef, wedge from
                # its ranges -- have only just created them, so an expression pointing at
                # one could not resolve in the earlier pass and got no live-update
                # listener. A `.value` read stayed correct, which is why sweeps and exports
                # were right while the UI never updated: the value was fine and only the
                # notification was missing.
                _resolve_pending_expressions(deferred, warn=True)

                # Register every loaded chunk with the sync, as _on_param_added does for one
                # created live. When a file is opened at startup the sync is not yet
                # initialized during this load, so _on_param_added never ran for them -- and
                # a param created after the load (a Stage Expression variable, a Light's
                # attributes) found no prim to record its edits on and was saved without them.
                sync = ChunkStageSync()
                if getattr(sync, "_chunk_prim_paths", None) is not None:
                    for prim, added in deferred:
                        sync._chunk_prim_paths.track(added, str(prim.GetPath()))

                # Last, after CHUNK_LOADED and the restores above: those can cook again (Render
                # Settings recooks once its renderer groups exist), and a cook solves up the
                # ownership chain, which marks a group stale all over again.
                _clear_cooked_groups(list(scene.get_chunks()))
                _settle_cooked_params(list(scene.get_chunks()))
            finally:
                chunk_notice._loading_stage = old_loading_stage
                # A load that failed part-way still leaves its layers on the stage.
                if placing:
                    ChunkLayerManager().place_deferred_layers()

        # Chunks already homed under a context recreated above -- no new tab needed, and
        # the context chunk already exists, so just process them directly.
        if _EXISTING_CONTEXT in groups:
            _add_prim_mods(groups.pop(_EXISTING_CONTEXT)[1])

        # Root-layer chunks first -- create a context tab named after the file.
        if None in groups:
            layer = stage.GetRootLayer()
            tab_name = Path(layer.GetDisplayName() or layer.identifier).stem or "Chunks"
            scene.add_context(tab_name)
            _add_prim_mods(groups.pop(None)[1])

        # Sublayer groups -- each gets its own context tab for visual organisation
        for src_layer, prim_mods in groups.values():
            tab_name = Path(src_layer.GetDisplayName() or src_layer.identifier).stem or "Chunks"
            scene.add_context(tab_name)
            _add_prim_mods(prim_mods)
    finally:
        chunk_notice._loading_stage = old_loading_stage

    # After the flag is down, not inside: a menu_items write made while it is up sends
    # nothing at all (AttributesBase.value_changed), so a lofted menu re-synced any earlier
    # would be right in Python and still wrong in the widget showing it.
    _resync_lofted_menus(batch_chunk for batch in loaded for _, batch_chunk in batch)


def _resync_lofted_menus(chunks: Iterable[core.ChunkBase]) -> None:
    """Copy every lofted Menu's items from its inner Menu again, once a load is over.

    _restore_lofted_params wires the mirror (_mirror_menu_items) and syncs it once, but
    that is before CHUNK_LOADED -- and a Menu that fills its items there (Layer's After
    Layer list, from the stage) does so after it. During a load no attribute
    change sends a notice, so the fill went unheard and the group's dropdown came back
    holding only the saved value. One explicit pass, the same shape as the second
    _resolve_pending_expressions pass and for the same reason.
    """
    seen: set[int] = set()
    for chunk in chunks:
        context = chunk.get_context()
        if context is None or id(context) in seen:
            continue
        seen.add(id(context))
        for param in context.get_my_params():
            if (mirror := getattr(param, "_loft_menu_items", None)) is not None:
                mirror()


def flatten_without_chunks(stage):
    """Flatten the stage to a layer carrying the result but none of the recipe.

    A Chunk prim left in a delivered file is not inert: open it anywhere usd_chunks is
    installed and the network reloads and re-cooks, overriding the very geometry the
    file was exported to carry. Stripping them is what makes the output plain USD that
    anyone can open with nothing installed.

    FlattenLayerStack rather than Stage.Export, because it keeps references as
    references where Export inlines the referenced asset outright -- which stops
    mattering only while a network builds all its own geometry.

    Shallowest path first, so removing a parent Chunk takes its children with it and
    the later lookup for those children simply finds nothing.

    The stage's up-axis and units are stamped on as well (stamp_stage_metadata): flattening
    carries them only when the root layer itself authors them, which a new scene never has.
    """
    flat = UsdUtils.FlattenLayerStack(stage)
    strip_chunks(flat)
    stamp_stage_metadata(flat, stage)
    return flat


def strip_chunks(layer, only_paths=None) -> None:
    """Remove the Chunk prims from `layer`, in place, and everything that pointed at them.

    Shared by scene.export() and the Export USD chunk, so both write the same plain USD.

    Args:
        layer: The layer to clean.
        only_paths: Strip just these chunk prim paths and leave every other chunk in
            place. None strips every Chunk prim in the layer.
    """
    if only_paths is None:
        tmp = Usd.Stage.Open(layer)
        candidates = (p.GetPath() for p in tmp.TraverseAll() if p.GetTypeName() == chunk_naming.CHUNK_PRIM_TYPENAME)
    else:
        candidates = (Sdf.Path(p) for p in only_paths)
    # Shallowest path first, so removing a parent Chunk takes its children with it and
    # the later lookup for those children simply finds nothing.
    chunk_paths = sorted(candidates, key=lambda p: p.pathElementCount)
    for path in chunk_paths:
        spec = layer.GetPrimAtPath(path)
        if not spec:
            continue  # already removed as child of a shallower Chunk
        parent = spec.nameParent
        if parent:
            del parent.nameChildren[spec.name]
        else:
            del layer.pseudoRoot.nameChildren[spec.name]

    # Chunks connect the attributes they drive to their own params (a camera's focalLength
    # to Camera.inputs:...:focalLength). The values are already flattened in, so the links
    # would only point at prims this file no longer has. That includes a chunk whose prim
    # sat in another layer and so was never in `chunk_paths`: any link into the chunk
    # scope that has nothing left to land on goes too.
    chunks_root = Sdf.Path(chunk_naming.CHUNKS_ROOT)

    def is_gone(target):
        if any(target.HasPrefix(c) for c in chunk_paths):
            return True
        return target.HasPrefix(chunks_root) and not layer.GetPrimAtPath(target.GetPrimPath())

    def drop_links_to_chunks(path):
        if not path.IsPropertyPath():
            return
        prop = layer.GetPropertyAtPath(path)
        links = prop.connectionPathList if isinstance(prop, Sdf.AttributeSpec) else prop.targetPathList
        items = list(links.GetAddedOrExplicitItems())
        kept = [t for t in items if not is_gone(t)]
        if len(kept) == len(items):
            return
        if kept:
            for target in set(items) - set(kept):
                links.RemoveItemEdits(target)
        else:
            links.ClearEdits()  # no `.connect = None` left behind

    # The scope the chunks hung under is recipe as well. Nothing lives AT /chunks, so once
    # its chunks are gone an empty `def Scope "chunks" {}` is the one trace of the network
    # left in a file whose whole point is to carry none -- and it is enough to make a
    # delivered asset look like it wants usd_chunks installed. A layer that only held
    # chunk bookkeeping also leaves `over`s behind: the ones that framed it (`over
    # "chunks"` / `over "ChunkGroup"`), and the bookkeeping of chunks defined in another
    # layer -- a chunkgroup's inner chunks write theirs into the Edit Target's layer, while
    # the group itself is in the session layer. Any subtree under /chunks with no `def` in
    # it is one of those. Something else parked there is a `def`, and stays.
    root = layer.GetPrimAtPath(chunks_root)
    is_scope = root and root.typeName in (chunk_naming.CHUNKS_ROOT_TYPENAME, "")
    if is_scope:
        _prune_over_only(root)
    # After the pruning, so links into what it removed go too.
    layer.Traverse(Sdf.Path.absoluteRootPath, drop_links_to_chunks)
    if is_scope and not root.nameChildren and (root.typeName or root.specifier == Sdf.SpecifierOver):
        del layer.pseudoRoot.nameChildren[root.name]
    if only_paths is None:
        _default_to_the_only_root(layer)


def _default_to_the_only_root(layer) -> None:
    """Make a layer's one root prim its defaultPrim, when it names none that is left.

    With the recipe gone, an asset built by a network is typically one root prim -- and a
    file referenced without a prim path composes nothing unless it names a defaultPrim, so
    every delivered asset had to be patched by hand before a scene could reference it. A
    layer with several roots is left to say which it means; one naming a prim it still has
    keeps it.
    """
    roots = [spec for spec in layer.rootPrims if spec.specifier == Sdf.SpecifierDef]
    if layer.defaultPrim and layer.GetPrimAtPath(Sdf.Path.absoluteRootPath.AppendChild(layer.defaultPrim)):
        return
    if len(roots) == 1:
        layer.defaultPrim = roots[0].name
    elif layer.defaultPrim:
        layer.ClearDefaultPrim()  # it named a Chunk prim, now stripped


def _prune_over_only(spec) -> bool:
    """Delete the children of `spec` with no `def` below them; True if `spec` still has one."""
    has_def = spec.specifier == Sdf.SpecifierDef
    for child in list(spec.nameChildren):
        if _prune_over_only(child):
            has_def = True
        else:
            del spec.nameChildren[child.name]
    return has_def


def export_chunks(scene: chunk_scene.SceneSender, path: str | Path) -> None:
    """Export the current chunk graph as one clean, portable USD layer.

    Copies each chunk's already-composed, already-correctly-wired prim spec straight across
    from the live stage -- connections (inputs:chunk exec wiring, expression refs), values,
    node positions, everything, exactly as currently authored -- rather than re-deriving
    them from Python object state. The Python DAG (chunk.get_dependencies()) is empty for a
    graph wired live in the node graph, which authors the USD-level inputs:chunk connection
    directly and never calls add_dependency(), so re-deriving would chain chunks in
    iteration order instead of their real exec order.

    UsdUtils.FlattenLayerStack resolves the live composition first -- chunk-prim bookkeeping
    can be split across several layers (chunk_utils.get_bookkeeping_layer, since it follows
    Edit Target) -- giving each prim/attr its correct final composed state to copy from,
    regardless of which layer(s) it's actually scattered across.

    Only chunk prims themselves are copied -- reopening regenerates everything a chunk
    drives by re-running load_chunks' cook pass, not by replaying a content snapshot.
    Content that isn't reproduced by re-execution (an RBD Solver's bake) or that never
    went through a chunk at all does not round-trip through this.

    The stage's up-axis and units are stamped onto the file as well. Sdf.CopySpec copies
    prim specs and nothing else, and those two are not prim data: they are stage
    metadata, resolved only from a stage's own root/session layer and never composed up
    from anything underneath. Without an opinion of its own the exported file falls back
    to USD's global default: a Z-up, centimetre scene would reopen Y-up in metres, with
    every chunk-authored prim lying on its side at a hundredth of its size.

    Chunks that arrive by composition -- a sequence network sublayered into this file, or
    referenced in under a chunkgroup -- are not this file's to write out. Only the layers
    the chunk system owns are flattened (_owned_stage), so what gets copied for such a
    chunk is the sparse `over` this scene put on it, and a chunk with no local opinion at
    all is skipped rather than copied from a spec that does not exist locally. The root
    layer's foreign sublayers are carried onto the file so the sequence stays composed
    underneath those overs on reopen. This is what lets a shot tweak a sequence's network
    and save without inlining it.

    Args:
        scene: The scene to export.
        path: The file to write.
    """
    stage = scene.get_stage()
    if not stage:
        return
    dst = _export_layer(scene, Path(path))
    stamp_stage_metadata(dst, stage)
    dst.Export(str(path))
    _reload_cached_layer(path)


def _export_layer(scene: chunk_scene.SceneSender, path: Path) -> Sdf.Layer:
    """export_chunks' layer, before it is stamped and written.

    That is the scene's chunk prims, composed from the owned layers, with the root's foreign
    sublayers carried. `path` is where it will be written, which decides how asset paths are
    anchored.
    """
    stage = scene.get_stage()
    # Chunk layers hold cook output. Nearly all of it is scene content, outside the chunk
    # prims copied below, but an Override Chunk Prim cooks opinions onto another chunk's
    # prim: its cook makes them again, and saved they would outlive the chunk. Only a layer
    # with something under /chunks is left out, and not one that is also where some chunk
    # keeps its prim (an Edit Target's layer); with none, the live stage is flattened as before.
    #
    # And every other cook layer that cannot reach what is copied out below -- the chunk
    # prims, their subtrees and their ancestors: flattening is quadratic in the layers it
    # is given, and with a layer per chunk, saving a big network was mostly flattening cook
    # output only to throw it away.
    lm = chunk_registry.ChunkLayerManager()
    chunks = list(scene.get_chunks())
    homes = {chunk_utils.get_bookkeeping_layer(stage, c) for c in chunks}
    # The live stage, not one opened over `flat`: a chunk composed in from a foreign layer
    # is typed Chunk here and nowhere in `flat`, and it is exactly the one whose local
    # `over` (if any) must still be found and copied below.
    chunk_paths = sorted(
        (p.GetPath() for p in stage.TraverseAll() if p.GetTypeName() == _CHUNK_PRIM_TYPENAME),
        key=lambda p: p.pathElementCount,
    )
    chunk_roots = {p.GetPrefixes()[0] for p in chunk_paths}
    cook_layers = [
        layer.identifier for c in chunks
        if lm.initialized and (layer := lm.get_layer(c)) is not None and layer not in homes
        and (layer.GetPrimAtPath(chunk_naming.CHUNKS_ROOT) is not None
             or not _may_reach(layer, chunk_roots))
    ]
    owned = _owned_stage(stage, cook_layers)
    resolve = _asset_path_resolver(stage.GetRootLayer(), Path(path))
    try:
        flat = _flatten_owned(stage, owned, resolve)
    finally:
        _flatten_stages[:] = [s for s in _flatten_stages if s != owned]

    dst = Sdf.Layer.CreateAnonymous()
    _carry_foreign_sublayers(stage.GetRootLayer(), dst, Path(path))
    for prim_path in chunk_paths:
        if dst.GetPrimAtPath(prim_path):
            continue  # already brought in as part of an ancestor's subtree copy
        if flat.GetPrimAtPath(prim_path) is None:
            continue  # composed entirely from foreign layers; this file has nothing to say
        parent = prim_path.GetParentPath()
        if not parent.isEmpty and parent != Sdf.Path.absoluteRootPath and not dst.GetPrimAtPath(parent):
            Sdf.CreatePrimInLayer(dst, parent)
        Sdf.CopySpec(flat, prim_path, dst, prim_path)
        _carry_ancestor_specs(flat, dst, prim_path)
    return dst


def stamp_stage_metadata(dst: Sdf.Layer, stage: Usd.Stage) -> None:
    """Stamp the stage's up-axis and units onto the in-memory layer before it is written.

    Read with the same UsdGeom API the live stage answers with, so the file carries the
    opinion from its first byte rather than being reopened and patched afterwards. Written
    straight onto the layer's pseudo-root: opening a stage over `dst` to set them composed
    the whole layer, which for an exported scene is everything in it.

    Shared by every writer -- save, export_network, scene.export and the Export USD chunk --
    since a file without them falls back to USD's global default whatever the scene was.
    """
    root = dst.pseudoRoot
    root.SetInfo(UsdGeom.Tokens.upAxis, UsdGeom.GetStageUpAxis(stage))
    root.SetInfo(UsdGeom.Tokens.metersPerUnit, UsdGeom.GetStageMetersPerUnit(stage))


def _rebase_paths_in_layer(layer: Sdf.Layer, old_root: Sdf.Path, new_root: Sdf.Path) -> None:
    """Rewrite connections and relationship targets under new_root that still point into old_root.

    That is what Sdf.CopySpec to a new path leaves behind, the same way a BatchNamespaceEdit
    does (see _fixup_connection_targets).
    """

    def visit(prim_spec):
        for prop in prim_spec.properties.values():
            if isinstance(prop, Sdf.AttributeSpec):
                items = prop.connectionPathList.GetAddedOrExplicitItems()
                moved = [p.ReplacePrefix(old_root, new_root) for p in items]
                if moved != list(items):
                    prop.connectionPathList.ClearEdits()
                    prop.connectionPathList.explicitItems = moved
            elif isinstance(prop, Sdf.RelationshipSpec):
                items = prop.targetPathList.GetAddedOrExplicitItems()
                moved = [p.ReplacePrefix(old_root, new_root) for p in items]
                if moved != list(items):
                    prop.targetPathList.ClearEdits()
                    prop.targetPathList.explicitItems = moved
        for child in prim_spec.nameChildren.values():
            visit(child)

    visit(layer.GetPrimAtPath(new_root))


def export_network(scene: chunk_scene.SceneSender, group: core.ChunkBase, path: str | Path) -> str:
    """Write one group's network to its own file, the way export_chunks writes a scene's.

    The group goes at /chunks/<name> with its chunks beneath it, wiring rebased to the new
    root, and the exec input from the graph outside dropped. Built from export_chunks' own
    layer rather than a second copy of its rules, so a published network is exactly the
    group as a save would have written it: composed, sparse where it was composed in, arcs
    kept.

    Args:
        scene: The scene.
        group: The chunkgroup to export.
        path: The file to write.

    Returns:
        The group's prim path in the file -- what a host references.

    Raises:
        ValueError: There is no stage, or the group has nothing to export.
    """
    stage = scene.get_stage()
    if not stage:
        raise ValueError("no stage to export from")
    src_root = Sdf.Path(get_chunk_prim_path(group))
    dst_root = Sdf.Path(_CHUNKS_ROOT).AppendChild(src_root.name)
    whole = _export_layer(scene, Path(path))
    if whole.GetPrimAtPath(src_root) is None:
        raise ValueError(f"{src_root} has nothing to export")
    out = Sdf.Layer.CreateAnonymous()
    scope = Sdf.CreatePrimInLayer(out, Sdf.Path(_CHUNKS_ROOT))
    scope.specifier = Sdf.SpecifierDef
    scope.typeName = _CHUNKS_ROOT_TYPENAME
    Sdf.CopySpec(whole, src_root, out, dst_root)
    _rebase_paths_in_layer(out, src_root, dst_root)
    group_spec = out.GetPrimAtPath(dst_root)
    exec_in = group_spec.properties.get(chunk_naming.EXEC_INPUT)
    if exec_in is not None:
        exec_in.connectionPathList.ClearEdits()
    stamp_stage_metadata(out, stage)
    out.Export(str(path))
    _reload_cached_layer(path)
    return str(dst_root)


def _owned_layers(stage: Usd.Stage) -> list[Sdf.Layer]:
    """Every layer in the stack the chunk system may write, strong to weak."""
    return [
        layer for layer in stage.GetLayerStack(includeSessionLayers=True)
        if not chunk_utils.is_foreign_layer(layer, stage)
    ]


def publish_network(group: core.ChunkBase, path: str | Path) -> None:
    """Write group's network to path and make group the host of that file.

    The inverse of localise_network. After the write, the group prim references the file
    and its children's local specs are dropped from every owned layer, so the same chunks
    now compose in from the file and anything the shot does to them from here on is an
    override. The chunk objects are untouched: their prims still exist, composed rather than
    authored. The group prim's own bookkeeping (position, active view) stays local -- it is
    this graph's node.

    Args:
        group: The chunkgroup to publish.
        path: The file to write.
    """
    scene = _active_scene()
    stage = group.get_scene_stage()
    if not (scene and stage):
        return
    group_path = Sdf.Path(get_chunk_prim_path(group))
    target = export_network(scene, group, path)
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        with ChunkStageSync()._chunk_prim_edit_context(group):
            prim = stage.GetPrimAtPath(group_path)
            prim.GetReferences().SetReferences([Sdf.Reference(str(path), Sdf.Path(target))])
        for layer in _owned_layers(stage):
            spec = layer.GetPrimAtPath(group_path)
            if spec is None:
                continue
            for name in list(spec.nameChildren.keys()):
                del spec.nameChildren[name]
    finally:
        chunk_notice._usd_write_active = prev
    invalidate_edit_layer_memo()


def is_localisable(chunk: core.ChunkBase) -> bool:
    """Whether localise_network has anything to do for this chunk.

    That is a network host, or a chunkgroup that references its network from a file
    (publish_network's result).

    Args:
        chunk: The chunk.

    Returns:
        True if it can be localised.
    """
    if is_network_host(chunk):
        return True
    if not is_chunkgroup(chunk):
        return False
    stage = chunk.get_scene_stage()
    if not stage:
        return False
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    return bool(prim and prim.IsValid() and _hosts_by_arc(prim))


def localise_network(host: core.ChunkBase) -> core.ChunkBase | None:
    """Turn a hosted network into a plain chunkgroup of this file.

    The inverse of publish_network, and what "Allow Editing of Contents" means here. The
    composed subtree under the host prim -- the file's chunks with the shot's overrides on
    top -- is flattened and authored as local defs in the host's bookkeeping layer, the arc
    is dropped, and the host prim becomes a chunkgroup. The live objects are then rebuilt
    from the prims, since the host was a Reference (or Sublayer, Payload) chunk and its
    replacement is a chunkgroup; the same load path a reopened file takes, so the result is
    what saving and reopening would have given.

    Args:
        host: The hosting chunk.

    Returns:
        The new chunkgroup, or None if there was nothing to localise.
    """
    if not is_localisable(host):
        return None
    scene = _active_scene()
    stage = host.get_scene_stage()
    if not (scene and stage):
        return None
    sync = ChunkStageSync()
    host_path = Sdf.Path(get_chunk_prim_path(host))
    already_a_group = is_chunkgroup(host)
    composed = Usd.Stage.OpenMasked(
        stage.GetRootLayer(), stage.GetSessionLayer(), Usd.StagePopulationMask([str(host_path)])
    )
    flat = composed.Flatten(False)
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        with sync._chunk_prim_edit_context(host):
            layer = stage.GetEditTarget().GetLayer()
        Sdf.CreatePrimInLayer(layer, host_path)
        Sdf.CopySpec(flat, host_path, layer, host_path)
        spec = layer.GetPrimAtPath(host_path)
        spec.referenceList.ClearEdits()
        spec.payloadList.ClearEdits()
        spec.typeName = _CHUNK_PRIM_TYPENAME
        spec.specifier = Sdf.SpecifierDef
        for name in list(spec.properties.keys()):
            if name.startswith(_INPUTS_PREFIX) and name != chunk_naming.EXEC_INPUT:
                del spec.properties[name]
            elif name == chunk_naming.MODULE_HASH_ATTR:
                del spec.properties[name]
        chunk_type = spec.properties.get(_CHUNK_TYPE_ATTR) or Sdf.AttributeSpec(
            spec, _CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, Sdf.VariabilityUniform)
        chunk_type.default = "chunkgroup"
        # The arc and the host's own inputs: in every other owned layer too, or a stronger
        # one keeps the prim a Reference after this one says chunkgroup.
        for other in _owned_layers(stage):
            other_spec = other.GetPrimAtPath(host_path)
            if other_spec is None or other is layer:
                continue
            other_spec.referenceList.ClearEdits()
            other_spec.payloadList.ClearEdits()
            for name in list(other_spec.properties.keys()):
                if name.startswith(_INPUTS_PREFIX) and name != chunk_naming.EXEC_INPUT:
                    del other_spec.properties[name]
                elif name in (_CHUNK_TYPE_ATTR, chunk_naming.MODULE_HASH_ATTR):
                    del other_spec.properties[name]
    finally:
        chunk_notice._usd_write_active = prev
    invalidate_edit_layer_memo()
    if already_a_group:
        # A published chunkgroup taking its network back: the objects are the right kind
        # already, and their prims are now authored where they were composed.
        return host

    # Rebuild the objects from the prims: the host object is a Reference chunk, and what
    # the prim now describes is a chunkgroup. Deepest first, so a nested group goes before
    # its parent; under _reloading, so nothing is unauthored on the way out.
    inside = [
        c for c in scene.get_chunks()
        if get_chunk_prim_path(c).startswith(str(host_path) + "/")
    ]
    inside.sort(key=lambda c: get_chunk_prim_path(c).count("/"), reverse=True)
    was_reloading = sync._reloading
    sync._reloading = True
    try:
        for c in inside:
            c.request_delete()
        host.request_delete()
    finally:
        sync._reloading = was_reloading
    prim = stage.GetPrimAtPath(host_path)
    sync._loaded_prim_paths.discard(str(host_path))
    load_chunks(stage, scene, sync._loaded_prim_paths,
                prims=iter([prim] + list(Usd.PrimRange(prim, _LOAD_INCLUDE_INACTIVE))[1:]))
    return next((c for c in scene.get_chunks() if get_chunk_prim_path(c) == str(host_path)), None)


def _may_reach(layer: Sdf.Layer, roots: set[Sdf.Path], seen: set[str] | None = None) -> bool:
    """Whether layer, or a layer under it, may hold a spec at or under one of `roots`.

    A layer holds a spec for every ancestor of each spec it has, so one with no root prim
    among `roots` has nothing at or below them. Says True when it cannot tell: a sublayer
    not open to look at.
    """
    seen = set() if seen is None else seen
    if layer.identifier in seen:
        return False
    seen.add(layer.identifier)
    if any(spec.path in roots for spec in layer.rootPrims):
        return True
    for sub_path in layer.subLayerPaths:
        sub = Sdf.Layer.FindRelativeToLayer(layer, sub_path)
        if sub is None or _may_reach(sub, roots, seen):
            return True
    return False


def _flatten_owned(stage: Usd.Stage, owned: Usd.Stage, resolve) -> Sdf.Layer:
    """UsdUtils.FlattenLayerStack of the owned stage, rescuing a stack with broken specs."""
    try:
        return UsdUtils.FlattenLayerStack(owned, resolve)
    except Tf.ErrorException:
        # One property spec with no usable type makes FlattenLayerStack refuse the whole
        # layer stack, and flattening is how a scene is saved -- so a single stub costs the
        # user everything they have done since the last save. Drop the stubs (they hold
        # nothing: no value, no type, no connection) and try once more.
        #
        # Only on the failure path. What creates a stub is not known -- reproducing it from
        # a reported session did not work -- so this is a rescue, not a fix, and a rescue
        # has no business walking every layer on every ordinary save. If nothing was found,
        # or flattening still fails, the original error is the honest answer.
        if not chunk_layers.drop_broken_property_specs(
            stage.GetLayerStack(includeSessionLayers=True), logger
        ):
            raise
        return UsdUtils.FlattenLayerStack(owned, resolve)


#: Stages _owned_stage opened over the scene's root layer, while they are being flattened.
#: The stage sync knows its stage by root layer, so it has to be told these are not it:
#: muting layers on one sends a change notice, and the sync took the masked stage for the
#: scene's, made it its own and scanned it for chunks.
_flatten_stages: list[Usd.Stage] = []


def _owned_stage(stage: Usd.Stage, also_mute: Iterable[str] = ()) -> Usd.Stage:
    """The stage as seen through only the layers the chunk system owns -- for flattening.

    UsdUtils.FlattenLayerStack takes every layer in the root layer stack, foreign ones
    (chunk_utils.is_foreign_layer) included, and a sublayered sequence file flattened into
    a shot's save is the sequence inlined: every one of its chunks a full `def` in the
    shot, its subLayers arc gone, the shot no longer following the sequence. The layers to
    flatten are the owned ones, and a layer stack minus some layers is a stage with those
    layers muted -- muting is per stage, so a second stage over the same root and session
    layers, with the foreign ones muted, leaves the live one exactly as it was.

    Opened with an empty population mask: flattening reads the layer stack, not composed
    prims, so nothing needs composing, and a scene's worth of content would otherwise be
    composed a second time on every save. With no foreign layer present the live stage
    itself comes back, and the save is what it always was.

    also_mute names more layers to leave out the same way, such as the chunks' cook layers.
    """
    foreign = [
        layer.identifier for layer in stage.GetLayerStack(includeSessionLayers=True)
        if chunk_utils.is_foreign_layer(layer, stage)
    ]
    foreign.extend(also_mute)
    if not foreign:
        return stage
    owned = Usd.Stage.OpenMasked(
        stage.GetRootLayer(), stage.GetSessionLayer(), Usd.StagePopulationMask()
    )
    # Before muting, which is what sends the notice -- see _flatten_stages.
    _flatten_stages.append(owned)
    owned.MuteAndUnmuteLayers(foreign, [])
    return owned


def _saving_beside(root: Sdf.Layer, path: Path) -> bool:
    """Whether `path` lands in the root layer's own directory.

    If so, a relative asset path authored in the root layer means the same file from the
    exported one.
    """
    if not root.realPath:
        return False
    return Path(path).resolve().parent == Path(root.realPath).parent.resolve()


def _asset_path_resolver(root: Sdf.Layer, path: Path):
    """How FlattenLayerStack rewrites asset paths for this export.

    Its default makes every asset path absolute, which is right for an opinion taken
    from an anonymous layer (nothing to anchor a relative path to) and for a file written
    somewhere else, and wrong for the ordinary save-in-place: a chunkgroup referencing
    `./seq.usda` came back as an absolute path into the user's home directory, and the
    shot stopped being relocatable with its sequence. Saving beside the root layer keeps
    that layer's asset paths exactly as the user wrote them.
    """
    anonymous_root = not root.realPath
    if not anonymous_root and not _saving_beside(root, path):
        return UsdUtils.FlattenLayerStackResolveAssetPath
    # An in-memory scene (a fresh session, a test) has no directory of its own: the
    # exported file's directory is the one its asset paths are made relative to.
    root_dir = (Path(path) if anonymous_root else Path(root.realPath)).parent.resolve()

    def resolve(layer, asset_path):
        if layer == root and not anonymous_root:
            return asset_path
        resolved = UsdUtils.FlattenLayerStackResolveAssetPath(layer, asset_path)
        # An arc authored in an anonymous layer (a hosted network, a published group)
        # had to be absolute -- nothing anchors a relative path there -- but the file it
        # names sits beside this one, and a shot that moves with its sequence should
        # still find it.
        try:
            relative = Path(resolved).resolve().relative_to(root_dir)
        except (ValueError, OSError):
            return resolved
        return "./" + relative.as_posix()

    return resolve


def _carry_foreign_sublayers(root: Sdf.Layer, dst: Sdf.Layer, path: Path) -> None:
    """Put the root layer's foreign sublayers onto the exported layer, offsets included.

    Owned sublayers are flattened into the export and need no arc; a foreign one is the
    reason the export is sparse for the chunks it contributes, and without the arc those
    overs would compose over nothing. Entries stay as written when the file is saved
    beside the root layer (the ordinary save-in-place), and are made absolute when it is
    written elsewhere, since a relative asset path is anchored to the layer holding it.
    """
    same_dir = _saving_beside(root, path)
    for index, entry in enumerate(root.subLayerPaths):
        # An owned sublayer is anonymous and listed by its identifier, so Find sees it. A
        # foreign entry is kept whether or not its file could be opened: a sublayer that
        # is missing today is still the user's arc, not something a save should drop.
        layer = Sdf.Layer.Find(entry)
        if layer is not None and layer.anonymous:
            continue
        dst.subLayerPaths.append(entry if same_dir else root.ComputeAbsolutePath(entry))
        dst.subLayerOffsets[len(dst.subLayerPaths) - 1] = root.subLayerOffsets[index]


def _reload_cached_layer(path: str | Path) -> None:
    """Bring Sdf's in-memory copy of a just-written file back in line with the disk.

    Sdf keeps one layer per path for the whole process, and Export writes the file without
    touching it. So saving over a file already open in this session -- most often the very
    scene being worked on -- would leave the old contents cached: reopening it, dropping it
    back into the graph, or anything else that goes through FindOrOpen would get the scene
    as it was BEFORE the save, and a later Save() of that layer would write the old contents
    back over the new file.

    When the file is the live stage's own root layer, the reload is a composition no-op for
    the chunks (the export is their composed state), but it still reports every chunk prim
    as resynced -- suppressed, so it does not send the whole graph through a rescan.
    """
    layer = Sdf.Layer.Find(str(path))
    if layer is None:
        return
    prev = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        layer.Reload(force=True)
    finally:
        chunk_notice._usd_write_active = prev


def _carry_ancestor_specs(flat: Sdf.Layer, dst: Sdf.Layer, prim_path: Sdf.Path) -> None:
    """Give an exported chunk's ancestors the typename AND specifier they have on the stage.

    Only chunk prims are copied across; their ancestors are conjured by
    Sdf.CreatePrimInLayer, which makes a bare `over` with no typename. The /chunks scope is
    typed Scope, not Chunk, so it is not in the copy set and would arrive as only that over.

    An over is not IsDefined(), and Usd.Stage.Traverse() prunes at an undefined prim --
    taking the whole chunk subtree with it. So an exported file loaded back as an empty
    scene: no scope, no chunks, nothing. Carry the specifier as well, and an ancestor
    that is a real defined prim on the stage stays one in the export.
    """
    parent = prim_path.GetParentPath()
    while not parent.isEmpty and parent != Sdf.Path.absoluteRootPath:
        spec = dst.GetPrimAtPath(parent)
        source = flat.GetPrimAtPath(parent)
        if spec is not None and source is not None:
            if not spec.typeName and source.typeName:
                spec.typeName = source.typeName
            if source.specifier == Sdf.SpecifierDef:
                spec.specifier = Sdf.SpecifierDef
        parent = parent.GetParentPath()


def save_scene(scene: chunk_scene.SceneSender, chunkgroup_path: str | Path) -> None:
    """Save the scene: the same file export_chunks writes, under the name the Save Scene menu uses.

    Only chunk prims are written. Reopening regenerates everything a chunk is responsible
    for by re-running it, so content that is not reproduced by re-execution needs an
    explicit re-run after reload -- a bake, the same as Houdini's File Cache.

    Args:
        scene: The scene to save.
        chunkgroup_path: Where the chunk graph is written -- the file to reopen.
    """
    export_chunks(scene, Path(chunkgroup_path))


def rename_context_prim_path(context: core.ChunkBase, new_prim_path: str | None) -> None:
    """Move all chunk prims in context to a new root using Sdf.CopySpec, then remove the old specs."""
    sync = ChunkStageSync()
    if not sync.initialized or not sync._stage:
        context.prim_path = new_prim_path
        return

    old_root = context.prim_path if context.prim_path else _CHUNKS_ROOT
    new_root = new_prim_path if new_prim_path else _CHUNKS_ROOT

    if old_root == new_root:
        context.prim_path = new_prim_path
        return

    stage = sync._stage
    chunk_objs = [c for c in context.get_my_params() if isinstance(c, core.ChunkBase)]

    # Use prim.GetPrimStack() to find the exact layers authoring each chunk prim,
    # rather than scanning GetLayerStack() which may not find anonymous sublayers.
    layer_chunks: dict = {}  # layer → [chunk_obj, ...]
    for chunk_obj in chunk_objs:
        prim = stage.GetPrimAtPath(Sdf.Path(f"{old_root}/{chunk_obj.name}"))
        if not (prim and prim.IsValid()):
            continue
        for spec in prim.GetPrimStack():
            layer_chunks.setdefault(spec.layer, []).append(chunk_obj)

    if not layer_chunks:
        context.prim_path = new_prim_path
        return

    old_write = chunk_notice._usd_write_active
    chunk_notice._usd_write_active = True
    try:
        for layer, to_copy in layer_chunks.items():
            with Sdf.ChangeBlock():
                # Ensure the new root container prim exists in this layer
                new_root_sdf = Sdf.Path(new_root)
                if not layer.GetPrimAtPath(new_root_sdf):
                    root_spec = Sdf.CreatePrimInLayer(layer, new_root_sdf)
                    root_spec.typeName = _CHUNK_PRIM_TYPENAME
                    root_spec.specifier = Sdf.SpecifierDef

                for chunk_obj in to_copy:
                    old_path = Sdf.Path(f"{old_root}/{chunk_obj.name}")
                    new_path = Sdf.Path(f"{new_root}/{chunk_obj.name}")
                    if layer.GetPrimAtPath(old_path):
                        Sdf.CopySpec(layer, old_path, layer, new_path)

                # Fix inputs:chunk connection paths in the copied specs
                for chunk_obj in to_copy:
                    new_path = Sdf.Path(f"{new_root}/{chunk_obj.name}")
                    new_spec = layer.GetPrimAtPath(new_path)
                    if not new_spec or _EXEC_INPUT not in new_spec.attributes:
                        continue
                    exec_attr_spec = new_spec.attributes[_EXEC_INPUT]
                    updated = [
                        Sdf.Path(new_root + str(conn)[len(old_root):])
                        if str(conn).startswith(old_root + "/") else conn
                        for conn in exec_attr_spec.connectionPathList.explicitItems
                    ]
                    exec_attr_spec.connectionPathList.explicitItems = updated

                # Remove old chunk specs
                for chunk_obj in to_copy:
                    old_path = Sdf.Path(f"{old_root}/{chunk_obj.name}")
                    old_spec = layer.GetPrimAtPath(old_path)
                    if not old_spec:
                        continue
                    parent_path = old_path.GetParentPath()
                    parent_spec = layer.GetPrimAtPath(parent_path) or layer.pseudoRoot
                    if old_path.name in parent_spec.nameChildren:
                        del parent_spec.nameChildren[old_path.name]

                # Remove the old root container prim if now empty
                old_root_spec = layer.GetPrimAtPath(Sdf.Path(old_root))
                if old_root_spec and not list(old_root_spec.nameChildren):
                    old_root_path = Sdf.Path(old_root)
                    old_root_parent = layer.GetPrimAtPath(old_root_path.GetParentPath()) or layer.pseudoRoot
                    if old_root_path.name in old_root_parent.nameChildren:
                        del old_root_parent.nameChildren[old_root_path.name]
    finally:
        chunk_notice._usd_write_active = old_write

    # Update path tracking in the sync singleton
    for chunk_obj in chunk_objs:
        old_tracked = f"{old_root}/{chunk_obj.name}"
        new_tracked = f"{new_root}/{chunk_obj.name}"
        sync._loaded_prim_paths.discard(old_tracked)
        sync._loaded_prim_paths.add(new_tracked)
        if sync._chunk_prim_paths.get(id(chunk_obj)) == old_tracked:
            sync._chunk_prim_paths.track(chunk_obj, new_tracked)

    context.prim_path = new_prim_path

    # Re-wire param attribute listeners to the new prim attributes
    for chunk_obj in chunk_objs:
        prim = stage.GetPrimAtPath(Sdf.Path(f"{new_root}/{chunk_obj.name}"))
        if prim and prim.IsValid():
            _load_wire_inputs(prim, chunk_obj)


def _lofted_attr_name(context_prim: Usd.Prim, context, prefix: str, owner, param_name: str) -> str:
    """The interface name a lofted param takes on the context prim.

    Qualified by the chunk it came from -- Rim_base_color, not base_color -- because a
    group's interface is flat while the params feeding it are not. Six materials in one
    group all offer base_color, and unqualified they landed on a single attribute: the
    second loft silently overwrote the first's value, and only one of them was actually
    driven by it.

    Always qualified, not only on a clash. Qualifying on demand would leave the first
    loft bare and the second prefixed, so the interface would depend on the order someone
    happened to loft in.

    An underscore rather than a colon. A colon is USD's namespace separator and already
    means something here -- inputs:Base:base_color is the param base_color inside the page
    group Base -- and _load_wire_inputs turns that back into a "Base.base_color" lookup.
    Rim:base_color would be read as a group named Rim.

    Suffixed if that is still taken, so lofting the same param twice cannot clobber what
    is already published. "Taken" means the attribute is DECLARED on the prim, not that it
    holds a value -- so a name stays reserved for as long as its declaration survives, even
    after the param that published it is gone. That is deliberate: an interface name is what
    the CLI addresses and what saved files record, so handing it to a different param
    because the first one was unlofted would silently repoint whatever still refers to it.

    The qualifier is a snapshot of the owning chunk's name at loft time, and renaming that
    chunk afterwards does not update it -- a param lofted from Rim stays Rim_base_color even
    if Rim is later renamed Key. Stability wins here for the same reason: the group's
    published interface must not change under anything addressing it by name.
    """
    base = param_name
    if owner is not None and owner is not context and getattr(owner, "name", None):
        base = f"{owner.name}_{param_name}"

    candidate, index = base, 1
    while context_prim.GetAttribute(f"{prefix}{candidate}"):
        candidate = f"{base}{index}"
        index += 1
    return candidate


def _lofted_label(owner, context, label: str) -> str:
    """The display label a lofted param takes on the context's params panel.

    Prefixed with the owning chunk's name at the moment of the loft, e.g. "Transform1: Prim
    Path" -- otherwise three same-named params lofted from different chunks (three
    Reference nodes' Prim Path, say) show up as three identical "Prim Path" rows in the
    panel with no way to tell which one drives which node; the qualifier that disambiguates
    them lives on the USD attribute name (_lofted_attr_name), but nothing surfaced it to the
    widget the user actually looks at.

    A one-time default, same as _lofted_attr_name's own qualifier -- NOT kept live against a
    later rename of the source chunk. The label is an ordinary editable field once set: a
    user is free to rename a lofted param to whatever they want, same as any other param,
    and that choice has to stick -- rewriting it out from under them on every rename would
    make "rename the lofted param" not really mean anything. It also keeps the label's
    meaning aligned with the attribute name it fronts, which is frozen for the same reason
    (the CLI and saved expressions address it by name; see loft_param_to_context).

    Not qualified when owner is context (lofting a group's own param up to its parent) or
    owner is None -- the group's plain label already names itself.
    """
    if owner is not None and owner is not context and getattr(owner, "name", None):
        return f"{owner.name}: {label}"
    return label


def _mirror_menu_items(inner_param: core.ChunkParamBase, context_param: core.ChunkParamBase) -> None:
    """Keep a lofted Menu offering what the inner Menu offers.

    A loft is a clone taken once, and menu_items came across with it: whatever the inner
    menu listed at that instant, frozen. That is wrong for any Menu whose items follow
    the stage (Set Variant's are read off the prim at Prim Path, and rebuild as the graph
    cooks), and wrong for every lofted Menu on a reopen: _restore_lofted_params clones
    before the inner chunk has read anything, so the group's dropdown came back holding
    the saved value and nothing else, with no way to pick anything different.

    Items only. The value already flows both ways through the PARAM_CHANGED bridges in
    the callers, and the label is deliberately the group's own (see _lofted_label).

    A bare NoticeConnection rather than add_callback, so it has no owner: an owned
    callback is dispatched inside the owner's edit context, and skipped outright while
    that layer is muted (NoticeConnection.handle_callback). A chunk under a context with
    no active view is exactly that -- dormant, its layer muted -- for the whole of a
    reopen, which is when its menus first fill. Nothing here authors, so there is no
    layer to be in. Both the closure and the connection are pinned on the context param,
    whose lifetime the mirror follows: the connection keeps its function by weakref, and
    disconnects when it is itself collected.
    """
    if not isinstance(inner_param, _chunk_mod.Menu) or not isinstance(context_param, _chunk_mod.Menu):
        return

    def _mirror(_notice=None, _sender=None):
        items = list(inner_param.menu_items or [])
        if list(context_param.menu_items or []) != items:
            context_param.menu_items = items

    context_param._loft_menu_items = _mirror  # strong-ref pin
    # Opening the group's menu asks the inner chunk's module for the items (menu_items hook),
    # and _mirror copies them across: the group's module knows nothing of them.
    context_param._loft_inner_menu = weakref.ref(inner_param)
    context_param._loft_menu_items_conn = NoticeConnection(
        method=_mirror,
        alias=ChunkNoticeAlias.ATTRIBUTE_CHANGED,
        sender=inner_param.get_attribute_sender("menu_items"),
    )
    _mirror()


def _forward_button_click(inner_param: core.ChunkParamBase, context_param: core.ChunkParamBase) -> None:
    """Make the group's button click the inner button."""
    if ChunkFlags.ParamType.BUTTON not in context_param.evaluate_type():
        return

    def _click_inner(_owner=None, _notice=None, _sender=None):
        inner_param.click()

    context_param._loft_click = _click_inner  # strong-ref pin: callbacks are held weakly
    context_param.add_callback(
        _click_inner,
        sender=context_param.get_attribute_sender("value"),
        send_on_create=False,
        private=True,
    )


def _wire_loft_sync(param: core.ChunkParamBase, context_param: core.ChunkParamBase) -> None:
    """Keep a lofted param and the inner param it fronts in step, both ways.

    Shared by loft_param_to_context and _restore_lofted_params, so a reopened group syncs
    exactly like a freshly lofted one. The restore once wired only the downward half: after
    a reopen, an edit to the inner param (its own widget, a gizmo, a script) cooked but
    never reached the group's interface attribute -- the one the file saves and the next
    load reads back -- so the edit was silently undone on the following save and open.
    """
    # Forward value changes from the context interface param back through the inner param
    # so that the inner chunk's callbacks fire. _on_objects_changed is suppressed during
    # the USD write, so we bridge the gap with a direct Python callback.
    #
    # The closure must be kept alive for as long as context_param lives -- NoticeConnection
    # stores plain functions as weakrefs, so without a strong reference the callback is
    # garbage-collected immediately after this function returns. Pinning it onto
    # context_param ensures the lifetime is tied to the param itself.
    # Shared by both directions below. Two params that write to each other are a loop, and
    # the value check alone does not close it: a float round-trips through the USD attr as
    # float32, so the value coming back is not always equal to the one sent, and a drag
    # could ping-pong instead of settling. The flag makes each user-facing write produce
    # exactly one write on the other side, whichever end it started from.
    syncing: list = []

    def _forward_to_inner(_owner, _notice, _sender=None):
        if syncing:
            return
        syncing.append(True)
        try:
            val = context_param.value
            if val is not None and param.value != val:
                param.value = val
        finally:
            syncing.clear()

    def _forward_to_context(_owner, _notice, _sender=None):
        """The mirror: what anything else writes to the inner param shows on the interface.

        Without it the loft only ever pushed downward, so a group published a param that
        showed a stale value the moment the inner chunk was driven by anything else -- a
        gizmo drag being the way it was noticed (chunk_gizmo writes `param.value`, exactly
        like a user would), but a script or another chunk writing that param left the
        group's interface just as wrong, and a saved file recorded the stale number.
        """
        if syncing or getattr(context_param, "_deleting", False):
            return
        syncing.append(True)
        try:
            val = param.value
            if val is not None and context_param.value != val:
                context_param.value = val
        finally:
            syncing.clear()

    context_param._loft_fwd = _forward_to_inner  # strong-ref pin
    context_param.add_callback(
        _forward_to_inner,
        notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_CHANGED,
        send_on_create=False,
        private=True,
    )
    # Pinned on the INNER param, whose lifetime this direction follows -- same reason the
    # forward closure is pinned on the context param.
    param._loft_back = _forward_to_context
    param.add_callback(
        _forward_to_context,
        notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_CHANGED,
        send_on_create=False,
        private=True,
    )


def loft_param_to_context(param: core.ChunkParamBase, context: core.ChunkBase) -> None:
    """Loft a param up to the context prim's public interface.

    Creates ``inputs:{owner}_{param.name}`` on the context prim, connects the inner
    storage attribute to it, and re-binds the Python param to track the context
    attribute -- so reads and writes flow through the context prim interface.

    The name is settled here, once, and then belongs to the group. It is deliberately not
    re-derived from the owning chunk later: the interface is what the CLI addresses by
    name, what expressions reference and what saved files record, so renaming an inner
    chunk must not silently change the group's published interface.

    Args:
        param: The param to loft.
        context: The group to publish it on.
    """
    sync = ChunkStageSync()
    if not sync.initialized or not sync._stage:
        return

    stage = sync._stage

    if not param._param_attr_path:
        # A param still at its default has no USD attr yet -- create it so there's something to
        # promote (Loft up right after creating a chunk is the common case).
        owner = param.get_chunk()
        if owner is not None:
            sync._sync_param_input(owner, param, force=True)
    if not param._param_attr_path:
        logger.warning("loft_param_to_context: param %r has no USD attribute bound", param.name)
        return

    inner_attr = stage.GetAttributeAtPath(Sdf.Path(param._param_attr_path))
    if not (inner_attr and inner_attr.IsValid()):
        logger.warning("loft_param_to_context: attribute not found at %s", param._param_attr_path)
        return

    # OUTPUT-flagged params loft to outputs:name so a group can expose computed results, not
    # just inputs (data-out ports); everything else is a data-in port (inputs:name).
    prefix = "outputs:" if param.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
    context_prim_path = Sdf.Path(context.prim_path if context.prim_path else _CHUNKS_ROOT)

    context_prim = stage.GetPrimAtPath(context_prim_path)
    if not (context_prim and context_prim.IsValid()):
        context_prim = stage.DefinePrim(context_prim_path)

    if not (context_prim and context_prim.IsValid()):
        logger.warning("loft_param_to_context: could not get/create context prim at %s", context.prim_path)
        return

    # Resolved against the prim, so the name is unique among what is already published.
    owner = param.get_chunk()
    lofted_name = _lofted_attr_name(context_prim, context, prefix, owner, param.name)
    attr_name = f"{prefix}{lofted_name}"
    context_attr_path = context_prim_path.AppendProperty(attr_name)

    context_attr = context_prim.CreateAttribute(attr_name, inner_attr.GetTypeName(), custom=True)
    if (val := param.value) is not None:
        context_attr.Set(val)
    inner_attr.SetConnections([context_attr_path])

    # Clone before any state changes so the context widget is fully enabled.
    # _param_attr_path and _driven_attr_paths are NO_COPY so the clone starts unbound;
    # we immediately wire it to the context attr.
    context_param = param.clone()
    # Named to match the attribute it fronts, before add_param -- which uniquifies names
    # itself, and would otherwise hand back base_color1 for a param bound to
    # inputs:Rim_base_color. That mismatch is what made the second loft a phantom control:
    # a distinct param in the panel, wired to somebody else's attribute.
    context_param.name = lofted_name
    # clone() carried param's own label over verbatim (e.g. "Prim Path"), which the widget
    # prefers over .name (get_label()) -- without this, every lofted param that shares its
    # inner param's label showed identically in the panel regardless of the qualifier baked
    # into .name, and there was no way to tell three lofted Prim Paths apart at a glance.
    # A one-time default the same as the name above: renaming this param afterwards (an
    # ordinary edit to its label, same as any param) is not overwritten by a later rename
    # of the source chunk.
    # No stored customData yet at this point -- a fresh loft always starts at the
    # generated default, same as the attribute name above.
    context_param.label = _lofted_label(owner, context, param.get_label())
    context_param._lofted = True
    context.add_param(context_param)
    # clone() carries over the source param's cached _chunk_parent (the inner chunk), and
    # add_param doesn't invalidate it -- so get_chunk() would keep returning the inner chunk
    # instead of the group this param now lives on. That broke lofting a group's own param up
    # again (the "loft to parent group" case): the Loft-up target resolver couldn't tell the
    # param sits on a group, so it re-lofted onto the same group. Clear it so it re-resolves.
    context_param._chunk_parent = None
    context_param._connect_param_attr(context_attr)

    _wire_loft_sync(param, context_param)
    _mirror_menu_items(param, context_param)
    _forward_button_click(param, context_param)

    # A rename (chunk_qt_widgets' "Rename..." on a lofted param, or any other code setting
    # .label) has to survive a save/reload -- without this it only lived in the Python
    # object for the rest of the session and _restore_lofted_params rebuilt the generated
    # default on the next load, silently discarding it. Scoped to the "label" attribute
    # specifically (its own AttributeSender), not the coarse PARAM_CHANGED the value-forward
    # callbacks above use, so this doesn't fire (and re-touch USD) on every value edit too.
    def _persist_lofted_label(_owner, _notice, _sender=None):
        if not context_param._param_attr_path:
            return
        target = stage.GetAttributeAtPath(Sdf.Path(context_param._param_attr_path))
        if not (target and target.IsValid()):
            return
        home = chunk_utils.get_attr_home_layer(target) or stage.GetSessionLayer()
        with Usd.EditContext(stage, home):
            chunk_utils.store_lofted_label(target, context_param.label)

    context_param._loft_label_persist = _persist_lofted_label  # strong-ref pin
    context_param.add_callback(
        _persist_lofted_label,
        sender=context_param.get_attribute_sender("label"),
        send_on_create=False,
        private=True,
    )


def _load_wire_inputs(prim: Usd.Prim, chunk: core.ChunkBase) -> None:
    # A runtime chunk has no module to have built its params, so they have to come from the
    # prim before there is anything for the loop below to restore values into. Here rather
    # than at one of load_chunks' three call sites: this is the one place every path to a
    # loaded chunk goes through, and getting only some of them back is how a chunk wired
    # into an exec chain came back without its params at all.
    _rebuild_runtime_params(prim, chunk)
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith(_INPUTS_PREFIX):
            continue
        param_path = name[len(_INPUTS_PREFIX):].replace(":", ".")

        # List group arrays: inputs:list_name:param_name = StringArray([...])
        # Detect by: parent resolves to a ChunkListGroupBase and value is a Vt array.
        parts = param_path.split(".")
        if len(parts) >= 2:
            parent_path = ".".join(parts[:-1])
            template_name = parts[-1]
            parent = chunk.get_param(parent_path) if parent_path else None
            if isinstance(parent, core.ChunkListGroupBase):
                val = attr.Get()
                if val is not None and hasattr(val, "__iter__") and not isinstance(val, str):
                    values = list(val)
                    if parent.size < len(values):
                        parent.size = len(values)
                    for i, v in enumerate(values):
                        # Setting size/values above can reactively rebuild the list
                        # (e.g. export_usd's stage-driven Layers to Export refresh
                        # fires mid-restore and resizes to the loading stage's layer
                        # count) -- re-check the bound every iteration or a shrink
                        # leaves this loop indexing past the end.
                        if i >= len(parent.get_my_params()):
                            break
                        if px := parent.get_indexed_param(template_name, i):
                            px.value = v
                    _restore_row_expressions(attr, parent, template_name)
                continue

        if param := chunk.get_param(param_path):
            param._connect_param_attr(attr)


def _resolve_pending_expressions(deferred, warn: bool) -> None:
    """Re-assign every @node.param@ expression that still has no live-update listener.

    `_load_wire_inputs` (via `_connect_param_attr`) resolves expressions as each chunk
    loads, but the chunk being referred to may not be registered yet at that point, and a
    param lofted in a CHUNK_LOADED callback does not exist until much later still. Either
    way resolution fails silently and no listener is attached -- a fresh `.value` read is
    correct, so the result looks right and only stops updating when the referenced param
    changes.

    Re-assigning `raw_value` re-runs resolution; anything that already has a listener is
    skipped, so this is idempotent and cheap to run more than once.

    warn only on the last pass. Reporting from an earlier one would flag every expression
    that is merely waiting for a chunk further down the file.
    """
    for _, c in deferred:
        for path, param in [*_iter_chunk_params(c), *_iter_list_rows(c)]:
            raw = getattr(param, "raw_value", None)
            if (
                isinstance(raw, str)
                and chunk_utils.has_expression(raw)
                and not getattr(param, "_expression_connections", None)
            ):
                param.value = raw
                if warn and not getattr(param, "_expression_connections", None):
                    logger.warning(
                        "load_chunks: expression %r on %s.%s refers to a param "
                        "that does not exist; it will not update live",
                        raw, c.name, path,
                    )


def _rebuild_runtime_params(prim: Usd.Prim, chunk: core.ChunkBase) -> None:
    """Give a saved runtime chunk its params back, from the prim's own attributes.

    A chunk built in the interpreter has no module, so there is no main() to rebuild its
    params -- the file is the only description of them that exists. It is a complete one for
    what a param IS: name, type and value are all authored as inputs:*, which is why the
    chunk could be saved in full and still come back empty.

    Only for the generic `runtime` type. Every other chunk gets its params from its module,
    and inventing extra ones here from stray attributes would fight it.

    What the attributes cannot say is which WIDGET a param was: param_from_attr maps a USD
    type to a param class, so a Slider of floats returns as whatever float maps to, without
    its range, flags or label. Values, names and wiring are exact; the rest is a file
    format's worth of work that a chunk with a module never needs.
    """
    if not (prim and prim.IsValid()):
        return
    type_attr = prim.GetAttribute(_CHUNK_TYPE_ATTR)
    if not (type_attr and type_attr.Get() == _RUNTIME_CHUNK_TYPE):
        return
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith(_INPUTS_PREFIX):
            continue
        # inputs:chunk is the EXEC input, not a param. Rebuilding it as one would give the
        # reloaded chunk a stray "chunk" dropdown, and -- worse -- turn its exec wire into a
        # param connection, drawn by the graph as an expression edge.
        if is_exec_input_name(name):
            continue
        *group_names, param_name = name[len(_INPUTS_PREFIX):].split(":")
        if chunk.get_param(".".join(group_names + [param_name])) is not None:
            continue
        try:
            param = _chunk_mod.param_from_attr(attr, param_name)
        except Exception as e:  # noqa: BLE001 - one odd attribute must not stop the load
            logger.debug("runtime chunk %s: skipping %s (%s)", prim.GetPath(), name, e)
            continue
        if param is None:
            continue
        param.set_flag(ChunkFlags.ParamFlags.LABELED)
        # inputs:group:param -- the attribute's namespaces are the groups it sat in, so
        # they are rebuilt from it. Only the groups that hold a param come back: an empty
        # group writes no attribute, so the file has no record of it.
        _runtime_group(chunk, group_names).add_param(param)


def _runtime_group(chunk: core.ChunkBase, names: list[str]) -> core.ChunkGroupBase:
    """The group at names under chunk, creating any that do not exist yet."""
    container = chunk
    for name in names:
        existing = next(
            (
                p for p in container.get_my_params()
                if p.name == name
                and isinstance(p, core.ChunkGroupBase)
                and not isinstance(p, (core.ChunkBase, core.ChunkListGroupBase))
            ),
            None,
        )
        container = existing if existing is not None else container.add_param(_chunk_mod.Group(name))
    return container


def _restore_dynamic_params(prim: Usd.Prim, chunk: core.ChunkBase) -> None:
    """Restore saved values for params created dynamically inside a CHUNK_LOADED callback.

    Params added during execute (e.g. show_attribute) aren't present when the first
    _load_wire_inputs runs before execute, so their chunk-prim storage attrs are skipped.
    After execute fires we scan those attrs again: if a param now exists but is still
    wired to a real-prim attr (not the chunk-prim storage), we set its value from the
    saved attr, and author that value onto the real attr as well -- the write-through the
    param's own listener would normally do is damped while a load is running, and this
    runs inside one.  The resulting PARAM_CHANGED triggers _sync_param_input /
    _sync_vector_input which wires the param to chunk storage and authors the driven USD
    connection.
    """
    prim_path = str(prim.GetPath())
    stage = prim.GetStage()
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith(_INPUTS_PREFIX):
            continue
        param_path = name[len(_INPUTS_PREFIX):].replace(":", ".")

        # List-group arrays (inputs:list_name:param_name = Vt array of per-row values) must
        # be distributed across the list's indexed rows, exactly like _load_wire_inputs does
        # -- never set whole onto the scalar row-template param (that raises a type error). A
        # list's rows can be (re)created during execute (e.g. Export USD rebuilding its layer
        # rows), so restoring the saved per-row values here, after execute, is what makes them
        # survive a load.
        parts = param_path.split(".")
        if len(parts) >= 2:
            parent = chunk.get_param(".".join(parts[:-1]))
            if isinstance(parent, core.ChunkListGroupBase):
                val = attr.Get()
                if val is not None and hasattr(val, "__iter__") and not isinstance(val, str):
                    values = list(val)
                    if parent.size < len(values):
                        parent.size = len(values)
                    template_name = parts[-1]
                    for i, v in enumerate(values):
                        if px := parent.get_indexed_param(template_name, i):
                            px.value = v
                    _restore_row_expressions(attr, parent, template_name)
                continue

        param = chunk.get_param(param_path)
        if param is None:
            continue
        existing = getattr(param, "_param_attr_path", None)
        if existing and existing.startswith(prim_path):
            continue  # already wired to chunk storage from first _load_wire_inputs
        # A scalar is bound the way a live edit binds it: to its chunk-prim storage --
        # `attr`, right here -- which _connect_param_attr reads the expression text or
        # value back from. Unbound, a later edit is never recorded (a lofted Stage
        # Expression variable saved without its value). Bound straight to the real prim
        # attr, as show_attribute binds, writing the resolved value there fed back into the
        # param and replaced a ${VAR} with a number -- so that attr becomes one the storage
        # drives instead.
        if isinstance(param, core.ChunkParamBase):
            param._connect_param_attr(attr)
            if existing:
                # By path: a chunk gated off already has its layer muted, so the real prim
                # is not on the stage to hand over as an attribute. set_chunks_muted pushes
                # the value once it is.
                #
                # Through connect_usd_attr, not a hand-rolled append + _author_driven_connections:
                # only connect_usd_attr honours _driven_var_selections, so a param bound inside a
                # variant (show_attribute) keeps its variant scope on this reload path.
                # _connect_param_attr above leaves param._var_selections alone (its
                # var_selections defaults to None, "don't touch"), but the driven connection
                # this authors would otherwise be written outside the variant, leaking into
                # every variant.
                if real_attr := stage.GetAttributeAtPath(Sdf.Path(existing)):
                    param.connect_usd_attr(real_attr, var_selections=getattr(param, "_var_selections", ()))
                # As in _sync_param_input: param._var_selections described `existing` (the
                # real attr, just handed to connect_usd_attr above), not the chunk's own
                # storage attr _param_attr_path is now bound to. Clear it so this param's
                # own future value writes resolve through get_attr_home_layer instead of a
                # stale variant scope that silently swallows them.
                param._var_selections = ()
            continue

        # A ${VAR} or @ref@ is restored as its text, not as the value it had at save time.
        val = chunk_utils.read_expression_text(attr) or attr.Get(0)
        if val is None:
            continue
        param.value = val
        val = param.value

        # Push the value to the real prim attribute by hand as well. Assigning the param
        # normally writes through on the ATTRIBUTE_CHANGED listener, but that dispatch is
        # damped for the duration of a load -- and this runs inside one. Without this the
        # param ends up holding the saved value while the prim it drives keeps its schema
        # default, which is a Light chunk reloading as intensity 1 on a 1x1 rectangle and
        # a render lit by whatever fallback the delegate supplies.
        #
        # Authored through the chunk's own edit context so it lands in the chunk's layer,
        # the same place its execute() would have put it, and is rebuilt with the rest on
        # the next cook rather than lingering as an override nobody wrote.
        layer = param.get_edit_context()
        if existing and layer is not None and not stage.IsLayerMuted(layer.identifier):
            bound = stage.GetAttributeAtPath(Sdf.Path(existing))
            if bound and not (isinstance(val, str) and bound.GetTypeName() != Sdf.ValueTypeNames.String):
                with Usd.EditContext(stage, layer):
                    bound.Set(val)


def _find_lofted_inner(stage: Usd.Stage, context: core.ChunkBase, context_attr_path: str):
    """The inner chunk and param whose attribute is lofted to context_attr_path.

    Args:
        stage: The stage.
        context: The group the param is lofted onto.
        context_attr_path: The group attribute's path.

    Returns:
        (inner chunk, inner param), or (None, None) if nothing inside feeds it.
    """
    for inner_chunk in context.get_my_params():
        if not isinstance(inner_chunk, core.ChunkBase):
            continue
        for _, param in _iter_chunk_params(inner_chunk):
            if not param._param_attr_path:
                continue
            inner_attr = stage.GetAttributeAtPath(Sdf.Path(param._param_attr_path))
            if not (inner_attr and inner_attr.IsValid()):
                continue
            if any(str(c) == context_attr_path for c in inner_attr.GetConnections()):
                return inner_chunk, param
    return None, None


def unloft_param(context_param: core.ChunkParamBase) -> None:
    """Remove a lofted param from its group. The inner param keeps its current value.

    A copy lofted further up is removed first, and nothing is left connected to the
    group's attribute.

    Args:
        context_param: The lofted param on the group.
    """
    sync = ChunkStageSync()
    if not (sync.initialized and sync._stage and getattr(context_param, "_lofted", False)):
        return
    stage = sync._stage
    context = context_param.get_chunk()
    attr_path = context_param._param_attr_path
    context_attr = stage.GetAttributeAtPath(Sdf.Path(attr_path)) if attr_path else None
    if context is None or not (context_attr and context_attr.IsValid()):
        return

    chunks = list(sync._scene.get_chunks()) if sync._scene else []

    # A copy lofted one level up is fed by this one, so it goes first.
    targets = {str(c) for c in context_attr.GetConnections()}
    for outer in chunks if targets else ():
        for upper in [p for p in outer.get_my_params()
                      if getattr(p, "_lofted", False) and p._param_attr_path in targets]:
            unloft_param(upper)

    _, inner = _find_lofted_inner(stage, context, attr_path)
    if inner is not None:
        sync.clear_attr_connections(stage.GetAttributeAtPath(Sdf.Path(inner._param_attr_path)))
        if hasattr(inner, "_loft_back"):
            del inner._loft_back  # drops the inner -> group sync callback

    # Break wires out of the lofted port: the attribute they read is about to go.
    expr = f"@{context.name}.{context_param.name}@"
    for chunk in chunks:
        for _, param in _iter_chunk_params(chunk):
            if isinstance(param.raw_value, str) and expr in param.raw_value:
                param.value = param.value
    root = stage.GetPrimAtPath(_CHUNKS_ROOT)
    for prim in Usd.PrimRange(root) if root else ():
        for attr in prim.GetAttributes():
            if any(str(c) == attr_path for c in attr.GetConnections()):
                sync.clear_attr_connections(attr)

    context_param.request_delete()
    sync._delete_stale_attr_specs(context_attr.GetPrim(), context_attr.GetName())


def _restore_lofted_params(stage: Usd.Stage, deferred: list) -> None:
    """Recreate Python-side lofted context params that were saved to USD.

    Scans the context prim's inputs: attributes and matches them to inner chunk
    params whose USD attribute has a connection pointing back to the context prim.
    Reconstructs the context_param clone and its forward-to-inner callback so the
    lofted UI and value forwarding work identically to a freshly lofted param.
    """
    seen_contexts: set = set()

    for _prim, chunk in deferred:
        context = chunk.get_context()
        if not context or id(context) in seen_contexts:
            continue

        context_prim_path = Sdf.Path(context.prim_path if context.prim_path else _CHUNKS_ROOT)
        context_prim = stage.GetPrimAtPath(context_prim_path)
        if not (context_prim and context_prim.IsValid()):
            continue

        seen_contexts.add(id(context))

        for context_attr in context_prim.GetAttributes():
            attr_name = context_attr.GetName()
            if not attr_name.startswith(_INPUTS_PREFIX):
                continue

            param_name = attr_name[len(_INPUTS_PREFIX):]
            # Only check direct ChunkParamBase children of context (already-lofted),
            # not nested params inside inner chunks which share the same name.
            if any(isinstance(p, core.ChunkParamBase) and p.name == param_name
                   for p in context.get_my_params()):
                continue

            inner_chunk, inner_param = _find_lofted_inner(stage, context, str(context_attr.GetPath()))
            if not inner_param:
                continue

            # Named from the attribute, not from the inner param the clone came from.
            # The interface name is the file's to decide -- it is qualified at loft time
            # (Rim_base_color) and may have been renamed since -- whereas the clone would
            # arrive calling itself base_color. Getting this wrong reverts the name on
            # every reload, and also defeats the already-lofted guard above, which
            # compares against the attribute's name: the param would never match, so a
            # duplicate would be added each time the file was opened.
            context_param = inner_param.clone()
            context_param.name = param_name
            # See loft_param_to_context's matching comment: without this, a reload showed
            # every lofted param under its bare inner label again, with no way to tell
            # which chunk it came from, even though live lofting (moments before a save)
            # had shown "Reference1: Prim Path". A user's own rename (chunk_qt_widgets'
            # "Rename...") is stored as customData on context_attr and takes priority over
            # the generated default -- it is exactly what has to override it.
            context_param.label = (
                chunk_utils.read_lofted_label(context_attr)
                or _lofted_label(inner_chunk, context, inner_param.get_label())
            )
            context_param._lofted = True
            context_param._connect_param_attr(context_attr)
            context.add_param(context_param)

            # Re-wired on every restore, same as loft_param_to_context's own copy -- a
            # rename after THIS reload has to survive the next one too.
            def _persist_lofted_label(_owner, _notice, _sender=None, _cp=context_param, _attr=context_attr):
                home = chunk_utils.get_attr_home_layer(_attr) or stage.GetSessionLayer()
                with Usd.EditContext(stage, home):
                    chunk_utils.store_lofted_label(_attr, _cp.label)

            context_param._loft_label_persist = _persist_lofted_label  # strong-ref pin
            context_param.add_callback(
                _persist_lofted_label,
                sender=context_param.get_attribute_sender("label"),
                send_on_create=False,
                private=True,
            )

            # As in loft_param_to_context: clone() carries the inner chunk over as the cached
            # parent, and the param now lives on the group.
            context_param._chunk_parent = None
            _wire_loft_sync(inner_param, context_param)

            # Deliver the restored value down to the inner param once, by hand. The inner
            # param's own _connect_param_attr deliberately leaves its value alone when it
            # sees a loft connection, because a lofted value is meant to arrive through
            # this callback rather than be read from USD twice. On load nothing ever sent
            # it: _connect_param_attr above set the context param before the callback
            # existed, and send_on_create=False means registering it sends nothing. The
            # inner param was left holding its type default -- an empty file path on a
            # Reference chunk, which cooks to no reference and renders an empty stage --
            # while USD and the lofted UI both showed the saved value. Seeding here is
            # before the execute pass, so the first cook already sees the real value.
            context_param._loft_fwd(None, None)
            _mirror_menu_items(inner_param, context_param)
            _forward_button_click(inner_param, context_param)
