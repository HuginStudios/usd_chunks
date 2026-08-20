"""Undo/redo diagnostics: why the node does not come back, and why the sliders do not move.

Run in usdview's Python shell:

    exec(open(r"C:\\usd_chunks\\scripts\\core_usd\\test_value_clips\\diagnose_redo.py").read())

Then follow the printed instructions. Read-only -- it prints and listens, never edits.

Answers two questions that could not be reproduced outside a real session:

  A. Ctrl+Z moves the prim back but leaves the chunk's translate values alone.
     ChunkStageSync._on_objects_changed already mirrors USD into params, but only for paths
     in GetChangedInfoOnlyPaths(). If undo reports its restore as a *resync* instead, the
     mirror never sees it. The listener below prints which of the two USD actually used,
     and whether the guards at the top of that handler suppressed the whole thing.

  B. Redo restores the chunk but its node never returns to the graph.
     The graph pairs chunk *prims* against live *chunk objects* and silently skips a prim
     it cannot match. So: does the prim exist after the redo, or not?
"""

from pxr import Sdf, Tf, Usd  # noqa: F401

from usd_chunks import chunk_notice, chunk_registry
from usd_chunks.plugins import chunk_usd

_listener = None
_log = []


def _line(label, value):
    print(f"  {label:<32} {value}")


def _roots(scene):
    """Every chunk root actually in use -- '/chunks' is only the default, a tab context
    gives its own ('/Tab1'). Hardcoding the default is why the first run of this script
    reported '/chunks is INVALID' and found no prims at all."""
    found = {}
    for c in scene.get_chunks():
        try:
            found[str(chunk_usd._resolve_chunk_root(c))] = True
        except Exception:  # noqa: BLE001
            pass
    return sorted(found) or [chunk_usd._CHUNKS_ROOT]


def state():
    """Snapshot of prims vs chunks vs graph nodes. Run before and after the redo."""
    reg = chunk_registry.ChunkRegistry()
    scene = getattr(reg, "scene", None)
    stage = scene.get_stage() if scene else None
    if stage is None:
        print("no stage -- is the chunks plugin loaded?")
        return

    sync = chunk_usd.ChunkStageSync()

    print("\n=== chunk PRIMS ===")
    prim_paths = set()
    for root_path in _roots(scene):
        root = stage.GetPrimAtPath(Sdf.Path(root_path))
        if not root.IsValid():
            _line(root_path, "INVALID (no prim at this path)")
            continue
        for prim in root.GetChildren():
            if prim.GetTypeName() != chunk_usd._CHUNK_PRIM_TYPENAME:
                continue
            prim_paths.add(str(prim.GetPath()))
            layers = [s.layer.identifier.split(":")[-1] for s in prim.GetPrimStack()]
            _line(str(prim.GetPath()), f"active={prim.IsActive()}  layers={layers}")

    print("\n=== live CHUNKS (and the path the graph looks for) ===")
    chunk_paths = {}
    for c in scene.get_chunks():
        try:
            key = f"{chunk_usd._resolve_chunk_root(c)}/{c.name}"
        except Exception as exc:  # noqa: BLE001
            _line(getattr(c, "name", c), f"<root unresolved: {exc}>")
            continue
        chunk_paths[key] = c
        registered = sync._chunk_prim_paths.get(id(c))
        note = "" if registered == key else f"   <-- sync says {registered!r}"
        _line(key, f"ctx={getattr(c.get_context(), 'name', None)}{note}")

    print("\n=== THE ANSWER for the missing node ===")
    for path in sorted(chunk_paths):
        if path not in prim_paths:
            _line("chunk alive, PRIM MISSING", f"{path}   <-- redo did not restore the prim")
    for path in sorted(prim_paths):
        if path not in chunk_paths:
            _line("prim alive, no chunk", f"{path}   <-- graph skips it, no node")
    if prim_paths == set(chunk_paths):
        print("  prims and chunks pair up perfectly -- the node is missing for another reason")

    print("\n=== params vs USD (the slider-not-undoing question) ===")
    mismatched = 0
    for c in scene.get_chunks():
        for _, param in chunk_usd._iter_chunk_params(c):
            data_path = getattr(param, "_param_attr_path", None)
            if not data_path:
                continue
            attr = stage.GetAttributeAtPath(Sdf.Path(data_path))
            if not (attr and attr.IsValid()):
                continue
            usd_val = attr.Get(0)
            if usd_val is not None and usd_val != param.value:
                mismatched += 1
                _line(f"{c.name}.{param.name}", f"param={param.value}  USD={usd_val}  <-- DRIFTED")
    if not mismatched:
        print("  params agree with USD")

    try:
        import shiboken6
        from PySide6 import QtWidgets

        for w in QtWidgets.QApplication.allWidgets():
            if type(w).__name__ != "ChunkGraphView" or not shiboken6.isValid(w):
                continue
            print("\n=== graph nodes ===")
            _line("showing", sorted(getattr(w.scene(), "_nodes", {})))
            break
    except Exception as exc:  # noqa: BLE001
        print(f"\n  could not inspect the graph: {exc}")
    print()


def watch():
    """Listen to ObjectsChanged and report what an undo actually looks like."""
    global _listener
    unwatch()
    _log.clear()

    def _on(notice, sender):
        resynced = [str(p) for p in notice.GetResyncedPaths()]
        info = [str(p) for p in notice.GetChangedInfoOnlyPaths()]
        _log.append((resynced, info, bool(chunk_notice._usd_write_active)))

    # Kept on the module, not local: Tf.Notice holds Python callables only weakly, so a
    # local one is collected the moment this function returns and the registration goes
    # quietly dead (see usd-undo PR #4 -- same trap).
    watch._cb = _on
    _listener = Tf.Notice.RegisterGlobally(Usd.Notice.ObjectsChanged, watch._cb)
    print("watching. Now press Ctrl+Z once, then run:  report()")


def unwatch():
    global _listener
    if _listener is not None:
        _listener.Revoke()
        _listener = None


def report():
    """What USD said during the undo -- the deciding evidence for bug A."""
    unwatch()
    if not _log:
        print("nothing captured -- did the undo actually run?")
        return

    print(f"\n=== {len(_log)} ObjectsChanged notices during the undo ===")
    param_resync = param_info = 0
    for resynced, info, write_active in _log:
        for p in resynced:
            if "inputs:" in p:
                param_resync += 1
        for p in info:
            if "inputs:" in p:
                param_info += 1
        if write_active:
            print("  NOTE: _usd_write_active was set -- the whole handler bailed early")

    _line("param paths as RESYNC", param_resync)
    _line("param paths as INFO-ONLY", param_info)
    print()
    if param_resync and not param_info:
        print("  => undo reports param changes as RESYNCS.")
        print("     The mirror only reads GetChangedInfoOnlyPaths(), so it never sees them.")
        print("     Fix: have _on_objects_changed mirror params from all_changed too.")
    elif param_info:
        print("  => params DID arrive as info-only, so the mirror should have run.")
        print("     Look at the guards at the top of _on_objects_changed instead.")
    else:
        print("  => no param paths in the notices at all: undo never touched inputs:*,")
        print("     so the prim moved via some other layer and the params were never involved.")
    print("\nSample of the first notice:")
    resynced, info, _ = _log[0]
    _line("resynced", resynced[:6])
    _line("info-only", info[:6])
    print()


print(__doc__)
print("Commands:  state()   watch()   report()")
print()
print("For the MISSING NODE:  reproduce it (drag, undo back, redo), then run  state()")
print("For the SLIDERS:       move the gizmo, run  watch(), press Ctrl+Z, run  report()")
print()
