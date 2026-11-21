"""Chunk folders added from inside Blender, on top of the ones USD_CHUNK_DIRS names.

Blender started from a launcher gets USD_CHUNK_DIRS from it; Blender started any other way
has only the folders the add-on is given -- the ``Chunk Folders`` preference and the
"Add Chunk Folder" button, which writes to it. A folder added here goes on the end of
USD_CHUNK_DIRS for this process, so the folders the environment named keep precedence on a
duplicate chunk name, and a registry that starts later (the next network opened) scans it
too. A registry already running scans it at once. No ``bpy`` here, so this is tested
outside Blender.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from usd_chunks import chunk_registry

logger = logging.getLogger(__name__)

ENV_VAR = chunk_registry.USD_CHUNK_DIRS.key


def _key(folder: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(folder)))


def join_folder(text: str, folder: str) -> str:
    """`text` (a ';'-separated folder list) with `folder` on the end, unless it is there."""
    parts = [p.strip().strip('"') for p in text.replace(os.pathsep, ";").split(";")] if text else []
    parts = [p for p in parts if p]
    if _key(folder) not in {_key(p) for p in parts}:
        parts.append(os.path.normpath(folder))
    return ";".join(parts)


def add_folders(folders, registry=None, environ=None) -> list[str]:
    """Put chunk `folders` on USD_CHUNK_DIRS and, if the registry is running, scan them.

    Folders already on USD_CHUNK_DIRS are skipped, and missing ones with a warning, so
    calling this again with the same list does nothing.

    Args:
        folders: Folders of chunk modules
        registry: The chunk registry; the running ChunkRegistry when None
        environ: The environment to extend; os.environ when None

    Returns:
        list[str]: The folders added by this call, absolute
    """
    environ = os.environ if environ is None else environ
    registry = registry if registry is not None else chunk_registry.ChunkRegistry()
    current = [p for p in environ.get(ENV_VAR, "").split(os.pathsep) if p]
    known = {_key(p) for p in current}
    added = []
    for folder in folders:
        path = os.path.normpath(os.path.abspath(folder))
        if _key(path) in known:
            continue
        if not os.path.isdir(path):
            logger.warning("chunk folder %s not found; skipped", path)
            continue
        known.add(_key(path))
        current.append(path)
        added.append(path)
    if not added:
        return added
    environ[ENV_VAR] = os.pathsep.join(current)
    if getattr(registry, "initialized", False):
        for path in added:
            registry.register_directory(path)
        registry.register_chunk_modules([Path(p) for p in added])
    logger.info("chunk folders added: %s", ", ".join(added))
    return added
