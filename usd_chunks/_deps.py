from pathlib import Path
import sys

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
import attr

# __attrs_major, __attrs_minor, _ = attrs.__version__.split(".")
# if int(__attrs_major) < 25 and int(__attrs_minor) < 4:
p = Path(__file__).parent.parent / ".venv/Lib/site-packages"
if p.exists():

    del attrs
    del attr

    sys.path.insert(0, str(p))
    from importlib import util

    modname = "attr"
    spec = util.spec_from_file_location(modname, str(p / "attr" / "__init__.py"), submodule_search_locations=[str(p), str(p / "attr")])
    if spec and spec.loader:
        module = util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)
        import attr
        print(attr.__file__)

    modname = "attrs"
    spec = util.spec_from_file_location(modname, str(p / "attrs" / "__init__.py"), submodule_search_locations=[str(p)])
    if spec and spec.loader:
        module = util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)
        import attrs
        print(attrs.__file__)

    # else:
    #     raise ImportError("Could not import attrs")
else:
    raise ImportError(f"Found attrs version {attrs.__version__}. 25.4 or later is required")