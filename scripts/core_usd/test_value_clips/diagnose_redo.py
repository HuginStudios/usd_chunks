"""Why a redone chunk has no node in the graph.

Run in usdview's Python shell IMMEDIATELY after reproducing:
  drag a prim (transform node appears) -> undo back -> redo -> node missing.

    exec(open(r"C:\\usd_chunks\\scripts\\core_usd\\test_value_clips\\diagnose_redo.py").read())

Read-only: prints, never edits, never raises out. The graph reconciles chunk *prims*
against *live chunk objects* and silently skips any prim it cannot match to one
(_rebuild_nodes: `chunk = chunks_by_path.get(path); if chunk is None: continue`), so the
question is which half of that pairing is missing.
"""

from pxr import Sdf, Usd  # noqa: F401

from usd_chunks import chunk_registry
from usd_chunks.plugins import chunk_usd


def _line(label, value):
    print(f"  {label:<34} {value}")


def diagnose_redo():
    reg = chunk_registry.ChunkRegistry()
    scene = getattr(reg, "scene", None)
    stage = scene.get_stage() if scene else None
    if stage is None:
        print("no stage -- is the chunks plugin loaded?")
        return

    print("\n=== chunk PRIMS on the stage ===")
    root = stage.GetPrimAtPath(Sdf.Path(chunk_usd._CHUNKS_ROOT))
    prim_paths = []
    if not root.IsValid():
        print("  /chunks is INVALID (the graph treats this as transient and keeps retrying)")
    else:
        for prim in root.GetFilteredChildren(Usd.TraverseInstanceProxies(
                Usd.PrimIsActive | ~Usd.PrimIsActive)):
            if prim.GetTypeName() != chunk_usd._CHUNK_PRIM_TYPENAME:
                continue
            prim_paths.append(str(prim.GetPath()))
            src = next((s.layer.identifier.split("/")[-1] for s in prim.GetPrimStack()), "?")
            _line(str(prim.GetPath()), f"active={prim.IsActive()}  from={src}")

    print("\n=== live CHUNK OBJECTS, and the path the graph will look for ===")
    # Exactly the key _rebuild_nodes builds its lookup with. A mismatch here -- a rename to
    # Transform1 on the redo, or a context that no longer matches -- is the whole bug.
    chunk_paths = {}
    for c in scene.get_chunks():
        try:
            key = f"{chunk_usd._resolve_chunk_root(c)}/{c.name}"
        except Exception as exc:  # noqa: BLE001
            _line(getattr(c, "name", c), f"<could not resolve root: {exc}>")
            continue
        chunk_paths[key] = c
        registered = chunk_usd.ChunkStageSync()._chunk_prim_paths.get(id(c))
        flag = "" if registered == key else f"   <-- sync says {registered!r}"
        _line(key, f"ctx={getattr(c.get_context(), 'name', None)}{flag}")

    print("\n=== the pairing ===")
    orphan_prims = [p for p in prim_paths if p not in chunk_paths]
    orphan_chunks = [p for p in chunk_paths if p not in prim_paths]
    for p in orphan_prims:
        _line("PRIM with no live chunk", f"{p}   <-- graph SKIPS this, no node")
    for p in orphan_chunks:
        _line("chunk with no prim", p)
    if not orphan_prims and not orphan_chunks:
        print("  every prim pairs with a chunk -- the mismatch is NOT the cause")

    print("\n=== what the graph currently shows ===")
    try:
        from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker  # noqa: F401
        import shiboken6
        from PySide6 import QtWidgets

        for w in QtWidgets.QApplication.allWidgets():
            if type(w).__name__ != "ChunkGraphView" or not shiboken6.isValid(w):
                continue
            nodes = getattr(w.scene(), "_nodes", {})
            _line("graph view nodes", sorted(nodes))
            missing = [p for p in prim_paths if p not in nodes]
            if missing:
                _line("prims with NO node", missing)
            break
        else:
            print("  no graph view found (is the node graph tab open?)")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not inspect the graph: {exc}")

    print()


diagnose_redo()
