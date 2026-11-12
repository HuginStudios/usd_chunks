"""tools/build_blender_addon.py: the one-file Blender add-on the release workflow ships."""
from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "build_blender_addon.py"


@pytest.fixture(scope="module")
def build():
    spec = importlib.util.spec_from_file_location("build_blender_addon", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_wheel(path: Path) -> Path:
    """A wheel-shaped zip with a Houdini HDA entry, a Blender file and a RECORD."""
    wheel = path / "usd_chunks-0.1.0-py3-none-any.whl"
    entries = {
        "usd_chunks/__init__.py": "",
        "usd_chunks/frontends/blender/addon/__init__.py": "def register(): pass\n",
        "usd_chunks/hosts/houdini/otls/x.hda/Sections.list": "",
        "usd_chunks/chunks/core/a.py": "",
    }
    record = "".join(f"{name},," + "\n" for name in entries)
    record += "usd_chunks-0.1.0.dist-info/RECORD,,\n"
    with zipfile.ZipFile(wheel, "w") as z:
        for name, text in entries.items():
            z.writestr(name, text)
        z.writestr("usd_chunks-0.1.0.dist-info/RECORD", record)
    return wheel


def test_a_tag_names_the_version(build):
    assert build.release_version("v0.2.0") == "0.2.0"
    assert build.release_version("refs/tags/v1.10.3") == "1.10.3"
    assert build.release_version("0.3.0-rc.1") == "0.3.0-rc.1"
    with pytest.raises(ValueError):
        build.release_version("nightly")


def test_the_manifest_lists_the_wheel_and_takes_the_version(build):
    text = build.manifest_text("0.4.0", "usd_chunks-0.1.0-py3-none-any.whl")
    assert 'version = "0.4.0"' in text
    assert 'wheels = ["./wheels/usd_chunks-0.1.0-py3-none-any.whl"]' in text
    # A key below [permissions] would belong to that table, and Blender would not see it.
    assert text.index("wheels = ") < text.index("[permissions]")


def test_a_manifest_without_the_comment_still_gets_the_wheel(build):
    source = 'id = "usd_chunks"\nversion = "0.1.0"\n\n[permissions]\nfiles = "x"\n'
    text = build.manifest_text("0.2.0", "w.whl", source=source)
    assert text.index('wheels = ["./wheels/w.whl"]') < text.index("[permissions]")


def test_the_zip_is_the_manifest_the_shim_and_a_slim_wheel(build, tmp_path):
    out = build.build_zip(_fake_wheel(tmp_path), "0.2.0", tmp_path / "dist")
    assert out.name == "usd_chunks_blender-0.2.0.zip"
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == [
            "__init__.py", "blender_manifest.toml", "wheels/usd_chunks-0.1.0-py3-none-any.whl"]
        shim = z.read("__init__.py").decode()
        wheel = tmp_path / "inner.whl"
        wheel.write_bytes(z.read("wheels/usd_chunks-0.1.0-py3-none-any.whl"))
    # The shim hands over to the wheel's add-on rather than carrying a second copy.
    assert "from usd_chunks.frontends.blender import addon" in shim
    with zipfile.ZipFile(wheel) as w:
        names = w.namelist()
        record = w.read("usd_chunks-0.1.0.dist-info/RECORD").decode()
    assert not any(n.startswith("usd_chunks/hosts/houdini/") for n in names)
    assert "usd_chunks/chunks/core/a.py" in names
    assert "houdini" not in record and "usd_chunks/chunks/core/a.py" in record
