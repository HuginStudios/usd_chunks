"""Build the one-file Blender add-on: an extension zip that carries usd_chunks as a wheel.

    python tools/build_blender_addon.py --out dist [--version v0.2.0]

A plain script, not part of the package: importing usd_chunks needs OpenUSD, and building
the zip must not.

Makes `dist/usd_chunks_blender-<version>.zip`. In Blender: Edit > Preferences > Add-ons >
Install from Disk, and it works -- no pip, no `--target`, no USD_CHUNK_DIRS. The release
workflow (.github/workflows/blender-addon.yml) runs this on every `v*` tag and attaches the
zip to the release.

**What is in the zip.** The add-on's manifest with a `wheels` entry, a small `__init__.py`,
and `wheels/usd_chunks-*.whl` -- the package built from this checkout, chunks/ included
(setup.py copies it in). Blender installs the wheel into the extension's own site-packages
when the zip is installed, and removes it with the extension.

**Why a shim and not the add-on folder.** The add-on package also lives inside the wheel,
as `usd_chunks.frontends.blender.addon`, and its submodules import it under that name. Had
the zip carried the folder too, Blender would load it a second time as
`bl_ext.<repo>.usd_chunks`: two copies of the module, each with its own globals, and only
one of them registered. The shim makes the wheel's copy the only one.

**What is not in it.** OpenUSD -- Blender ships its own, and a second one breaks it (see the
add-on's manifest). attrs and typing-extensions -- Blender ships both. Nothing a chunk
needs: a chunk asks for its own packages when it is used, as before.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "usd_chunks" / "frontends" / "blender" / "addon" / "blender_manifest.toml"

SHIM = '''"""USDChunks Editor, as installed from the release zip.

The add-on itself is `usd_chunks.frontends.blender.addon`, in the usd_chunks wheel this
extension carries. This module only hands Blender's register/unregister to it, so there is
one copy of the add-on's state, not two.
"""
from __future__ import annotations


def register():
    from usd_chunks.frontends.blender import addon
    addon.register()


def unregister():
    from usd_chunks.frontends.blender import addon
    addon.unregister()
'''


def release_version(tag: str) -> str:
    """The extension version for a git tag: `v0.2.0` -> `0.2.0`.

    Blender wants semver, so anything else is refused here rather than at install.
    """
    version = tag.strip().removeprefix("refs/tags/").lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise ValueError(f"{tag!r} is not a version tag like v0.2.0")
    return version


def manifest_text(version: str, wheel_name: str, source: str | None = None) -> str:
    """The add-on's manifest with this version and the wheel listed."""
    text = MANIFEST.read_text(encoding="utf-8") if source is None else source
    text, found = re.subn(r'(?m)^version = ".*"$', f'version = "{version}"', text, count=1)
    if not found:
        raise ValueError("the manifest has no version line")
    wheels = f'wheels = ["./wheels/{wheel_name}"]'
    text, found = re.subn(r"(?m)^# wheels = .*$", wheels, text, count=1)
    if not found:
        # Above [permissions]: a key after a table header would belong to that table.
        text = text.replace("\n[permissions]", f"\n{wheels}\n\n[permissions]", 1)
    return text


def build_wheel(out_dir: Path, repo: Path = REPO) -> Path:
    """Build the usd_chunks wheel from `repo` into `out_dir`. No dependencies with it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir",
                    str(out_dir), str(repo)], check=True)
    wheels = sorted(out_dir.glob("usd_chunks-*.whl"))
    if not wheels:
        raise RuntimeError(f"pip made no usd_chunks wheel in {out_dir}")
    return wheels[-1]


#: Wheel entries Blender has no use for. Houdini's HDA is a folder tree whose paths are deep
#: enough that, under a long Windows user folder, extracting it passes MAX_PATH and the whole
#: install fails half-way.
NOT_FOR_BLENDER = ("usd_chunks/hosts/houdini/",)


def slim_wheel(wheel: Path, out_dir: Path) -> Path:
    """A copy of `wheel` without NOT_FOR_BLENDER, its RECORD rewritten to match."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / wheel.name
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            name = item.filename
            if name.startswith(NOT_FOR_BLENDER):
                continue
            data = src.read(item)
            if name.endswith(".dist-info/RECORD"):
                lines = data.decode("utf-8").splitlines(keepends=True)
                data = "".join(line for line in lines
                               if not line.startswith(NOT_FOR_BLENDER)).encode("utf-8")
            dst.writestr(item, data)
    return target


def build_zip(wheel: Path, version: str, out_dir: Path) -> Path:
    """The extension zip: manifest, shim and the (slimmed) wheel. Returns its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"usd_chunks_blender-{version}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        slim = slim_wheel(wheel, Path(tmp))
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("blender_manifest.toml", manifest_text(version, slim.name))
            z.writestr("__init__.py", SHIM)
            z.write(slim, f"wheels/{slim.name}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=REPO / "dist", help="where the zip goes")
    parser.add_argument("--version", help="a tag like v0.2.0; default: the manifest's version")
    parser.add_argument("--wheel", type=Path, help="use this wheel instead of building one")
    args = parser.parse_args(argv)
    if args.version:
        version = release_version(args.version)
    else:
        version = re.search(r'(?m)^version = "(.*)"$', MANIFEST.read_text(encoding="utf-8"))[1]
    if args.wheel:
        wheel = args.wheel
    else:
        wheel = build_wheel(Path(tempfile.mkdtemp(prefix="usd_chunks_wheel_")))
    print(build_zip(wheel, version, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
