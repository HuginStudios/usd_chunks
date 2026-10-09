"""One-time setup of usd_chunks for Houdini. Run with the hython of the Houdini to set up::

    hython usd_chunks/hosts/houdini/install.py [--chunk-dirs DIR ...]

1. Installs whichever of attrs and typing-extensions -- usd_chunks's two pure-Python
   dependencies -- Houdini's Python cannot import (Houdini 21 has attrs, not
   typing-extensions) into the dependency dir, where pythonrc.py finds them.
   Not into Houdini's own Python, which every plugin shares and an upgrade replaces.
2. Writes a Houdini package file, usd_chunks.json, into this Houdini's user packages folder,
   pointing HOUDINI_PATH at this directory and PYTHONPATH at the usd_chunks checkout.

Run it again after moving the checkout or to change the chunk folders.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import subprocess
import sys

import hou

HERE = os.path.dirname(os.path.abspath(__file__))
#: The folder holding the usd_chunks package: a checkout's root, or site-packages.
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
DEPENDENCIES = ["attrs>=25.4.0", "typing-extensions"]


def dependency_dir() -> str:
    """What pythonrc._dependency_dir resolves to: one folder per Python version."""
    base = os.environ.get("USD_CHUNK_DEPENDENCY_DIR")
    if not base:
        if sys.platform == "win32":
            root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        else:
            root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        base = os.path.join(root, "usd_chunks", "installed_deps")
    return os.path.join(base, f"py{sys.version_info[0]}{sys.version_info[1]}")


def install_dependencies() -> None:
    """Install whichever of DEPENDENCIES Houdini's Python cannot import yet.

    Only the missing ones, and never --upgrade: the dependency dir is shared with every
    other host on this Python version and with chunk packages, and pip --target --upgrade
    replaces a package's folder outright.
    """
    target = dependency_dir()
    if target not in sys.path:
        sys.path.append(target)
    missing = [req for req, module in zip(DEPENDENCIES, ("attr", "typing_extensions"))
               if importlib.util.find_spec(module) is None]
    if not missing:
        print("usd_chunks: dependencies already importable")
        return
    os.makedirs(target, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--target", target, *missing], check=True)


def package(chunk_dirs: list[str]) -> dict:
    env = [{"PYTHONPATH": {"method": "prepend", "value": PACKAGE_ROOT.replace("\\", "/")}}]
    if chunk_dirs:
        env.append({"USD_CHUNK_DIRS": {
            "method": "prepend", "value": os.pathsep.join(d.replace("\\", "/") for d in chunk_dirs)}})
    return {"load_package_once": True, "env": env, "hpath": HERE.replace("\\", "/")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    checkout_chunks = os.path.join(PACKAGE_ROOT, "chunks")
    parser.add_argument(
        "--chunk-dirs", nargs="*",
        default=[checkout_chunks] if os.path.isdir(checkout_chunks) else [],
        help="Chunk folders for USD_CHUNK_DIRS (default: the checkout's chunks/, if any)")
    parser.add_argument("--no-deps", action="store_true", help="Skip installing dependencies")
    parser.add_argument(
        "--deps-only", action="store_true",
        help="Only install dependencies, no package file (a launcher sets the environment itself)")
    args = parser.parse_args()

    if not args.no_deps:
        install_dependencies()
    if args.deps_only:
        return
    packages = os.path.join(hou.homeHoudiniDirectory(), "packages")
    os.makedirs(packages, exist_ok=True)
    path = os.path.join(packages, "usd_chunks.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(package([os.path.abspath(d) for d in args.chunk_dirs]), f, indent=4)
    print(f"usd_chunks: wrote {path}. Restart Houdini; chunks are under TAB > usdchunks in LOPs.")


if __name__ == "__main__":
    main()
