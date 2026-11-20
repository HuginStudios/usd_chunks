"""Poly Haven's free models (https://polyhaven.com, CC0): the catalog, downloads, and their looks.

The Poly Haven Model chunk and the styles that use it (usd_chunks/styles.py) bring real
assets in through here: rocks, plants, branches, crates. A model is fetched once, as USD with
its textures, into a per-user folder (USD_CHUNK_POLYHAVEN_DIR, else the usd_chunks cache), and
read from there afterwards. Nothing here touches a stage the caller has open: the chunk does
the authoring, on the main thread, after a download a worker made.

A model's own materials are MaterialX, which renders black in Storm and crashes Aurora, so
look_files() gives the textures each material uses and the chunk builds a UsdPreviewSurface
from them, as examples/scripts/example_assets.py does.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import weakref
from dataclasses import dataclass, field
from pathlib import Path

from pxr import Sdf, Usd, UsdGeom, UsdShade

from usd_chunks import chunk_cache, chunk_scene

API = "https://api.polyhaven.com"
THUMBS = "https://cdn.polyhaven.com/asset_img/thumbs/{name}.png?width=256&height=256"
#: A client name, no contact details: an automated client, not a person.
AGENT = {"User-Agent": "usd_chunks"}
ENV = "USD_CHUNK_POLYHAVEN_DIR"
RESOLUTIONS = ("1k", "2k", "4k")
#: How long the list of models is kept before it is asked for again, in seconds.
CATALOG_AGE = 7 * 24 * 3600


def cache_dir() -> Path:
    """Where models, thumbnails and the catalog are kept: USD_CHUNK_POLYHAVEN_DIR, else the
    usd_chunks per-user cache."""
    return Path(os.environ.get(ENV) or chunk_cache.user_cache_dir("polyhaven"))


def _download(url: str, dest: Path, timeout: float = 60.0) -> Path:
    """url to dest, unless it is there already. Written beside it and moved in when complete,
    so a download cut short is fetched again rather than read half-done."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=timeout) as response, \
            open(part, "wb") as out:
        while block := response.read(1 << 20):
            out.write(block)
    part.replace(dest)
    return dest


def _json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=30) as response:
        return json.load(response)


def catalog(kind: str = "models", refresh: bool = False) -> dict[str, dict]:
    """Every Poly Haven asset of a kind ("models" or "textures"): {id: {"name", "categories",
    ...}}, from the cached list when it is less than a week old. Network on a cold cache; call
    it from a worker."""
    path = cache_dir() / f"{kind}.json"
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < CATALOG_AGE:
        return json.loads(path.read_text(encoding="utf-8"))
    data = _json(f"{API}/assets?t={kind}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def cached_catalog(kind: str = "models") -> dict[str, dict]:
    """The list of assets of a kind if it was fetched before, else {}: never touches the network."""
    path = cache_dir() / f"{kind}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def thumbnail(name: str) -> Path:
    """A model's picture, 256 pixels, fetched once. Network on a cold cache."""
    return _download(THUMBS.format(name=name), cache_dir() / "thumbs" / f"{name}.png", timeout=20)


def model_file(name: str, res: str = "1k") -> Path:
    """Where a model's .usdc is, downloaded or not."""
    return cache_dir() / name / f"{name}_{res}.usdc"


def cached_model(name: str, res: str = "1k") -> Path | None:
    """The model's .usdc if it has been downloaded, else None: never touches the network."""
    path = model_file(name, res)
    return path if path.exists() else None


def download_model(name: str, res: str = "1k") -> Path:
    """A model as USD with its textures, fetched once. Returns the .usdc file.

    The textures first and the .usdc last, so a model that is there has everything it needs.
    Raises when Poly Haven has no such model, or no USD of it at that resolution.
    """
    dest = model_file(name, res)
    if dest.exists():
        return dest
    files = _json(f"{API}/files/{name}")
    entry = (files.get("usd") or {}).get(res, {}).get("usd")
    if entry is None:
        raise ValueError(f"Poly Haven has no USD of {name!r} at {res}")
    for rel, f in entry.get("include", {}).items():
        _download(f["url"], dest.parent / rel)
    return _download(entry["url"], dest)


#: A texture set's images: the key the chunk uses, the map Poly Haven's file list calls it.
TEXTURE_MAPS = (("diffuse", "Diffuse"), ("rough", "Rough"), ("normal", "nor_gl"))


def texture_files(name: str, res: str = "1k") -> dict[str, Path]:
    """Where a texture set's images are, downloaded or not: {"diffuse"|"rough"|"normal": jpg}."""
    return {key: cache_dir() / "textures" / name / f"{name}_{key}_{res}.jpg" for key, _ in TEXTURE_MAPS}


def cached_texture(name: str, res: str = "1k") -> dict[str, Path] | None:
    """The texture set's images if they have been downloaded, else None."""
    files = texture_files(name, res)
    return files if all(f.exists() for f in files.values()) else None


def download_texture(name: str, res: str = "1k") -> dict[str, Path]:
    """A texture set's colour, roughness and normal images as JPEG, fetched once."""
    files = texture_files(name, res)
    if all(f.exists() for f in files.values()):
        return files
    listing = _json(f"{API}/files/{name}")
    for key, map_name in TEXTURE_MAPS:
        entry = (listing.get(map_name) or {}).get(res, {}).get("jpg")
        if entry is None:
            raise ValueError(f"Poly Haven has no {map_name} JPEG of {name!r} at {res}")
        _download(entry["url"], files[key])
    return files


def download_hdri(name: str, res: str = "2k") -> Path:
    """A sky as EXR, for a Dome Light's texture, fetched once."""
    dest = cache_dir() / "hdri" / f"{name}_{res}.exr"
    if dest.exists():
        return dest
    entry = (_json(f"{API}/files/{name}").get("hdri") or {}).get(res, {}).get("exr")
    if entry is None:
        raise ValueError(f"Poly Haven has no EXR of {name!r} at {res}")
    return _download(entry["url"], dest)


_fetching: set = set()


def headless(scene) -> bool:
    """True with no event loop to hand a finished download back on: a script, `usdchunks run`.
    There a download runs at once instead of in the background."""
    return scene is None or type(scene).call_later is chunk_scene.SceneSender.call_later


def fetch(node, key, work, report) -> None:
    """Run work() (a download) on a worker, then cook node again on the main thread.

    Once per key at a time, however many nodes ask. A node deleted meanwhile is not brought
    back; a failure is said with report(node, text).
    """
    scene = node.get_scene()
    if key in _fetching or scene is None:
        return
    _fetching.add(key)
    owner = weakref.ref(node)

    def run():
        error = ""
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - said on the node, never lost on a thread
            error = f"download failed: {exc}"

        def finish():
            _fetching.discard(key)
            if (live := owner()) is None:
                return
            if error:
                report(live, error)
            else:
                live.execute()

        scene.call_later(finish)

    threading.Thread(target=run, daemon=True).start()


@dataclass
class Model:
    """What a model's file holds, as the chunk needs it.

    Attributes:
        root: Its default prim's path.
        parts: The variations under it, by name (a rock set's rock_a, rock_b ...).
        bound: {material name: [prim paths bound to it, relative to the root]}.
        textures: {material name: {"diffuse"|"alpha"|"normal": image path}}.
        uv: The primvar its texture coordinates are in.
    """

    root: str
    parts: list[str] = field(default_factory=list)
    bound: dict[str, list[str]] = field(default_factory=dict)
    textures: dict[str, dict[str, str]] = field(default_factory=dict)
    uv: str = "st"


#: The images a look reads, by the part of the file name Poly Haven gives each. Roughness and
#: the rest are EXR in Poly Haven's USD, which Aurora cannot read: left out.
_MAPS = (("diffuse", "_diff_"), ("alpha", "_alpha_"))


def _texture_files(material: Usd.Prim) -> dict[str, str]:
    found: dict[str, str] = {}
    for prim in Usd.PrimRange(material):
        for attr in prim.GetAttributes():
            if attr.GetTypeName() != Sdf.ValueTypeNames.Asset or not attr.Get():
                continue
            path = (attr.Get().resolvedPath or "").replace("\\", "/")
            for key, tag in _MAPS:
                if tag in path.rsplit("/", 1)[-1] and not path.lower().endswith(".exr"):
                    found.setdefault(key, path)
    return found


def read_model(path: str | Path) -> Model:
    """The parts, materials and textures of a downloaded model file."""
    stage = Usd.Stage.Open(str(path))
    root = stage.GetDefaultPrim() or next(iter(stage.GetPseudoRoot().GetChildren()), None)
    if root is None:
        raise ValueError(f"{path} has no prims")
    info = Model(root.GetPath().pathString)
    info.parts = [c.GetName() for c in root.GetChildren() if c.IsA(UsdGeom.Xformable)]
    for prim in Usd.PrimRange(root):
        targets = UsdShade.MaterialBindingAPI(prim).GetDirectBindingRel().GetTargets()
        if not targets:
            continue
        name = targets[0].name
        info.bound.setdefault(name, []).append(prim.GetPath().MakeRelativePath(root.GetPath()).pathString)
        if name not in info.textures:
            material = stage.GetPrimAtPath(targets[0])
            info.textures[name] = _texture_files(material) if material else {}
    for prim in stage.Traverse():
        shader = UsdShade.Shader(prim)
        if shader and shader.GetIdAttr().Get() == "UsdPrimvarReader_float2":
            varname = shader.GetInput("varname")
            if varname and varname.Get():
                info.uv = str(varname.Get())
                break
    return info


def author_look(stage: Usd.Stage, path: str, files: dict[str, str], uv: str = "st") -> UsdShade.Material:
    """A UsdPreviewSurface material at path reading files: its colour, and its cut-out (a
    leaf's outline) when it has one. Roughness is a constant: Poly Haven's maps for it are EXR."""
    material = UsdShade.Material.Define(stage, path)
    surface = UsdShade.Shader.Define(stage, f"{path}/surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(surface.ConnectableAPI(), "surface")
    surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.8)
    if not files:
        surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set((0.5, 0.5, 0.5))
        return material
    reader = UsdShade.Shader.Define(stage, f"{path}/st")
    reader.CreateIdAttr("UsdPrimvarReader_float2")
    reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set(uv)
    st = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    def image(name: str, file: str, colour_space: str, output: str, type_name):
        tex = UsdShade.Shader.Define(stage, f"{path}/{name}")
        tex.CreateIdAttr("UsdUVTexture")
        tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(file)
        tex.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set(colour_space)
        tex.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(st)
        for wrap in ("wrapS", "wrapT"):
            tex.CreateInput(wrap, Sdf.ValueTypeNames.Token).Set("repeat")
        return tex.CreateOutput(output, type_name)

    if "diffuse" in files:
        rgb = image("diffuse", files["diffuse"], "sRGB", "rgb", Sdf.ValueTypeNames.Float3)
        surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(rgb)
    if "alpha" in files:
        cut = image("alpha", files["alpha"], "raw", "r", Sdf.ValueTypeNames.Float)
        surface.CreateInput("opacity", Sdf.ValueTypeNames.Float).ConnectToSource(cut)
        surface.CreateInput("opacityThreshold", Sdf.ValueTypeNames.Float).Set(0.5)
    return material


def author_texture_look(stage: Usd.Stage, path: str, files: dict, tiling: float = 1.0,
                        uv: str = "st") -> UsdShade.Material:
    """A UsdPreviewSurface material at path from a texture set: colour, roughness and normal
    map, repeated tiling times across the mesh's UVs (a UsdTransform2d: Storm honours it,
    Aurora does not)."""
    material = UsdShade.Material.Define(stage, path)
    surface = UsdShade.Shader.Define(stage, f"{path}/surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(surface.ConnectableAPI(), "surface")
    reader = UsdShade.Shader.Define(stage, f"{path}/st")
    reader.CreateIdAttr("UsdPrimvarReader_float2")
    reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set(uv)
    st = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
    if tiling != 1.0:
        scale = UsdShade.Shader.Define(stage, f"{path}/tiling")
        scale.CreateIdAttr("UsdTransform2d")
        scale.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(st)
        scale.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set((tiling, tiling))
        st = scale.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    def image(name, file, colour_space, output, type_name, scale=None, bias=None):
        tex = UsdShade.Shader.Define(stage, f"{path}/{name}")
        tex.CreateIdAttr("UsdUVTexture")
        tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(str(file))
        tex.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set(colour_space)
        tex.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(st)
        for wrap in ("wrapS", "wrapT"):
            tex.CreateInput(wrap, Sdf.ValueTypeNames.Token).Set("repeat")
        if scale is not None:
            tex.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(scale)
            tex.CreateInput("bias", Sdf.ValueTypeNames.Float4).Set(bias)
        return tex.CreateOutput(output, type_name)

    if "diffuse" in files:
        surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            image("diffuse", files["diffuse"], "sRGB", "rgb", Sdf.ValueTypeNames.Float3))
    if "rough" in files:
        surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).ConnectToSource(
            image("rough", files["rough"], "raw", "r", Sdf.ValueTypeNames.Float))
    if "normal" in files:
        surface.CreateInput("normal", Sdf.ValueTypeNames.Normal3f).ConnectToSource(
            image("normal", files["normal"], "raw", "rgb", Sdf.ValueTypeNames.Float3,
                  scale=(2.0, 2.0, 2.0, 1.0), bias=(-1.0, -1.0, -1.0, 0.0)))
    return material
