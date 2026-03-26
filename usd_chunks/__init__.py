from importlib.metadata import PackageNotFoundError, version
import sys
import logging
from pathlib import Path
from typing import Final

try:
    __version__ = version("usd_chunks")
except PackageNotFoundError:
    __version__ = "uninstalled"
__author__ = "Viktor Lanner"
__email__ = "viktor.lanner@huginstudios.com"

_logger = logging.getLogger("usd_chunks")
_logger.setLevel(logging.INFO)

# create console handler and set level to debug
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)

formatter = logging.Formatter("%(levelname)s: %(asctime)s - %(name)s: %(message)s", datefmt="%y-%m-%d %H:%M")
ch.setFormatter(formatter)
_logger.addHandler(ch)

# Attempt to import nesessary deps. If not found, use the installed packages from .venv
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
