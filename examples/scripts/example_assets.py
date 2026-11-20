"""The free assets the pipeline examples use: downloaded once, and made ready to render.

    python examples/scripts/example_assets.py      # fetch everything now, about 200 MB

Running this first is optional: each example fetches what it needs the first time it runs.
Nothing is downloaded twice.

* Models, textures and HDRIs from Poly Haven (https://polyhaven.com), CC0: free for any use.
* The SO-101 robot arm from TheRobotStudio's SO-ARM100 repository, Apache 2.0.

Files go to `examples/assets/downloads/`, or to the folder in `USD_CHUNK_EXAMPLE_ASSETS`.
Set that to share one download between checkouts.

The second half builds with chunks: `add_models()` brings models in with Reference, Reset
Transform and Bind Material chunks, and `texture_look()` puts a texture on a mesh.

Poly Haven's USD files come from Blender. Their MaterialX networks don't render in Storm or
Aurora (plants come out black, or Aurora crashes), and their preview networks leave out the
leaf cut-outs. So each model gets a plain UsdPreviewSurface built from its own textures,
which every Hydra renderer draws.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

from pxr import Plug, Sdf, Tf, Usd, UsdGeom, UsdShade

ROOT = Path(os.environ.get("USD_CHUNK_EXAMPLE_ASSETS")
            or Path(__file__).resolve().parent.parent / "assets" / "downloads")
API = "https://api.polyhaven.com/files/"
AGENT = {"User-Agent": "usd_chunks-examples"}

# SO-ARM100 at a fixed commit, so the arm the examples pose never changes under them.
SO101_COMMIT = "eecbe3e0a9ebb23e25ad7b2759b03884c6660903"
SO101_RAW = f"https://raw.githubusercontent.com/TheRobotStudio/SO-ARM100/{SO101_COMMIT}/Simulation/SO101/"

MODELS = ("fir_sapling", "shrub_04", "grass_medium_01", "nettle_plant", "boulder_01", "dead_tree_trunk_02",
          "tree_stump_01")
TEXTURES = ("brown_mud_leaves_01", "grass_path_2", "concrete_floor_02", "rock_boulder_dry", "coast_sand_01",
            "aerial_grass_rock")
HDRIS = ("kloofendal_48d_partly_cloudy_puresky",)


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def _get(url: str, dest: Path) -> Path:
    """Download *url* to *dest* unless it is there already."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=60) as response, \
            open(part, "wb") as out:
        while block := response.read(1 << 20):
            out.write(block)
    part.replace(dest)
    return dest


def _files(name: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(API + name, headers=AGENT), timeout=60) as response:
        return json.load(response)


def model(name: str, res: str = "1k") -> Path:
    """A Poly Haven model as USD, with its textures. Returns the .usdc file."""
    dest = ROOT / "polyhaven" / name / f"{name}_{res}.usdc"
    if dest.exists():
        return dest
    entry = _files(name)["usd"][res]["usd"]
    size = entry["size"] + sum(f["size"] for f in entry.get("include", {}).values())
    print(f"Downloading {name} ({size / 1e6:.0f} MB) from polyhaven.com ...", flush=True)
    for rel, f in entry.get("include", {}).items():
        _get(f["url"], dest.parent / rel)
    return _get(entry["url"], dest)  # last, so a broken download is fetched again


def texture(name: str, res: str = "2k") -> dict[str, Path]:
    """A Poly Haven texture set: {"diffuse": jpg, "rough": jpg}."""
    folder = ROOT / "polyhaven" / name
    found = {"diffuse": folder / f"{name}_diff_{res}.jpg", "rough": folder / f"{name}_rough_{res}.jpg"}
    if not all(p.exists() for p in found.values()):
        print(f"Downloading texture {name} from polyhaven.com ...", flush=True)
        files = _files(name)
        for key, map_name in (("diffuse", "Diffuse"), ("rough", "Rough")):
            _get(files[map_name][res]["jpg"]["url"], found[key])
    return found


def hdri(name: str = HDRIS[0], res: str = "2k") -> Path:
    """A Poly Haven HDRI as EXR, for a Dome Light."""
    dest = ROOT / "polyhaven" / "hdri" / f"{name}_{res}.exr"
    if not dest.exists():
        print(f"Downloading HDRI {name} from polyhaven.com ...", flush=True)
        _get(_files(name)["hdri"][res]["exr"]["url"], dest)
    return dest


def so101() -> Path:
    """The SO-101 arm: its URDF and the STL meshes it names. Returns the .urdf file."""
    dest = ROOT / "so101" / "so101_new_calib.urdf"
    if dest.exists():
        return dest
    print("Downloading the SO-101 arm (16 MB) from github.com/TheRobotStudio/SO-ARM100 ...", flush=True)
    part = _get(SO101_RAW + dest.name, dest.with_name("so101_new_calib.urdf.part"))
    for rel in sorted(set(re.findall(r'filename="([^"]+)"', part.read_text(encoding="utf-8")))):
        _get(SO101_RAW + rel, dest.parent / rel)
    part.replace(dest)  # last, so a broken download is fetched again
    return dest


def fetch_all() -> None:
    so101()
    for name in MODELS:
        model(name)
    for name in TEXTURES:
        texture(name)
    for name in HDRIS:
        hdri(name)


# ---------------------------------------------------------------------------
# Building the scene with chunks
# ---------------------------------------------------------------------------


def has_aurora() -> bool:
    """True when the Aurora path tracer is installed for this USD (2026-11 bundle and later)."""
    return Plug.Registry().FindTypeByName("HdAuroraRendererPlugin") != Tf.Type.Unknown


def _texture_files(material: Usd.Prim) -> dict[str, str]:
    """The material's colour, cut-out and roughness images, by the names Poly Haven gives them."""
    found = {}
    for prim in Usd.PrimRange(material):
        for attr in prim.GetAttributes():
            if attr.GetTypeName() != Sdf.ValueTypeNames.Asset or not attr.Get():
                continue
            path = attr.Get().resolvedPath.replace("\\", "/")
            for key, tag in (("diffuse", "_diff_"), ("alpha", "_alpha_"), ("rough", "_rough_")):
                if tag in path.rsplit("/", 1)[-1]:
                    found.setdefault(key, path)
    return found


def _read_model(asset: str, variation: str | None) -> tuple[str, dict, dict, str]:
    """What the chunks need to know about a Poly Haven model, read from its file.

    Returns the prim to import (the variation, or the model's only part), its materials as
    {name: [prims bound to it, relative to that prim]} and {name: texture files}, and the
    primvar its texture coordinates are in.
    """
    stage = Usd.Stage.Open(str(model(asset)))
    root = stage.GetDefaultPrim()
    if variation is None:
        parts = [c.GetName() for c in root.GetChildren() if c.IsA(UsdGeom.Xformable)]
        if len(parts) != 1:
            raise ValueError(f"{asset} has {len(parts)} parts ({', '.join(parts)}): name the one to use")
        variation = parts[0]
    part = root.GetChild(variation)
    bound, files = {}, {}
    for prim in Usd.PrimRange(part):
        targets = UsdShade.MaterialBindingAPI(prim).GetDirectBindingRel().GetTargets()
        if targets:
            name = targets[0].name
            bound.setdefault(name, []).append(prim.GetPath().MakeRelativePath(part.GetPath()).pathString)
            # Colour and cut-out only: their roughness maps are EXR, which Aurora can't read.
            textures = _texture_files(stage.GetPrimAtPath(targets[0]))
            files.setdefault(name, {k: v for k, v in textures.items() if k != "rough"})
    readers = [UsdShade.Shader(p).GetInput("varname") for p in stage.Traverse()
               if p.IsA(UsdShade.Shader) and UsdShade.Shader(p).GetIdAttr().Get() == "UsdPrimvarReader_float2"]
    varname = next((str(r.Get()) for r in readers if r and r.Get()), "st")
    return part.GetPath().pathString, bound, files, varname


def _texture_shader(material: str, name: str, file: str, colour_space: str, output: str) -> str:
    return f'''
    def Shader "{name}"
    {{
        uniform token info:id = "UsdUVTexture"
        asset inputs:file = @{file}@
        token inputs:sourceColorSpace = "{colour_space}"
        float2 inputs:st.connect = <{material}/st.outputs:result>
        token inputs:wrapS = "repeat"
        token inputs:wrapT = "repeat"
        {output}
    }}'''


def _look(path: str, files: dict[str, str], varname: str = "st") -> str:
    """A Material at *path*: a UsdPreviewSurface reading *files* (diffuse, rough, alpha)."""
    name = path.rsplit("/", 1)[-1]
    inputs = []
    shaders = [f'''
    def Shader "st"
    {{
        uniform token info:id = "UsdPrimvarReader_float2"
        string inputs:varname = "{varname}"
        float2 outputs:result
    }}''']
    if "diffuse" in files:
        inputs.append(f"color3f inputs:diffuseColor.connect = <{path}/diffuse.outputs:rgb>")
        shaders.append(_texture_shader(path, "diffuse", files["diffuse"], "sRGB", "color3f outputs:rgb"))
    if "rough" in files:
        inputs.append(f"float inputs:roughness.connect = <{path}/rough.outputs:r>")
        shaders.append(_texture_shader(path, "rough", files["rough"], "raw", "float outputs:r"))
    else:
        inputs.append("float inputs:roughness = 0.75")
    if "alpha" in files:
        # A cut-out, not a see-through leaf: below the threshold is not drawn at all.
        inputs += [f"float inputs:opacity.connect = <{path}/alpha.outputs:r>", "float inputs:opacityThreshold = 0.5"]
        shaders.append(_texture_shader(path, "alpha", files["alpha"], "raw", "float outputs:r"))
    body = "\n        ".join(inputs)
    return f'''def Material "{name}"
{{
    token outputs:surface.connect = <{path}/surface.outputs:surface>

    def Shader "surface"
    {{
        uniform token info:id = "UsdPreviewSurface"
        {body}
        token outputs:surface
    }}''' + "".join(shaders) + "\n}"


def looks_layer(root: str, looks: dict[str, tuple[dict[str, str], str]]) -> str:
    """A layer for Inline USD: a Scope *root* of textured materials, {name: (files, uv primvar)}.

    The one part of these examples that is plain USD. A texture wired into a material from
    chunks (Preview Surface, UV Texture, Primvar Reader) doesn't reach the real shaders yet,
    and a material needs that for its colour, roughness and leaf cut-out maps.
    """
    names = [p for p in root.split("/") if p]
    body = "\n\n".join(_look(f"{root}/{name}", files, varname) for name, (files, varname) in looks.items())
    body = "\n".join("    " + line if line else line for line in body.split("\n"))
    text = f'def Scope "{names[-1]}"\n{{\n{body}\n}}'
    for name in reversed(names[:-1]):
        text = f'over "{name}"\n{{\n' + "\n".join("    " + line if line else line for line in text.split("\n")) + "\n}"
    return "#usda 1.0\n" + text + "\n"


def texture_look(scene, upstream, path: str, name: str, prims: str):
    """Chunks for Poly Haven texture *name* on *prims* (the ground, say): the material at *path*.

    It repeats once per unit of the mesh's own st. Aurora ignores a UsdTransform2d (UV
    Transform), so to make it repeat every few metres, build the ground mesh small and scale
    it up with a Transform: its st stays in the small units.
    """
    files = {key: file.as_posix() for key, file in texture(name).items()}
    root, _, look = path.rpartition("/")
    title = look.title().replace("_", "")
    last = scene.create.inline_usd(upstream, name=f"{title}Look", code=looks_layer(root, {look: (files, "st")}))
    # Weaker than Descendants: a face subset of the mesh (a Height Field's sand band) keeps
    # the material bound to it.
    return scene.create.bind_material(last, name=f"Bind{title}", material_path=path, primpath_location=prims,
                                      strength="Weaker than Descendants")


def add_models(scene, upstream, root: str, items: dict[str, tuple[str, str | None]], network_dir: Path,
               hidden: bool = True, looks: str = "/looks"):
    """Chunks that bring Poly Haven models in under *root*, ready to render. Returns the last.

    *items* maps a name to (Poly Haven model, the variation to use, or None for a model of
    one part). *network_dir* is the folder the network is saved in: file paths are written
    relative to it, so the network keeps working when moved together with the assets.

      Prim             a Scope at *root*, made invisible when *hidden* (prototypes that a
                       Scatter Points copies, not things to render where they stand)
      Reference        per item, the variation's prim (Import Prim Path), at <root>/<name>
      Reset Transform  every variation at the origin: Poly Haven sets them side by side
      Inline USD       the looks, a UsdPreviewSurface per material from the model's own
                       textures (see looks_layer), under *looks*. Their own MaterialX renders black in
                       Storm and crashes Aurora, so the Reference leaves it out.
      Bind Material    per look, onto the meshes and face subsets the model bound it to.
                       Weaker than Descendants, so a face subset (a fir's twigs) keeps its
                       own look rather than the mesh's (the bark).
    """
    last = scene.create.prim(upstream, name=f"{root.rsplit('/', 1)[-1].title()}Root", primpath_location=root,
                             prim_type="Scope")
    if hidden:
        last = scene.create.property_editor(last, name="HidePrototypes", prim=root, edits='visibility = "invisible"')
    materials, binds = {}, {}
    for name, (asset, variation) in items.items():
        part, bound, files, varname = _read_model(asset, variation)
        file = Path(os.path.relpath(model(asset), network_dir)).as_posix()
        last = scene.create.reference(last, name=name.title().replace("_", ""), file_path=file,
                                      import_primpath=part, primpath_location=f"{root}/{name}")
        for look, prims in bound.items():
            materials[look] = (files[look], varname)
            binds.setdefault(look, []).extend(f"{root}/{name}" + ("" if p == "." else f"/{p}") for p in prims)
    last = scene.create.reset_transform(last, name="ToOrigin", prim_path=",".join(f"{root}/{n}" for n in items))
    last = scene.create.inline_usd(last, name="ModelLooks", code=looks_layer(looks, materials))
    for look, prims in binds.items():
        last = scene.create.bind_material(last, name=f"Bind_{look}", material_path=f"{looks}/{look}",
                                          primpath_location=",".join(prims), strength="Weaker than Descendants")
    return last


def aurora_settings(path: str = "/Render/aurora", samples: int = 128) -> str:
    """A session layer for usdrecord: RenderSettings that stop Aurora after *samples*, denoised."""
    scope, _, name = path.strip("/").rpartition("/")
    return f"""#usda 1.0
def Scope "{scope}"
{{
    def RenderSettings "{name}"
    {{
        int aurora:max_samples = {samples}
        bool aurora:is_denoising_enabled = 1
        bool aurora:use_environment_image_as_background = 1
    }}
}}
"""


def render_params(out: Path, samples: int = 128) -> dict:
    """Render chunk params for the best renderer here: Aurora when it is installed, else Storm.

    Aurora reads its own settings (samples, denoiser, the HDRI as background) only from the
    session layer, not from the stage, so they go in a small file passed to usdrecord.
    """
    if has_aurora():
        layer = out / "aurora_settings.usda"
        layer.write_text(aurora_settings(samples=samples), encoding="utf-8")
        return {"renderer": "Aurora", "extra_args": f"--sessionLayer {layer.as_posix()} -rs /Render/aurora"}
    return {"extra_args": "--enableDomeLightVisibility"}


if __name__ == "__main__":
    fetch_all()
    print(f"Assets are in {ROOT}")
