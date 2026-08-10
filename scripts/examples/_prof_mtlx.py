"""Profile creating a MaterialX node inside the running app.

Paste into usdview's interpreter (Window > Interpreter):

    exec(open(r"C:\\Users\\danen\\hugin\\usd_chunks\\scripts\\examples\\_prof_mtlx.py").read())

Creates a Standard Surface exactly as the chunk menu does, under cProfile, and prints
where the time actually goes -- including the Qt work an offscreen run never performs
(proxy widget, port placement from widget geometry, real painting).
"""

import cProfile
import io
import pstats
import time

from usd_chunks import chunk_registry

ENTRY = "pbr/standard_surface"


def profile(entry_key=ENTRY, top=30):
    reg = chunk_registry.ChunkRegistry()
    scene = reg.scene

    entries = {
        "/".join(e.namespace[1:]): e
        for e in reg.get_menu_entries()
        if e.module.module_name == "mtlx_shader" and len(e.namespace) > 2
    }
    entry = entries.get(entry_key)
    if entry is None:
        print("no such entry:", entry_key)
        print("available:", sorted(entries)[:10], "...")
        return

    # Warm the shader catalogue so the 784-definition walk is not counted as node cost.
    # Not added to the scene: _build_edges walks every node in the graph, so a node left
    # behind makes the next run measurably slower and successive runs incomparable.
    entry.create()

    before = len(list(scene.get_chunks()))

    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()

    chunks = entry.create()
    for c in chunks:
        scene.add_chunk(c)

    pr.disable()
    elapsed = time.perf_counter() - t0

    print(f"created {entry_key} in {elapsed:.3f}s   (scene had {before} chunks)")

    # Leave the scene as we found it, so repeated runs stay comparable.
    for c in chunks:
        try:
            c.request_delete()
        except Exception as e:  # noqa: BLE001 - cleanup must not mask the measurement
            print("  cleanup failed:", e)

    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(top)
    for line in s.getvalue().splitlines():
        if line.strip():
            print(line)


def profile_expand(entry_key=ENTRY, top=30, settle_s=1.0):
    """Profile expanding one collapsed group on a freshly created shader node.

    The work an expand triggers is timer-driven -- the height animation and the deferred
    port re-placement both run after the click returns -- so the event loop is pumped
    inside the profiled region. Profiling the toggle alone measures almost nothing, which
    is why guessing at this from the create profile kept going wrong.
    """
    from PySide6 import QtCore, QtWidgets

    from usd_chunks.plugins.chunk_qt_widgets import ContainerWidget

    reg = chunk_registry.ChunkRegistry()
    scene = reg.scene

    entries = {
        "/".join(e.namespace[1:]): e
        for e in reg.get_menu_entries()
        if e.module.module_name == "mtlx_shader" and len(e.namespace) > 2
    }
    entry = entries.get(entry_key)
    if entry is None:
        print("no such entry:", entry_key)
        return

    chunks = entry.create()
    for c in chunks:
        scene.add_chunk(c)
    node = chunks[0]

    app = QtWidgets.QApplication.instance()
    for _ in range(20):
        app.processEvents()

    # Every ContainerWidget built for one of this chunk's groups, collapsed, with content.
    names = {p.name for p in node.get_my_params() if hasattr(p, "add_param")}
    targets = [
        w for w in app.allWidgets()
        if isinstance(w, ContainerWidget)
        and getattr(getattr(w, "group", None), "name", None) in names
        and w.is_collapsed()
    ]
    if not targets:
        print("no collapsed group widget found for", node.name)
        for c in chunks:
            c.request_delete()
        return

    target = targets[0]
    print(f"expanding group {target.group.name!r} on {node.name}")

    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()

    target.set_expanded(True)
    # Pump with a wait rather than spinning: a bare processEvents loop calls it a quarter
    # of a million times and shows up as the single largest cost in its own profile.
    end = time.perf_counter() + settle_s
    while time.perf_counter() < end:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 10)

    pr.disable()
    print(f"expand + {settle_s}s settle took {time.perf_counter() - t0:.3f}s")

    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(top)
    for line in s.getvalue().splitlines():
        if line.strip():
            print(line)

    for c in chunks:
        try:
            c.request_delete()
        except Exception as e:  # noqa: BLE001
            print("  cleanup failed:", e)


profile()
