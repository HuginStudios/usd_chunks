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
from usd_chunks import chunk_stage

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
            found[str(chunk_stage._resolve_chunk_root(c))] = True
        except Exception:  # noqa: BLE001
            pass
    return sorted(found) or [chunk_stage._CHUNKS_ROOT]


def state():
    """Snapshot of prims vs chunks vs graph nodes. Run before and after the redo."""
    reg = chunk_registry.ChunkRegistry()
    scene = getattr(reg, "scene", None)
    stage = scene.get_stage() if scene else None
    if stage is None:
        print("no stage -- is the chunks plugin loaded?")
        return

    sync = chunk_stage.ChunkStageSync()

    print("\n=== chunk PRIMS ===")
    prim_paths = set()
    for root_path in _roots(scene):
        root = stage.GetPrimAtPath(Sdf.Path(root_path))
        if not root.IsValid():
            _line(root_path, "INVALID (no prim at this path)")
            continue
        for prim in root.GetChildren():
            if prim.GetTypeName() != chunk_stage._CHUNK_PRIM_TYPENAME:
                continue
            prim_paths.add(str(prim.GetPath()))
            layers = [s.layer.identifier.split(":")[-1] for s in prim.GetPrimStack()]
            _line(str(prim.GetPath()), f"active={prim.IsActive()}  layers={layers}")

    print("\n=== live CHUNKS (and the path the graph looks for) ===")
    chunk_paths = {}
    for c in scene.get_chunks():
        try:
            key = f"{chunk_stage._resolve_chunk_root(c)}/{c.name}"
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
        for _, param in chunk_stage._iter_chunk_params(c):
            data_path = getattr(param, "_param_attr_path", None)
            if not data_path:
                continue
            attr = stage.GetAttributeAtPath(Sdf.Path(data_path))
            if not (attr and attr.IsValid()):
                continue
            usd_val = attr.Get(0)
            # Same tolerance the mirror uses. A param holds a 64-bit float and USD stores
            # 32, so an exact compare reports every round-tripped value as drifted -- which
            # it did on the first run here, for three values that were in fact fine.
            if usd_val is not None and not chunk_stage._values_agree(usd_val, param.value):
                mismatched += 1
                _line(f"{c.name}.{param.name}", f"param={param.value}  USD={usd_val}  <-- DRIFTED")
    if not mismatched:
        print("  params agree with USD (float32 rounding ignored)")

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


_traced = []


_trace_listener = None


def trace_mirror():
    """Record, per notice, whether it carried param paths and whether a write was in flight.

    For "undo works for a while and then stops". Run it, undo several steps, then run
    mirror_report().

    Its own listener rather than a wrapper around ChunkStageSync's handler. Wrapping does
    not work and looked like it did: Tf.Notice.Register was handed a *bound method* when
    the plugin started, and a bound method captures the function at bind time -- so
    reassigning the attribute afterwards changes nothing about what USD calls, and the
    trace comes back empty with no error. Registering separately sees the same notices, and
    _usd_write_active is a module-level flag set around whole write blocks, so reading it
    here gives the same answer the real handler got regardless of listener order.
    """
    global _trace_listener
    stop_trace()
    _traced.clear()

    def _on(notice, sender):
        paths = [str(p) for p in notice.GetResyncedPaths()]
        paths += [str(p) for p in notice.GetChangedInfoOnlyPaths()]
        params = [p for p in paths if "inputs:" in p]
        _traced.append((bool(chunk_notice._usd_write_active), params))

    # Held on the function: Tf.Notice keeps Python callables only weakly.
    trace_mirror._cb = _on
    _trace_listener = Tf.Notice.RegisterGlobally(Usd.Notice.ObjectsChanged, trace_mirror._cb)
    print("tracing. Undo several steps, then run:  mirror_report()")


def stop_trace():
    global _trace_listener
    if _trace_listener is not None:
        _trace_listener.Revoke()
        _trace_listener = None


def mirror_report():
    stop_trace()

    if not _traced:
        print("nothing traced -- did any undo actually run? Click the VIEWPORT first.")
        return

    acted = [p for s, p in _traced if not s and p]
    dropped = [p for s, p in _traced if s and p]
    print(f"\n=== {len(_traced)} notices ===")
    _line("carrying param paths, acted on", len(acted))
    _line("carrying param paths, SUPPRESSED", len(dropped))
    print()
    if dropped:
        print("  => some notices arrived while a write was in flight, and were discarded.")
        print()
        print("     Read the ACTED ON count, not this one. A chunk cooking after a mirrored")
        print("     write emits suppressed notices carrying its own param paths, and those")
        print("     are correctly ignored -- so SUPPRESSED is inflated by normal activity.")
        print(f"     The real question: you undid N steps, and {len(acted)} notices carrying")
        print("     param paths got through. If that is well short of N, undos are being")
        print("     swallowed. If it matches N, they are not, and the mirror's write is")
        print("     being undone by something after it.")
        _line("example dropped", dropped[0][:3])
    elif acted:
        print("  => nothing was suppressed, so the in-flight-write theory is WRONG.")
        print(f"     {len(acted)} notices carried param paths and the mirror saw them all.")
        print("     If fewer sliders moved than that, the mirror ran and its write did not")
        print("     stick -- suspect the chunk cooking straight over it.")
    else:
        print("  => no notice carried a param path at all.")
        print("     The undo never touched inputs:*, so the prim moved by some other route")
        print("     and the params were never part of the undo to begin with.")
    print()


def audit():
    """Every layer that holds a chunk-prim spec or a scene-prim override, composed or not.

    For "I deleted a chunk and it came back". Deletion removes the prim spec from every
    layer in stage.GetLayerStack(), so a spec sitting in a layer that is NOT in the stack at
    that moment -- muted, detached, or known only to ChunkLayerManager -- survives untouched
    and can be recomposed later. state() cannot show this: it reads the composed stage, so
    a spec in an uncomposed layer is invisible to it.

    Run it right after deleting, and again after the drag that brings them back.
    """
    reg = chunk_registry.ChunkRegistry()
    scene = getattr(reg, "scene", None)
    stage = scene.get_stage() if scene else None
    if stage is None:
        print("no stage")
        return

    manager = chunk_registry.ChunkLayerManager()
    in_stack = {layer.identifier: layer for layer in stage.GetLayerStack(includeSessionLayers=True)}
    known = dict(in_stack)
    for layer in (getattr(manager, "chunk_layers", None) or {}).values():
        if layer is not None:
            known.setdefault(layer.identifier, layer)

    print("\n=== layers holding chunk prims or prim overrides ===")
    for identifier, layer in known.items():
        if layer is None:
            continue
        chunk_specs, overrides = [], []

        def _walk(spec):
            for child in spec.nameChildren:
                path = str(child.path)
                if child.typeName == chunk_stage._CHUNK_PRIM_TYPENAME:
                    chunk_specs.append(path)
                elif child.properties or child.nameChildren:
                    if not path.startswith("/chunks") and "/Tab" not in path:
                        if child.properties:
                            overrides.append(path)
                _walk(child)

        try:
            _walk(layer.pseudoRoot)
        except Exception as exc:  # noqa: BLE001
            print(f"  {identifier[-24:]}  <unreadable: {exc}>")
            continue
        if not chunk_specs and not overrides:
            continue
        composed = "IN STACK" if identifier in in_stack else "NOT COMPOSED  <-- delete cannot reach it"
        print(f"  {identifier[-24:]}  [{composed}]")
        for path in chunk_specs:
            _line("    chunk prim", path)
        for path in overrides:
            _line("    prim override", path)

    print("\n=== live chunks ===")
    _line("names", [c.name for c in scene.get_chunks()])
    muted = [m for m in stage.GetMutedLayers()]
    if muted:
        _line("muted layers", [m[-24:] for m in muted])
    print()


def gizmo_pos():
    """Why a handle is drawn somewhere other than on the prim.

    Select the prim, then run this. The handler positions the gizmo from
    XformCache.GetLocalToWorldTransform on whatever is in selectedPrims, so the answer is
    one of: the path it tracked is not the prim you picked, the prim does not resolve, or
    the world transform genuinely is the identity.
    """
    from pxr import UsdGeom
    import shiboken6
    from PySide6 import QtWidgets

    handler = None
    for w in QtWidgets.QApplication.allWidgets():
        if not shiboken6.isValid(w):
            continue
        press = getattr(w, "mousePressEvent", None)
        owner = getattr(getattr(press, "func", None), "__self__", None)
        if owner is not None and type(owner).__name__ == "GizmoHandler":
            handler = owner
            break
    if handler is None:
        print("no GizmoHandler found -- is usd_gizmos loaded?")
        return

    stage = handler.scene.get_stage()
    _line("tracked selection", [str(p) for p in handler.selectedPrims])
    try:
        _line("host says selected", [str(p) for p in (handler.scene.get_selected_prims() or [])])
    except NotImplementedError:
        _line("host says selected", "<not implemented>")

    fresh = UsdGeom.XformCache(0)
    for path in handler.selectedPrims:
        prim = stage.GetPrimAtPath(path)
        if not (prim and prim.IsValid()):
            _line(str(path), "PRIM DOES NOT RESOLVE  <-- gizmo falls back to the origin")
            continue
        cached = handler._xformCache.GetLocalToWorldTransform(prim)
        live = fresh.GetLocalToWorldTransform(prim)
        _line(str(path).rsplit("/", 1)[-1], f"cached={cached.ExtractTranslation()}  fresh={live.ExtractTranslation()}")
        if cached.ExtractTranslation() != live.ExtractTranslation():
            _line("", "  <-- STALE CACHE: only cleared after a drag, never on selection")
    _line("gizmo.world", getattr(handler.gizmo, "world", None))
    print()


def undo_entries():
    """How many entries are on the real undo stack right now.

    usd-undo coalesces by mouse press/release: its app-level event filter accumulates edits
    from MouseButtonPress and flushes one entry on MouseButtonRelease. Anything arriving
    AFTER the release goes through a 0ms timer and becomes a separate entry -- and a chunk's
    cook is deferred, so its write to the prim can land there. That second entry holds the
    prim move with no param change in it, which is what an undo step that shifts the object
    but not the sliders looks like.

    Usage: run it, do ONE drag, run it again. A rise of 1 means the drag is a single entry
    and the theory is wrong. A rise of 2 or more confirms it.
    """
    try:
        import hugin
    except ImportError:
        print("usd-undo not loaded in this session")
        return None
    manager = hugin.initialize()
    undo = len(getattr(manager, "_undoStack", ()))
    redo = len(getattr(manager, "_redoStack", ()))
    pending = manager._timer.isActive() if hasattr(manager, "_timer") else "?"
    _line("undo entries", undo)
    _line("redo entries", redo)
    _line("flush pending (post-release edit)", pending)
    return undo


def drag_test():
    """Guided version of undo_entries(): prints the before count and what to do next."""
    before = undo_entries()
    if before is None:
        return
    drag_test._before = before
    print()
    print(f"  baseline {before}. Now do ONE drag, then run:  drag_result()")


def drag_result():
    before = getattr(drag_test, "_before", None)
    if before is None:
        print("run drag_test() first")
        return
    after = undo_entries()
    if after is None:
        return
    grew = after - before
    print()
    _line("entries added by one drag", grew)
    print()
    if grew <= 1:
        print("  => one drag is one undo entry. The cook is landing inside the drag, so")
        print("     the deferred-cook theory is WRONG and the extra steps come from")
        print("     somewhere else (chunk creation, stray clicks elsewhere in the UI).")
    else:
        print("  => one drag produced more than one entry, so the cook (or something else")
        print("     deferred) is landing after the release and becoming its own step.")
        print("     Undoing that one moves the prim and leaves the sliders alone.")
    print()


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
print("Commands:  state()  watch()  report()  trace_mirror()  mirror_report()")
print("           undo_entries()  drag_test()  drag_result()  audit()  gizmo_pos()")
print()
print("For the MISSING NODE:  reproduce it (drag, undo back, redo), then run  state()")
print("For the SLIDERS:       move the gizmo, run  watch(), press Ctrl+Z, run  report()")
print("For UNDO MISSING STEPS: run  trace_mirror(), undo several times, run  mirror_report()")
print("For UNEVEN UNDO STEPS:  run  drag_test(), do ONE drag, run  drag_result()")
print("For DELETED-BUT-BACK:   delete the chunks, run  audit(), drag, run  audit() again")
print()
