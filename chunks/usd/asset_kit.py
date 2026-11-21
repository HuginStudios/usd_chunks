"""Asset Kit chunk -- your own assets laid out ready to scatter: a folder of USD files, or prims.

A scatter copies whatever its prototype rows point at, as it is: a rock modelled in
centimetres comes in a hundred times too big, a plant whose pivot is at its middle sinks
into the ground, and a library asset placed in the scene brings its placement along. This
chunk takes a set of assets and lays each one out the way a scatter wants it: at the origin,
standing on the ground (its lowest point at height 0, centred over the origin), in the
scene's units and up axis, and, if Fit Size is set, scaled so its largest side is that long.
A Scatter Points row of <Location>/* then copies every one of them. Typical Size is
the largest side of the middle-sized one; Spacing Out is Spacing times that, for a
Scatter's Spacing to read, so a pile of big rocks spreads out as far as a pile of pebbles.

* Files: a folder (every .usd, .usda, .usdc and .usdz in it and its subfolders), files, or
  globs, separated by ";". A folder that has an asset in several detail levels
  (rock_low.usdz, rock_mid.usdz, rock_high.usdz) gives one of them, by Detail.
* Prims: prims in the scene to use, globs and %collections included. These are not put under
  a style's object: they name your assets wherever they are.
* Fallback: what to lay out when Files and Prims are both empty. A style points it at the
  built-in kit it makes, so it works before you give it anything. Fit Size is not applied to
  it: a built-in kit is made at the size the style wants.

Each asset is <Location>/<name>, with the asset itself referenced at
<Location>/<name>/place/asset. The fitting is on place, a level down, because a scatter
copies <Location>/<name> without its own transform. Prims used as the assets are hidden, as
a scatter hides its sources (Hide Sources), so only the copies are seen.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import glob
import os
import re

from pxr import Gf, Sdf, Tf, Usd, UsdGeom

from usd_chunks import chunk, chunk_utils

#: More assets than a scatter can use well; the rest are left out, and the readout says so.
MAX_ASSETS = 64
EXTENSIONS = (".usd", ".usda", ".usdc", ".usdz")
DETAILS = ("Low", "Mid", "High")
_LEVEL = re.compile(r"[_-](low|mid|high|lod\d+)$", re.IGNORECASE)


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _rank(level: str, detail: str) -> int:
    """How well a file's detail level suits Detail: 0 is the one asked for."""
    order = {"low": 0, "mid": 1, "high": 2}
    want = order[detail.lower()]
    if level.startswith("lod"):  # lod0 is the finest
        return abs(2 - min(int(level[3:]), 2) - want)
    return abs(order[level] - want)


def find_files(spec: str, detail: str = "Mid") -> list[str]:
    """The USD files `spec` names, one per asset.

    Args:
        spec: Folders, files or globs, separated by ";" or new lines.
        detail: Low, Mid or High: which of an asset's detail levels to take, when its files
            are named <asset>_low, _mid, _high (or _lod0, _lod1...).

    Returns:
        Absolute paths, sorted, at most one per asset.
    """
    found: list[str] = []
    for token in re.split(r"[;\n]+", spec or ""):
        token = os.path.expanduser(token.strip().strip('"'))
        if not token:
            continue
        if os.path.isdir(token):
            token = os.path.join(token, "**", "*")
        for path in glob.glob(token, recursive=True):
            if os.path.isfile(path) and path.lower().endswith(EXTENSIONS):
                found.append(os.path.abspath(path))
    best: dict[str, tuple[int, str]] = {}
    for path in sorted(set(found)):
        stem = os.path.splitext(path)[0]
        match = _LEVEL.search(stem)
        key, rank = (stem[:match.start()], _rank(match.group(1).lower(), detail)) if match else (stem, 0)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, path)
    return sorted(path for _, path in best.values())


def _name_for(text: str, taken: set[str]) -> str:
    """A prim name made from text, not one already used."""
    base = _LEVEL.sub("", text)
    name = Tf.MakeValidIdentifier(base) or "asset"
    out, i = name, 1
    while out in taken:
        i += 1
        out = f"{name}_{i}"
    taken.add(out)
    return out


def _file_reference(path: str):
    """(The reference to the file, its metres per unit, its up axis), or None for a file with
    no prim to reference."""
    layer = Sdf.Layer.FindOrOpen(path)
    if layer is None:
        return None
    target = layer.defaultPrim
    if not target:
        roots = [p.name for p in layer.rootPrims if p.specifier == Sdf.SpecifierDef]
        if not roots:
            return None
        target = roots[0]
    info = layer.pseudoRoot
    metres = info.GetInfo("metersPerUnit") if info.HasInfo("metersPerUnit") else 0.01
    up = info.GetInfo("upAxis") if info.HasInfo("upAxis") else "Y"
    return Sdf.Reference(path, Sdf.Path.absoluteRootPath.AppendChild(target)), float(metres), str(up)


def _scale_of(prim) -> float:
    """How much prim is scaled in the world, as one number (the mean of its axes')."""
    world = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(prim)
    return sum(Gf.Vec3d(world.GetRow3(i)).GetLength() for i in range(3)) / 3.0


def _sources(node, stage) -> tuple[list, str]:
    """[(name, reference, how much bigger than the file it is, up axis or None)] for the
    assets, and what they came from (files, prims or fallback). A file's size is its units in
    the scene's; a prim's is how much it is scaled in the scene, so a built-in kit made at a
    scale keeps it."""
    is_s = chunk.is_param_t(str)
    files = str(node.get_param("files").value or "")
    detail = node.get_param("detail", is_s).value or "Mid"
    taken: set[str] = set()
    out = []
    for path in find_files(files, detail):
        if (found := _file_reference(path)) is not None:
            ref, metres, up = found
            out.append((_name_for(os.path.splitext(os.path.basename(path))[0], taken), ref,
                        metres / UsdGeom.GetStageMetersPerUnit(stage), up))
    source = "files" if out else ""
    for param, label in (("prims", "prims"), ("fallback", "the built-in kit")):
        if out:
            break
        spec = (node.get_param(param, is_s).value or "").strip()
        if not spec:
            continue
        # Inactive prims too: a library of assets is often deactivated so it is not drawn.
        for path in chunk_utils.resolve_prim_paths(stage, spec, include_inactive=True):
            prim = stage.GetPrimAtPath(path)
            if prim and prim.IsValid() and not prim.IsPseudoRoot():
                out.append((_name_for(prim.GetName(), taken), Sdf.Reference("", Sdf.Path(path)),
                            _scale_of(prim), None))
        source = label if out else source
    return out, source


def execute(node) -> None:
    stage = node.get_stage()
    if stage is None:
        return
    is_s, is_f = chunk.is_param_t(str), chunk.is_param_t(float)
    location = (node.get_param("primpath_location", is_s).value or "").strip() or "/asset_kit"
    fit = float(node.get_param("fit", is_f).value or 0.0)
    assets, source = _sources(node, stage)
    if source == "the built-in kit":
        fit = 0.0
    if not assets:
        _report(node, "give it a folder of assets, files, or prims")
        return
    more = len(assets) - MAX_ASSETS
    assets = assets[:MAX_ASSETS]

    scene_up = UsdGeom.GetStageUpAxis(stage)
    up = 2 if scene_up == UsdGeom.Tokens.z else 1
    # A prim's scale is measured against the kit's own: under a scaled object, both are.
    within = _scale_of(UsdGeom.Xform.Define(stage, location).GetPrim()) or 1.0
    bbox = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    hide = bool(node.get_param("hide_sources", chunk.is_param_t(bool)).value)
    placed, hidden, sizes = 0, [], []
    for name, ref, size_in_scene, file_up in assets:
        holder = UsdGeom.Xform.Define(stage, f"{location}/{name}")
        place = UsdGeom.Xform.Define(stage, f"{location}/{name}/place")
        inner = stage.DefinePrim(f"{location}/{name}/place/asset")
        inner.GetReferences().AddReference(ref)
        if not inner.GetTypeName():  # the reference reached nothing
            stage.RemovePrim(holder.GetPath())
            continue
        inner.SetActive(True)  # a deactivated library entry comes alive here
        inner.SetInstanceable(False)
        if not ref.assetPath:
            # A prim in the scene: without the place it has there, and seen here even when
            # the prim itself is hidden.
            UsdGeom.Xformable(inner).GetXformOpOrderAttr().Set([])
            if inner.IsA(UsdGeom.Imageable):
                UsdGeom.Imageable(inner).CreateVisibilityAttr().Set(UsdGeom.Tokens.inherited)
            hidden.append(ref.primPath)
        turn = Gf.Matrix4d(1.0)
        if file_up and file_up != scene_up:
            turn.SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), 90.0 if scene_up == UsdGeom.Tokens.z else -90.0))
        unit = size_in_scene if ref.assetPath else size_in_scene / within
        box = bbox.ComputeLocalBound(inner)
        box.Transform(turn)
        rng = box.ComputeAlignedRange()
        if rng.IsEmpty():
            stage.RemovePrim(holder.GetPath())
            continue
        size = max(rng.GetSize()) * unit
        scale = unit * (fit / size if fit > 0.0 and size > 0.0 else 1.0)
        base = Gf.Vec3d(rng.GetMidpoint())
        base[up] = rng.GetMin()[up]
        # Ops apply right to left: scale, turn, then the move that puts the turned, scaled
        # base at the origin (a uniform scale and a turn commute, so it is -base * scale).
        xform = UsdGeom.Xformable(place)
        xform.AddTranslateOp().Set(-base * scale)
        if turn != Gf.Matrix4d(1.0):
            xform.AddTransformOp(opSuffix="upAxis").Set(turn)
        xform.AddScaleOp().Set(Gf.Vec3f(scale))
        placed += 1
        sizes.append(size * scale / unit)
    if hide:
        # Visibility, not deactivation: a built-in kit is written by chunks above this one, and
        # USD refuses to write under an inactive prim, so their next cook would fail.
        for path in hidden:
            if Sdf.Path(location).HasPrefix(path):
                continue
            src = stage.OverridePrim(path)
            if src.IsA(UsdGeom.Imageable):
                UsdGeom.Imageable(src).CreateVisibilityAttr().Set(UsdGeom.Tokens.invisible)
    # The middle size, not the mean: one long plank among stones should not spread them out.
    typical = float(sorted(sizes)[len(sizes) // 2]) if sizes else 0.0
    spacing = typical * float(node.get_param("spacing", is_f).value or 0.0)
    for param, value in (("size", typical), ("out_spacing", spacing)):
        if (out := node.get_param(param)) is not None and abs((out.value or 0.0) - value) > 1e-9:
            out.value = value
    note = f", {more} more left out" if more > 0 else ""
    _report(node, f"{placed} asset{'s' if placed != 1 else ''} from {source}{note}")


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Asset Kit")
    flags = chunk.ChunkFlags.ParamFlags
    labeled = flags.LABELED
    chunk_api.add_param(chunk.FileBrowser(
        "files", "Files", reads=True, extensions=list(EXTENSIONS), flags=labeled | flags.DIRECTORY,
        tooltip="A folder of USD files (its subfolders too), files, or globs, separated by \";\". "
        "C:/assets/rocks takes every rock in it; C:/assets/*/*_mid.usdz the mid detail of each.",
    ))
    chunk_api.add_param(chunk.Menu(
        "detail", "Detail", menu_items=list(DETAILS), default_value="Mid", flags=labeled,
        tooltip="Which file to take of an asset that comes in detail levels, named <asset>_low, "
        "_mid and _high (or _lod0, _lod1...).",
    ))
    # PRIM_PATHS, not PRIM_SELECTION: your assets are wherever they are, so in a style they are
    # not put under the object the style is on, as its own paths are.
    chunk_api.add_param(chunk.LineEdit(
        "prims", "Prims", flags=labeled | flags.PRIM_PATHS,
        tooltip="Prims in the scene to use as the assets: /Library/rocks/*, %rocks. Used when "
        "Files finds nothing. A deactivated library works: each copy is switched on.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "fallback", "Fallback", flags=labeled | flags.PRIM_SELECTION,
        tooltip="Prims to use when Files and Prims are both empty: a style points it at the "
        "built-in kit it makes.",
    ))
    chunk_api.add_param(chunk.Checkbox(
        "hide_sources", "Hide Sources", default_value=True, flags=labeled,
        tooltip="Hide the prims used as the assets (Prims, or the built-in kit), so only what "
        "is made from them is seen. Files are never shown.",
    ))
    chunk_api.add_param(chunk.Slider(
        "fit", "Fit Size", min_value=0.0, max_value=5.0, default_value=0.0, flags=labeled,
        tooltip="Scale each asset so its largest side is this long, in scene units, so rocks of "
        "any size make the same pile. 0 keeps each one's own size. Not applied to the Fallback.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "primpath_location", "Location", default_value="/asset_kit", flags=labeled,
        tooltip="Where the assets are laid out. A Scatter Points row of <Location>/* copies them all.",
    ))
    chunk_api.add_param(chunk.Slider(
        "spacing", "Spacing", min_value=0.0, max_value=4.0, default_value=1.0, flags=labeled,
        tooltip="How close a Scatter of these puts them, in sizes of the middle-sized asset: 1 is side "
        "by side, 0.3 piled up, 2 well apart. The Scatter's Spacing reads it as @Kit.out_spacing@, so "
        "bigger assets spread out by themselves.",
    ))
    chunk_api.add_param(chunk.Slider(
        "out_spacing", "Spacing Out", default_value=0.0, min_value=0.0, max_value=1000.0,
        flags=labeled | flags.OUTPUT | flags.TRANSIENT,
        tooltip="Read-only. Spacing times Typical Size, in scene units: for a Scatter's Spacing.",
    ))
    chunk_api.add_param(chunk.Slider(
        "size", "Typical Size", default_value=0.0, min_value=0.0, max_value=1000.0,
        flags=labeled | flags.OUTPUT | flags.TRANSIENT,
        tooltip="Read-only. The largest side of the middle-sized asset, as laid out. A Scatter's "
        "Spacing can follow it (@Kit.size@ * 0.3), so bigger assets spread out by themselves.",
    ))
    chunk_api.add_param(chunk.Label("readout", "no assets yet", default_value="no assets yet"))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="folder",
        description="""
        Lays out your own assets for a scatter: a folder of USD files, or prims in the scene.

        Each asset is put at the origin, standing on the ground, in the scene's units and up
        axis (a centimetre asset in a metre scene is scaled down), and with Fit Size, scaled
        so its largest side is that long. A Scatter Points row of <Location>/* copies them.
        Styles give you one per kind of thing they place (Debris, Plants, Props), so they
        scatter your assets instead of their built-in ones.
        """,
    )
