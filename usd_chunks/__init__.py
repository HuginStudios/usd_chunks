from importlib.metadata import PackageNotFoundError, version
# import sys
# from pathlib import Path

try:
    __version__ = version("usd_chunks")
except PackageNotFoundError:
    __version__ = "uninstalled"
__author__ = "Viktor Lanner"
__email__ = "viktor.lanner@huginstudios.com"


from . import _deps

# try:
#     import attrs  # noqa: F401
#     import typing_extensions  # noqa: F401
# except ImportError as e:
#     p = Path(__file__).parent.parent / ".venv/Lib/site-packages"
#     if p.exists():
#         sys.path.append(str(p))
#     else:
#         raise e

# finally:
#     import attrs

#     major, minor, build = attrs.__version__.split(".")

#     if int(major) < 25 and int(minor) < 4:
#         p = Path(__file__).parent.parent / ".venv/Lib/site-packages"
#         if p.exists():
#             sys.path.insert(0, str(p))
#         else:
#             raise ImportError(f"Found attrs version {attrs.__version__}. 25.4 or later is required")
