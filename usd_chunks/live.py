"""Live sessions: a network kept open while its params are changed from outside.

`usdchunks run net.usda --live` cooks the network as `run` does, then stays up. Another
process changes params with `usdchunks send net.usda Chunk.param=value`, or send() here.
The two talk through two files beside the network and nothing else:

    net.live.usda   the client writes param values into it, as plain USD overs on the
                    chunk prims. The session holds it as a sublayer of its session layer.
    net.live.json   the session writes the manifest into it: each param's attribute and
                    type, and which request it has finished.

When the overrides file changes the session reloads it, and the values reach the chunks
the way any edit on the stage does: ChunkStageSync adopts the authored inputs and the
solve recooks only what they reach. Nothing here sets a param directly, so a live edit is
exactly a stage edit -- and the overrides file is a record of what was tried, to read,
diff, keep or delete. It is left in place when the session ends, so the next session
opens with the same values; `send --reset` clears it.

The manifest exists because a param still at its default has no attribute yet: its name
on the prim (grouped, `inputs:cube:width`) and its type are known only to the loaded
chunk. A client that never loads the network writes the right attribute from it.
"""
from __future__ import annotations

import difflib
import json
import os
import time
from pathlib import Path

from pxr import Sdf, Usd

from usd_chunks import chunk_core as core
from usd_chunks import chunk_naming
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_stage import (
    _iter_chunk_params,
    _iter_chunk_vectors,
    get_chunk_prim,
    is_transient_param,
    param_sdf_type,
)

#: Where in the overrides layer's customLayerData a client numbers its writes, so it can
#: wait for the session to have finished the one it made.
REQUEST_KEY = "usdchunks:liveRequest"

#: How often a session looks at the overrides file, in seconds.
POLL_SECONDS = 0.2

#: The manifest's name and type for a chunk's on/off switch, `Chunk.active`: its prim's
#: active metadata rather than an attribute.
ACTIVE = "active"

_EMPTY_LAYER = "#usda 1.0\n"
_INTEGER_TYPES = {"int", "int64", "uint", "uint64", "uchar"}
_REAL_TYPES = {"float", "double", "half"}
_TEXT_TYPES = {"string", "token", "asset"}


# How many sessions are serving in this process (is_serving).
_serving = 0


def is_serving() -> bool:
    """True while a live session in this process is up.

    What lets a chunk keep something costly between cooks only then -- the Render chunk
    keeps its renderer, and with it the scene the renderer translated, rather than
    building both again for every render.
    """
    return _serving > 0


class LiveError(RuntimeError):
    """A send that cannot be made: no session, or a param the manifest does not have."""


def overrides_path(network: str | os.PathLike) -> Path:
    """The overrides layer beside network: net.usda -> net.live.usda."""
    path = Path(network)
    return path.with_name(f"{path.stem}.live.usda")


def manifest_path(network: str | os.PathLike) -> Path:
    """The manifest beside network: net.usda -> net.live.json."""
    path = Path(network)
    return path.with_name(f"{path.stem}.live.json")


def _write_atomic(path: Path, text: str) -> None:
    """Write text to path in one step, so a reader never sees half of it."""
    tmp = path.with_name(f"{path.name}.tmp")
    # Bytes, not write_text: on Windows that turns every newline into CRLF, the ones inside
    # a multi-line string value too -- and an Inline USD chunk's code that arrives with
    # carriage returns in it no longer parses.
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def _stamp(path: Path) -> tuple[int, int] | None:
    # Size as well as the time: two writes inside one tick of the file system's clock
    # look like none at all by their time alone.
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class LiveSession:
    """The session side: an overrides layer on a stage, reloaded when its file changes.

    Attach before the network loads, so values left in the file by an earlier session
    are in place for the first cook rather than arriving as edits after it.
    """

    def __init__(self, stage: Usd.Stage, network: str | os.PathLike):
        self.network = Path(network).resolve()
        self.overrides_path = overrides_path(self.network)
        self.manifest_path = manifest_path(self.network)
        if not self.overrides_path.exists():
            _write_atomic(self.overrides_path, _EMPTY_LAYER)
        self.layer = Sdf.Layer.FindOrOpen(str(self.overrides_path))
        self._session_layer = stage.GetSessionLayer()
        # Strongest: the chunk prims are in the network's own layers, all weaker than this.
        self._session_layer.subLayerPaths.insert(0, self.layer.identifier)
        self._stamp = _stamp(self.overrides_path)
        self._closed = False
        global _serving
        _serving += 1

    def request(self) -> int:
        """The number of the last write a client made to the overrides layer."""
        return int(self.layer.customLayerData.get(REQUEST_KEY, 0))

    def poll(self) -> bool:
        """Reload the overrides layer if its file changed. True when it did."""
        stamp = _stamp(self.overrides_path)
        if stamp is None or stamp == self._stamp:
            return False
        self._stamp = stamp
        # Forced: a reload goes by the file's time, and a write inside the same tick as the
        # last one would be skipped.
        self.layer.Reload(force=True)
        return True

    def publish(self, scene, error: str | None = None, live: bool = True) -> None:
        """Write the manifest: every settable param, and the request finished so far."""
        manifest = {
            "network": str(self.network),
            "overrides": str(self.overrides_path),
            "live": live,
            "pid": os.getpid(),
            "done": self.request(),
            "error": error,
            "params": settable_params(scene),
        }
        _write_atomic(self.manifest_path, json.dumps(manifest, indent=1))

    def close(self, scene) -> None:
        """Say the session is over, and take the overrides layer off the stage."""
        if not self._closed:
            self._closed = True
            global _serving
            _serving -= 1
        self.publish(scene, live=False)
        paths = self._session_layer.subLayerPaths
        if self.layer.identifier in paths:
            paths.remove(self.layer.identifier)


def _owners(scene):
    """The contexts (whose params are lofted interfaces) and every chunk, at any depth."""
    return [*scene.get_contexts(), *core.all_chunks(scene.get_chunks())]


def _prim_path(owner) -> str | None:
    prim = get_chunk_prim(owner)
    if prim is not None:
        return str(prim.GetPath())
    return getattr(owner, "prim_path", None) or None


def settable_params(scene) -> dict[str, dict[str, str]]:
    """{"Chunk.param.path": {"attr": attribute path, "type": Sdf type name}} for the scene.

    Every param that has been built, and every input already authored on a chunk prim --
    which covers a param a chunk builds only once its group is opened (Render Settings'
    renderer settings) as long as the network has a value for it. A name two chunks share
    is listed by prim path instead ("/chunks/G/box.cube.width"), since the short name
    would pick one of them silently.
    """
    rows = []  # (short key, long key, attr path, type)
    seen_attrs = set()
    for owner in _owners(scene):
        prim_path = _prim_path(owner)
        if not prim_path:
            continue
        if isinstance(owner, core.ChunkBase):
            # Switching a chunk off is its prim's `active`, which the sync adopts from the
            # stage like any other edit: its output goes, and the chain cooks on without it.
            rows.append((f"{owner.name}.{ACTIVE}", f"{prim_path}.{ACTIVE}", prim_path, ACTIVE))
        found = [*_iter_chunk_params(owner), *_iter_chunk_vectors(owner)]
        for path, param in found:
            if is_transient_param(param) or param.has_flag(ChunkFlags.ParamFlags.OUTPUT):
                continue
            sdf_type = param_sdf_type(param)
            if sdf_type is None or ChunkFlags.ParamType.BUTTON in param.evaluate_type():
                continue
            attr = f"{prim_path}.{chunk_naming.INPUTS_PREFIX}{path.replace('.', ':')}"
            seen_attrs.add(attr)
            rows.append((f"{owner.name}.{path}", f"{prim_path}.{path}", attr, str(sdf_type)))
        prim = scene.get_stage().GetPrimAtPath(prim_path)
        for usd_attr in prim.GetAttributes() if prim else ():
            name = usd_attr.GetName()
            attr = str(usd_attr.GetPath())
            if (not name.startswith(chunk_naming.INPUTS_PREFIX) or chunk_naming.is_exec_input_name(name)
                    or attr in seen_attrs):
                continue
            rest = name[len(chunk_naming.INPUTS_PREFIX):]
            rows.append((f"{owner.name}.{rest}", f"{prim_path}.{rest}", attr, str(usd_attr.GetTypeName())))
    shorts = {}
    for short, *_ in rows:
        shorts[short] = shorts.get(short, 0) + 1
    return {
        (short if shorts[short] == 1 else long): {"attr": attr, "type": sdf_type}
        for short, long, attr, sdf_type in rows
    }


def read_manifest(network: str | os.PathLike) -> dict:
    """The manifest of the session running network, or LiveError if none is."""
    path = manifest_path(network)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LiveError(f"No live session for {network}: start one with `usdchunks run {network} --live`.")
    if not manifest.get("live"):
        raise LiveError(f"The live session for {network} has ended.")
    return manifest


def _resolve(params: dict, name: str) -> dict:
    """The manifest entry for name: its full key, or Chunk.leaf when only one param ends so.

    The leaf is matched up to a namespace as well, so `Fill.intensity` finds a Light's
    `attr_grp.inputs:intensity`.
    """
    if name in params:
        return params[name]
    owner, _, leaf = name.partition(".")
    matches = [
        key for key in params
        if key.startswith(f"{owner}.") and leaf
        and (key.endswith(f".{leaf}") or key.rsplit(".", 1)[-1].rsplit(":", 1)[-1] == leaf)
    ]
    if len(matches) == 1:
        return params[matches[0]]
    if matches:
        raise LiveError(f"{name!r} is ambiguous: {', '.join(sorted(matches))}")
    close = difflib.get_close_matches(name, params, n=5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise LiveError(f"No param {name!r} in the live session.{hint}")


def _value(type_name: str, value):
    """value as the attribute's type wants it. A string is read the way `run` reads one."""
    if not isinstance(value, str) or type_name in _TEXT_TYPES:
        return value
    if type_name == "bool":
        return value.strip().lower() in ("1", "true", "yes", "on")
    try:
        if type_name in _INTEGER_TYPES:
            return int(value)
        if type_name in _REAL_TYPES:
            return float(value)
        # A vector or colour: comma-separated, brackets optional.
        number = int if type_name.startswith("int") else float
        return tuple(number(p) for p in value.strip().strip("()[]").split(",") if p.strip())
    except ValueError:
        raise LiveError(f"Cannot read {value!r} as {type_name}.")


def send(network: str | os.PathLike, values: dict | None = None, *, reset: bool = False,
         wait: bool = False, timeout: float | None = None) -> int:
    """Set params on the live session running network. Returns the request's number.

    values maps names (as `run` takes them: "Chunk.param", or the manifest's full keys)
    to values -- Python values, or strings read by the param's type. Earlier values stay
    in the overrides file unless reset clears them first. With wait, returns once the
    session has cooked the change (and rendered, if the network renders), or raises
    LiveError after timeout seconds.
    """
    manifest = read_manifest(network)
    edits = [(_resolve(manifest["params"], name), value) for name, value in (values or {}).items()]

    path = Path(manifest["overrides"])
    layer = Sdf.Layer.CreateAnonymous(".usda")
    if not reset and path.exists():
        layer.ImportFromString(path.read_text(encoding="utf-8"))
    for entry, value in edits:
        if entry["type"] == ACTIVE:
            Sdf.CreatePrimInLayer(layer, Sdf.Path(entry["attr"])).active = _value("bool", value)
            continue
        attr_path = Sdf.Path(entry["attr"])
        type_name = Sdf.ValueTypeNames.Find(entry["type"])
        prim = Sdf.CreatePrimInLayer(layer, attr_path.GetPrimPath())
        spec = layer.GetAttributeAtPath(attr_path) or Sdf.AttributeSpec(prim, attr_path.name, type_name)
        spec.default = _value(entry["type"], value)
    data = dict(layer.customLayerData)
    request = int(data.get(REQUEST_KEY, 0)) + 1
    data[REQUEST_KEY] = request
    layer.customLayerData = data
    _write_atomic(path, layer.ExportToString())

    if wait:
        _wait_for(network, request, timeout)
    return request


def _wait_for(network, request: int, timeout: float | None) -> None:
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        manifest = read_manifest(network)
        if manifest.get("done", 0) >= request:
            if manifest.get("error"):
                raise LiveError(f"The live session failed: {manifest['error']}")
            return
        if deadline is not None and time.monotonic() > deadline:
            raise LiveError(f"The live session did not finish request {request} in {timeout} s.")
        time.sleep(POLL_SECONDS / 2)
