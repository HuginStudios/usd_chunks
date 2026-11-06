"""usd_chunks: interactive UI panels ("chunks") for OpenUSD, built from plain Python modules.

Importing the package checks that the OpenUSD bindings are importable, sets the
``usd_chunks`` logger's level (from ``USD_CHUNK_LOG_LEVEL``), and checks the attrs version.
It adds no console handler; entry points call ``configure_logging()`` for that.
"""
from __future__ import annotations
from importlib.metadata import PackageNotFoundError, version
import os
import logging
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

# Old module names (usd_chunks.plugins.*, chunk_graph, chunk_gizmo, ...) keep importing as
# aliases of where those modules live now. Stdlib only, so it costs nothing here.
from usd_chunks import _moved

_moved.install()

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

# A library leaves output to whoever runs it: importing adds no console handler, so a host
# that configures logging itself does not print every message twice. Entry points (the CLI,
# the usdview plugin, the Blender add-on, the graph frontends) call configure_logging().
_logger.addHandler(logging.NullHandler())
_console_handler: logging.Handler | None = None


def configure_logging(level: int = logging.DEBUG) -> None:
    """Send ``usd_chunks`` log records to the console, unless the host already logs somewhere.

    Called by the entry points, not on import. Idempotent. Skipped when the root logger has a
    handler: records propagate there, and a second handler would print each one twice.

    Args:
        level: The least severe record printed. A script Scene prints warnings and up.
    """
    global _console_handler
    if _console_handler is not None or logging.getLogger().handlers:
        return
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(level)
    _console_handler.setFormatter(logging.Formatter(
        "%(levelname)s: %(asctime)s - %(name)s: %(message)s", datefmt="%y-%m-%d %H:%M"
    ))
    _logger.addHandler(_console_handler)


import attrs  # noqa: E402

ATTRS_VERSION: Final = attrs.__version_info__
if ATTRS_VERSION < (25, 4):  # type: ignore
    _logger.warning(
        "Found unsupported attrs version %s. Some functionality may not work. "
        "Consider updating to 25.4", attrs.__version__,
    )

# The scripting entry point: `Scene.new()` for a script of its own, `Scene.current()` for the
# one a host is already running (usdview's). `current_scene` stays for older scripts. Last,
# so everything above is set up before the chunk model they pull in is imported.
from usd_chunks.chunk_scene import current_scene as current_scene  # noqa: E402
from usd_chunks.chunk_script import Scene as Scene  # noqa: E402
