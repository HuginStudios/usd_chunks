"""MDL to MaterialX -- give MDL-shaded materials a portable surface.

Assets authored for Omniverse bind their looks through MDL (`OmniPBR.mdl`, `OmniGlass.mdl`)
via `info:mdl:sourceAsset`. MDL is NVIDIA's, so every other renderer -- V-Ray, Storm,
Karma, Cycles -- finds no surface it can build and falls back to default grey. The asset
opens anywhere and shades nowhere, which is a quiet way for a "portable" USD asset to be
portable in geometry only.

This authors a MaterialX `standard_surface` from the MDL parameters that already carry the
look, and connects it as the material's **mtlx** surface output.

Adding an output rather than replacing the material is the whole trick: a UsdShade.Material
can carry several surface outputs at once, one per render context. The MDL output stays
exactly where it was, every existing binding keeps pointing at the same material prim, and
nothing has to be rebound -- a renderer simply picks the context it understands.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import logging

from pxr import Sdf, Usd, UsdShade

from usd_chunks import chunk

logger = logging.getLogger(__name__)

#: MDL parameter -> standard_surface input, for the scalars that map one to one.
_SCALARS = {
    "metallic_constant": ("metalness", 0.0),
    "reflection_roughness_constant": ("specular_roughness", 0.4),
    "specular_level": ("specular", 0.5),
}

_MTLX = "mtlx"


def _authored(shader, name):
    """An MDL input's value if it was actually authored, else None."""
    inp = shader.GetInput(name)
    if inp is None:
        return None
    attr = inp.GetAttr()
    return attr.Get() if attr and attr.HasAuthoredValue() else None


def _mdl_shader(material):
    """The material's MDL surface shader, or None if it is not MDL-shaded."""
    for child in material.GetPrim().GetChildren():
        if child.GetTypeName() != "Shader":
            continue
        src = child.GetAttribute("info:mdl:sourceAsset")
        if src and src.HasAuthoredValue():
            sub = child.GetAttribute("info:mdl:sourceAsset:subIdentifier")
            return UsdShade.Shader(child), str(sub.Get()) if sub and sub.HasAuthoredValue() else ""
    return None, ""


def _build_surface(stage, material, mdl, sub_id):
    """Author a standard_surface beside the MDL shader and return it."""
    path = material.GetPath().AppendChild("mtlx_surface")
    surface = UsdShade.Shader.Define(stage, path)
    surface.CreateIdAttr("ND_standard_surface_surfaceshader")

    for mdl_name, (mtlx_name, fallback) in _SCALARS.items():
        value = _authored(mdl, mdl_name)
        if value is None and mdl_name != "specular_level":
            continue
        surface.CreateInput(mtlx_name, Sdf.ValueTypeNames.Float).Set(
            float(value if value is not None else fallback)
        )

    # A texture wins over the constant: these assets author diffuse_color_constant as flat
    # white and put the entire look in the map, so honouring only the constant produces a
    # correctly-lit, entirely white appliance.
    texture = _authored(mdl, "diffuse_texture")
    colour = _authored(mdl, "diffuse_color_constant")
    if texture is not None:
        image = UsdShade.Shader.Define(stage, material.GetPath().AppendChild("mtlx_basecolor"))
        image.CreateIdAttr("ND_image_color3")
        # Resolve to an absolute path before copying it across. An asset path is
        # anchored to the layer that authored it, and this surface is authored into the
        # chunk's own layer -- a different anchor. Carrying the authored relative path
        # over means the texture silently fails to resolve and every material renders
        # black, which reads as a shading bug rather than a path one.
        resolved = texture.resolvedPath or texture.path
        image.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(resolved))
        # The assets carry no explicit wrap mode and their maps are laid out in UV 0-1.
        image.CreateInput("uaddressmode", Sdf.ValueTypeNames.String).Set("periodic")
        image.CreateInput("vaddressmode", Sdf.ValueTypeNames.String).Set("periodic")
        surface.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            image.ConnectableAPI(), "out"
        )
    elif colour is not None:
        surface.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).Set(colour)

    if "glass" in sub_id.lower():
        # OmniGlass carries no standard_surface equivalent to copy across, so this is the
        # one place a look is asserted rather than translated: clear, thin, barely rough.
        surface.CreateInput("transmission", Sdf.ValueTypeNames.Float).Set(1.0)
        surface.CreateInput("specular_roughness", Sdf.ValueTypeNames.Float).Set(0.02)
        surface.CreateInput("metalness", Sdf.ValueTypeNames.Float).Set(0.0)
        glass_colour = _authored(mdl, "glass_color")
        if glass_colour is not None:
            surface.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).Set(glass_colour)

    return surface


def execute(node):
    root_path = node.get_param("primpath_location").value
    if not root_path:
        return

    stage = node.get_stage()
    root = stage.GetPrimAtPath(Sdf.Path(root_path))
    if not root or not root.IsValid():
        logger.warning("no prim at %s", root_path)
        return

    converted = skipped = 0
    for prim in Usd.PrimRange(root):
        if prim.GetTypeName() != "Material":
            continue
        material = UsdShade.Material(prim)
        mdl, sub_id = _mdl_shader(material)
        if mdl is None:
            skipped += 1
            continue
        surface = _build_surface(stage, material, mdl, sub_id)
        material.CreateSurfaceOutput(_MTLX).ConnectToSource(surface.ConnectableAPI(), "out")
        converted += 1

    logger.debug(
        "%d material(s) given an mtlx surface under %s%s",
        converted, root_path, f", {skipped} not MDL" if skipped else "",
    )


def main():
    chunk_api = chunk.Chunk("MDL to MaterialX")
    chunk_api.add_param(
        chunk.LineEdit(
            "primpath_location",
            "Prim Path",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="Root to scan. Every Material beneath it whose shader is MDL gets a "
            "MaterialX standard_surface added as a second surface output.",
        )
    )
    return chunk_api


def config():
    return chunk.DefaultConfig(category=chunk.StandardCategory.MTLX, icon="sphere")
