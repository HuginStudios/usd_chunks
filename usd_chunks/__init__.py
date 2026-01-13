from importlib.metadata import PackageNotFoundError, version
import sys
from pathlib import Path

try:
    __version__ = version("usd_chunks")
except PackageNotFoundError:
    __version__ = "uninstalled"
__author__ = "Viktor Lanner"
__email__ = "viktor.lanner@huginstudios.com"

try:
    import attrs  # noqa: F401
except ImportError:
    p = Path(__file__).parent.parent / ".venv/Lib/site-packages"
    if p.exists():
        sys.path.append(str(p))
