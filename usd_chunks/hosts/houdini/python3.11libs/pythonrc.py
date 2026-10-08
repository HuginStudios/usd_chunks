"""Houdini startup for usd_chunks: dependencies on sys.path, then the TAB menu.

Houdini runs every pythonrc.py on HOUDINI_PATH, so this one adds to startup rather than
replacing anything of SideFX's (a uiready.py here would shadow Houdini's own).

typing_extensions is not in Houdini's Python (attrs is, as 22.2). install.py puts what is
missing in the dependency dir -- the same per-Python-version folder chunk packages install into -- which
is spelled out here because usd_chunks itself cannot be imported until it is on sys.path.
The usd_chunks imports therefore come after that, and are guarded: a missing dependency
must say what to do, not stop Houdini's startup.
"""
from __future__ import annotations
import os
import sys

import hou


def _dependency_dir() -> str:
    base = os.environ.get("USD_CHUNK_DEPENDENCY_DIR")
    if not base:
        if sys.platform == "win32":
            root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        else:
            root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        base = os.path.join(root, "usd_chunks", "installed_deps")
    return os.path.join(base, f"py{sys.version_info[0]}{sys.version_info[1]}")


def _houdini_python() -> str | None:
    """Houdini's own plain Python interpreter -- the one sys.executable is not.

    In Houdini sys.executable is houdini.exe (hython in hython), and usd_chunks starts its
    subprocesses (pip, sim bake workers, sweeps) with `<python> -m ...`: with houdini.exe
    each one was a whole new Houdini, whose startup could start the next. This interpreter
    shares Houdini's site-packages, imports Houdini's pxr given $HFS/bin on PATH, and needs
    no licence or UI. See usd_chunks.chunk_process.
    """
    hfs = os.environ.get("HFS", "")
    v = sys.version_info
    if sys.platform == "win32":
        path = os.path.join(hfs, f"python{v[0]}{v[1]}", "python.exe")
    else:
        path = os.path.join(hfs, "python", "bin", f"python{v[0]}.{v[1]}")
    return path if hfs and os.path.isfile(path) else None


# Before usd_chunks is imported, so nothing it starts is ever houdini.exe. $HFS/bin goes
# on PATH (for those child processes only -- Houdini itself is already running) so the
# plain interpreter finds the DLLs Houdini's pxr needs.
if not os.environ.get("USD_CHUNK_PYTHON") and (_python := _houdini_python()):
    os.environ["USD_CHUNK_PYTHON"] = _python
    _bin = os.path.join(os.environ["HFS"], "bin")
    if _bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")

_deps = _dependency_dir()
if os.path.isdir(_deps) and _deps not in sys.path:
    # Appended: whatever Houdini ships wins over a copy in the dependency dir.
    sys.path.append(_deps)

try:
    from usd_chunks import configure_logging
    from usd_chunks.hosts.houdini import tools
except ImportError as _exc:
    print(f"usd_chunks: not loaded ({_exc}). Run usd_chunks/hosts/houdini/install.py "
          "with hython once; see usd_chunks/hosts/houdini/README.md.")
else:
    configure_logging()
    if hou.isUIAvailable():
        import hdefereval  # UI-only module, so only importable on this branch

        hdefereval.executeDeferred(tools.install_tab_menu)
