"""The free assets the pipeline examples use: downloaded once, and made ready to render.

    python examples/scripts/example_assets.py      # fetch everything now, about 230 MB

Running this first is optional: each example fetches what it needs the first time it runs.
Nothing is downloaded twice.

* Models, textures and HDRIs from Poly Haven (https://polyhaven.com), CC0: free for any use.
* The SO-101 robot arm from TheRobotStudio's SO-ARM100 repository, Apache 2.0.

Files go to `examples/assets/downloads/`, or to the folder in `USD_CHUNK_EXAMPLE_ASSETS`.
Set that to share one download between checkouts.

Poly Haven's USD files come from Blender. Their MaterialX networks don't render in Storm or
Aurora (plants come out black, or Aurora crashes), and their preview networks leave out the
leaf cut-outs. `prototypes()` gives each asset a plain UsdPreviewSurface built from its own
textures instead, which every Hydra renderer draws.
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

MODELS = ("fir_sapling", "fern_02", "shrub_04", "grass_medium_01", "nettle_plant", "boulder_01",
          "rock_moss_set_01", "dead_tree_trunk_02", "tree_stump_01", "barrel_03", "concrete_road_barrier")
TEXTURES = ("brown_mud_leaves_01", "grass_path_2", "concrete_floor_02", "rock_boulder_dry")
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
# Ready to render
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


def _texture_prim(at: str, name: str, path: str, colour_space: str) -> str:
    return f'''
def Shader "{name}"
{{
    uniform token info:id = "UsdUVTexture"
    asset inputs:file = @{path}@
    token inputs:sourceColorSpace = "{colour_space}"
    float2 inputs:st.connect = <{at}/look_st.outputs:result>
    token inputs:wrapS = "repeat"
    token inputs:wrapT = "repeat"
    float outputs:r
    color3f outputs:rgb
}}'''


def _preview(at: str, files: dict[str, str], varname: str, roughness: float = 0.75) -> str:
    """Shader prims for a UsdPreviewSurface named look under the material at *at*."""
    inputs = [f"float inputs:roughness = {roughness}"]
    prims = [f'''
def Shader "look_st"
{{
    uniform token info:id = "UsdPrimvarReader_float2"
    string inputs:varname = "{varname}"
    float2 outputs:result
}}''']
    if "diffuse" in files:
        inputs.append(f"color3f inputs:diffuseColor.connect = <{at}/look_diff.outputs:rgb>")
        prims.append(_texture_prim(at, "look_diff", files["diffuse"], "sRGB"))
    if "alpha" in files:
        # A cut-out, not a see-through leaf: below the threshold is not drawn at all.
        inputs += [f"float inputs:opacity.connect = <{at}/look_alpha.outputs:r>", "float inputs:opacityThreshold = 0.5"]
        prims.append(_texture_prim(at, "look_alpha", files["alpha"], "raw"))
    body = "\n    ".join(inputs)
    return f'''
def Shader "look"
{{
    uniform token info:id = "UsdPreviewSurface"
    {body}
    token outputs:surface
}}''' + "".join(prims)


def _nest(names: list[str], body: str, specifier: str = "over") -> str:
    """*body* inside nested prims *names* (outermost first), indented."""
    for name in reversed(names):
        body = f'{specifier} "{name}"\n{{\n' + _indent(body) + "\n}"
    return body


def _indent(text: str) -> str:
    return "\n".join("    " + line if line else line for line in text.strip("\n").split("\n"))


def _prototype(path: str, asset: str, variation: str | None) -> str:
    """One prototype: the asset referenced at *path*, one variation, preview materials."""
    file = model(asset)
    stage = Usd.Stage.Open(str(file))
    root = stage.GetDefaultPrim()
    body = []
    if variation:
        # Poly Haven puts a set's variations side by side; keep one, moved to the origin, and
        # deactivate the rest so no renderer loads them.
        t = UsdGeom.Xformable(root.GetChild(variation)).GetLocalTransformation().ExtractTranslation()
        body += [f"double3 xformOp:translate = ({-t[0]}, {-t[1]}, {-t[2]})",
                 'uniform token[] xformOpOrder = ["xformOp:translate"]']
        body += [f'over "{child.GetName()}" (\n    active = false\n)\n{{\n}}' for child in root.GetChildren()
                 if child.IsA(UsdGeom.Xformable) and child.GetName() != variation]
    readers = [UsdShade.Shader(p).GetInput("varname") for p in stage.Traverse()
               if p.IsA(UsdShade.Shader) and UsdShade.Shader(p).GetIdAttr().Get() == "UsdPrimvarReader_float2"]
    varname = next((str(r.Get()) for r in readers if r and r.Get()), "st")
    # One override per material, grouped by parent: a layer may hold each prim spec once.
    groups: dict[str, list[str]] = {}
    for material in (p for p in stage.Traverse() if p.IsA(UsdShade.Material)):
        rel = material.GetPath().MakeRelativePath(root.GetPath()).pathString
        at = f"{path}/{rel}"
        groups.setdefault(rel.rpartition("/")[0], []).append(_nest([material.GetName()], f"""token outputs:mtlx:surface.connect = None
token outputs:displacement.connect = None
token outputs:surface.connect = <{at}/look.outputs:surface>
{_preview(at, _texture_files(material), varname)}"""))
    for parent, overs in groups.items():
        body.append(_nest(parent.split("/") if parent else [], "\n".join(overs)))
    leaf = path.rsplit("/", 1)[-1]
    return f'def Xform "{leaf}" (\n    prepend references = @{file.as_posix()}@\n)\n{{\n' + _indent("\n".join(body)) + "\n}"


def prototypes(root: str, items: dict[str, tuple[str, str | None]], hidden: bool = True) -> str:
    """A layer for Inline USD: a Scope *root* holding one asset per item, ready to render.

    *items* maps a name to (Poly Haven asset, variation to keep or None). Hidden, for
    prototypes a Scatter Points copies; *hidden* False for props placed as they are.
    """
    protos = "\n".join(_prototype(f"{root}/{name}", asset, variation) for name, (asset, variation) in items.items())
    if hidden:
        protos = 'token visibility = "invisible"\n' + protos
    names = [p for p in root.split("/") if p]
    return "#usda 1.0\n" + _nest(names[:-1], _nest(names[-1:], protos, "def Scope")) + "\n"


def ground_material(path: str, name: str) -> str:
    """A layer for Inline USD: a Material at *path* with Poly Haven texture *name*, once per st unit.

    Aurora ignores a UsdTransform2d, so to make the texture repeat every few metres, build
    the ground mesh small and scale it up with a Transform: its st stays in the small units.
    """
    files = texture(name)
    names = [p for p in path.split("/") if p]
    material = f'''token outputs:surface.connect = <{path}/look.outputs:surface>
def Shader "look"
{{
    uniform token info:id = "UsdPreviewSurface"
    color3f inputs:diffuseColor.connect = <{path}/diffuse.outputs:rgb>
    float inputs:roughness.connect = <{path}/rough.outputs:r>
    token outputs:surface
}}
def Shader "st"
{{
    uniform token info:id = "UsdPrimvarReader_float2"
    string inputs:varname = "st"
    float2 outputs:result
}}'''
    for shader, file, space, out in (("diffuse", files["diffuse"], "sRGB", "color3f outputs:rgb"),
                                     ("rough", files["rough"], "raw", "float outputs:r")):
        material += f'''
def Shader "{shader}"
{{
    uniform token info:id = "UsdUVTexture"
    asset inputs:file = @{file.as_posix()}@
    token inputs:sourceColorSpace = "{space}"
    float2 inputs:st.connect = <{path}/st.outputs:result>
    token inputs:wrapS = "repeat"
    token inputs:wrapT = "repeat"
    {out}
}}'''
    return "#usda 1.0\n" + _nest(names[:-1], _nest(names[-1:], material, "def Material")) + "\n"


def aurora_settings(path: str = "/Render/aurora", samples: int = 128) -> str:
    """A layer for Inline USD: RenderSettings that stop Aurora after *samples*, denoised."""
    names = [p for p in path.split("/") if p]
    settings = f"""int aurora:max_samples = {samples}
bool aurora:is_denoising_enabled = 1
bool aurora:use_environment_image_as_background = 1"""
    return "#usda 1.0\n" + _nest(names[:-1], _nest(names[-1:], settings, "def RenderSettings"), "def Scope") + "\n"


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
