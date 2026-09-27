"""usd_chunks: interactive UI panels ("chunks") for OpenUSD, built from plain Python modules.

Importing the package checks that the OpenUSD bindings are importable, sets up the
``usd_chunks`` logger (its level comes from ``USD_CHUNK_LOG_LEVEL``), and checks the attrs
version.
"""
from __future__ import annotations
from importlib.metadata import PackageNotFoundError, version
import os
import sys
import logging
from pathlib import Path
from typing import Final

try:
    import pxr  # noqa: F401
except ImportError as _exc:  # pragma: no cover - depends on the install, not the code
    # usd-core is an optional extra rather than a dependency, so this is the one place
    # that has to explain why. Inside a DCC the bindings are already present, and
    # installing usd-core there would add a SECOND, non-interoperable USD (hosts namespace
    # their builds -- Blender's is pxrBlender_v26_03__pxrReserved__), so the fix genuinely
    # differs by where you are running.
    raise ImportError(
        "usd_chunks needs the OpenUSD Python bindings (pxr), which are not importable.\n"
        "  Inside a DCC that ships USD (usdview, Houdini, Maya, Blender): they are "
        "already there -- run usd_chunks under the host's interpreter rather than "
        "installing usd-core, which would add a second, incompatible USD.\n"
        "  Standalone: pip install 'usd-chunks[standalone]'"
    ) from _exc

try:
    __version__ = version("usd_chunks")
except PackageNotFoundError:
    __version__ = "uninstalled"
__author__ = "Hugin Studios"
__email__ = "info@huginstudios.com"

_logger = logging.getLogger("usd_chunks")
# USD_CHUNK_LOG_LEVEL=DEBUG (before launching usdview) surfaces DEBUG-level messages
# that are filtered out at the default INFO level -- e.g. to see what a reload is
# actually doing param-by-param. An unrecognized value is left at INFO rather than
# raising, since a typo here shouldn't stop usdview from starting.
_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_logger.setLevel(_LOG_LEVELS.get(os.environ.get("USD_CHUNK_LOG_LEVEL", "").upper(), logging.INFO))

# create console handler and set level to debug
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)

formatter = logging.Formatter("%(levelname)s: %(asctime)s - %(name)s: %(message)s", datefmt="%y-%m-%d %H:%M")
ch.setFormatter(formatter)
_logger.addHandler(ch)

# Attempt to import necessary deps. If not found, use the installed packages from .venv
try:
    import attrs  # noqa: F401
    import typing_extensions  # noqa: F401
except ImportError as e:
    p = Path(__file__).parent.parent / ".venv/Lib/site-packages"
    if p.exists():
        sys.path.append(str(p))
    else:
        raise e

    import attrs

ATTRS_VERSION: Final = attrs.__version_info__
if ATTRS_VERSION < (25, 4):  # type: ignore
    print(
        "Found unsupported attrs version:",
        attrs.__version__,
        "Some functionality may not work. Consider updating to 25.4",
    )
