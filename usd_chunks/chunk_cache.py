"""On-disk cache of which pip packages are already installed.

This is a pure optimisation: it saves a `pip list` on startup, and nothing else depends
on it being right. `ChunkDependencyManager.update_cache` rebuilds it from the environment
whenever a lookup misses, so the correct response to a cache we cannot read is to throw it
away, not to try harder.

The file is plain JSON: each package name maps to a list of installed versions.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import zlib
from pathlib import Path

logger = logging.getLogger(__name__)


def user_cache_dir(*parts: str) -> str:
    """A per-user folder for what usd_chunks keeps between runs, joined with parts.

    %LOCALAPPDATA%/usd_chunks on Windows, $XDG_CACHE_HOME (else ~/.cache)/usd_chunks
    elsewhere. Never beside the package: installed, that is site-packages, often read-only
    and shared. Not created here; whoever writes into it does that.

    Args:
        *parts: Subfolders and/or a file name inside the usd_chunks folder.

    Returns:
        The path.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "usd_chunks", *parts)


def compute_checksum(obj: str) -> int:
    """The adler32 checksum of a string.

    Used to key the cache file to the interpreter that wrote it -- two Pythons on one
    machine have different site-packages and must not share a cache.

    Args:
        obj: The string to checksum.

    Returns:
        The checksum.
    """
    return zlib.adler32(obj.encode())


def read_package_cache(file: str | Path) -> dict[str, set[str]]:
    """Read a cache file, or return an empty cache if it cannot be read.

    Anything unexpected -- missing, truncated, or not the expected shape -- is treated as
    "no cache". The caller rebuilds from `pip list`, so a bad file costs one subprocess call
    and nothing else.

    Args:
        file: The cache file.

    Returns:
        {package: installed versions}.
    """
    path = Path(file)
    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        logger.debug("Ignoring unreadable package cache %s: %s", path, exc)
        return {}

    if not isinstance(raw, dict):
        logger.debug("Ignoring package cache %s: expected an object, got %s", path, type(raw).__name__)
        return {}

    cache: dict[str, set[str]] = {}
    for name, versions in raw.items():
        if not isinstance(versions, list):
            return {}
        cache[name] = {v for v in versions if v is None or isinstance(v, str)}
    return cache


def write_package_cache(file: str | Path, cache: dict[str, set[str]]) -> None:
    """Write the cache, creating the directory if it is not there yet."""
    path = Path(file)
    os.makedirs(path.parent, exist_ok=True)

    # Sorted so the file is stable between runs and diffs cleanly when someone looks at it.
    # None is a real entry -- a package whose version could not be determined -- and json
    # cannot sort it against strings, so key on the empty string for ordering only.
    plain = {name: sorted(cache[name], key=lambda v: v or "") for name in sorted(cache)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plain, f, indent=2)

    logger.debug("Saved file %s", path)
