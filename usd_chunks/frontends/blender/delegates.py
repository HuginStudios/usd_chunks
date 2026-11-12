"""Hydra render delegates beyond Storm, for the add-on's live view in Blender.

Blender's USD finds render delegates the way any USD does: through the plugin registry. A
delegate built against Blender's own USD (its monolithic ``usd_ms``, under Blender's private
namespace) loads like Storm, and then shows in the viewport's Renderer menu. One built for
another USD does not: it would bring a second USD into the process.

Blender started from a launcher can put the delegate on ``PXR_PLUGINPATH_NAME`` and ``PATH``
itself. Blender started any other way has neither, so the add-on registers the folders it is
given -- the ``Hydra Delegate Folders`` preference and ``USD_CHUNKS_HYDRA_DELEGATES`` -- once,
when it starts. No ``bpy`` here, so this is tested outside Blender.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from pxr import Plug

logger = logging.getLogger(__name__)

#: os.pathsep-separated delegate folders, read when the add-on registers.
ENV_VAR = "USD_CHUNKS_HYDRA_DELEGATES"

_dll_handles: list = []  # os.add_dll_directory handles: dropping one removes the directory
_registered: set[str] = set()


def split_folders(text: str) -> list[str]:
    """The folders in a preference or environment value: separated by ';' or os.pathsep."""
    parts = text.replace(";", os.pathsep).split(os.pathsep) if text else []
    return [p.strip().strip('"') for p in parts if p.strip().strip('"')]


def plug_info_dir(folder: str) -> Path | None:
    """The directory holding a delegate's plugInfo.json: the folder itself or its resources/."""
    root = Path(folder)
    for candidate in (root, root / "resources"):
        if (candidate / "plugInfo.json").is_file():
            return candidate
    return None


def dll_dirs(info_dir: Path) -> list[Path]:
    """Where the delegate's DLL and its dependencies are looked for.

    A delegate's own DLLs (Aurora: aurora.dll, slang.dll, ...) sit beside its library, which
    plugInfo.json usually reaches as ``..`` from a resources/ folder. Windows looks for a
    library's dependencies on the search path, not beside the library, so both go on it.
    """
    dirs = [info_dir]
    if info_dir.name == "resources":
        dirs.append(info_dir.parent)
    return dirs


def register_folders(folders, registry=None) -> list[str]:
    """Register the Hydra delegate plugins in `folders` with USD's plugin registry.

    Folders already registered, missing, or without a plugInfo.json are skipped (the last
    two with a warning), so calling this again with the same list does nothing.

    Args:
        folders: Delegate folders: each holds plugInfo.json, or a resources/ folder that does
        registry: The plugin registry; USD's own when None

    Returns:
        list[str]: The plugInfo directories registered by this call
    """
    registry = registry if registry is not None else Plug.Registry()
    infos = []
    for folder in folders:
        info = plug_info_dir(folder)
        if info is None:
            logger.warning("Hydra delegate folder %s has no plugInfo.json; skipped", folder)
            continue
        key = str(info.resolve())
        if key in _registered:
            continue
        for d in dll_dirs(info):
            if hasattr(os, "add_dll_directory"):
                _dll_handles.append(os.add_dll_directory(str(d)))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        _registered.add(key)
        infos.append(key)
    if infos:
        registry.RegisterPlugins(infos)
        logger.info("Hydra delegates registered from %s", ", ".join(infos))
    return infos
