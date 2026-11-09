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

from pxr import Gf, Sdf, Usd, UsdShade

from usd_chunks import chunk

logger = logging.getLogger(__name__)

#: MDL parameter -> standard_surface input, for the scalars that map one to one.
_SCALARS = {
    "metallic_constant": ("metalness", 0.0),
    "reflection_roughness_constant": ("specular_roughness", 0.4),
    "specular_level": ("specular", 0.5),
}

_MTLX = "mtlx"

#: Normal map scale, between the flat 0.1 the assets author and full strength.
_NORMAL_STRENGTH = 0.5


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


#: (standard_surface input, ORM channel index, MDL influence input, MDL constant, default)
_ORM_CHANNELS = (
    ("specular_roughness", 1, "reflection_roughness_texture_influence",
     "reflection_roughness_constant", 0.5),
    ("metalness", 2, "metallic_texture_influence", "metallic_constant", 0.0),
)

#: Colour space of a map that holds data, not colour. MaterialX has no "raw" (that is USD's
#: word, and a colour transform from it is unsupported); the working space is no transform.
_DATA_SPACE = "lin_rec709"


def _connect_orm(stage, material, mdl, surface):
    """Drive roughness and metalness from an OmniPBR ORM map (G = roughness, B = metal).

    OmniPBR blends constant and map by `*_texture_influence`, so the constant wins at 0
    and the map at 1; anything between becomes a mix. The map is data, not colour, so it
    is read in the working space -- tagging it sRGB would bend every roughness value.
    """
    orm = _authored(mdl, "ORM_texture")
    if orm is None or not _authored(mdl, "enable_ORM_texture"):
        return

    base = material.GetPath()
    image = UsdShade.Shader.Define(stage, base.AppendChild("mtlx_orm"))
    image.CreateIdAttr("ND_image_color3")
    file_input = image.CreateInput("file", Sdf.ValueTypeNames.Asset)
    file_input.Set(Sdf.AssetPath(orm.resolvedPath or orm.path))
    file_input.GetAttr().SetColorSpace(_DATA_SPACE)
    image.CreateInput("uaddressmode", Sdf.ValueTypeNames.String).Set("periodic")
    image.CreateInput("vaddressmode", Sdf.ValueTypeNames.String).Set("periodic")
    image.CreateOutput("out", Sdf.ValueTypeNames.Color3f)

    def channel_of(target, index):
        # One extract per channel: Aurora cannot compile a separate3's several outputs.
        extract = UsdShade.Shader.Define(stage, base.AppendChild(f"mtlx_{target}_channel"))
        extract.CreateIdAttr("ND_extract_color3")
        extract.CreateInput("in", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            image.ConnectableAPI(), "out"
        )
        extract.CreateInput("index", Sdf.ValueTypeNames.Int).Set(index)
        extract.CreateOutput("out", Sdf.ValueTypeNames.Float)
        return extract.ConnectableAPI()

    for target, index, influence_name, constant_name, default in _ORM_CHANNELS:
        influence = _authored(mdl, influence_name)
        influence = 1.0 if influence is None else float(influence)
        constant = _authored(mdl, constant_name)
        constant = default if constant is None else float(constant)
        socket = surface.CreateInput(target, Sdf.ValueTypeNames.Float)
        if influence <= 0.0:
            socket.Set(constant)
        elif influence >= 1.0:
            socket.ConnectToSource(channel_of(target, index), "out")
        else:
            mix = UsdShade.Shader.Define(stage, base.AppendChild(f"mtlx_{target}_mix"))
            mix.CreateIdAttr("ND_mix_float")
            mix.CreateInput("bg", Sdf.ValueTypeNames.Float).Set(constant)
            mix.CreateInput("fg", Sdf.ValueTypeNames.Float).ConnectToSource(
                channel_of(target, index), "out"
            )
            mix.CreateInput("mix", Sdf.ValueTypeNames.Float).Set(influence)
            socket.ConnectToSource(mix.ConnectableAPI(), "out")


def _connect_normal(stage, material, mdl, surface):
    """Feed an OmniPBR normal map through a MaterialX normalmap node.

    Read in the working space: a normal map is vectors, so an sRGB decode would tilt every normal.
    """
    texture = _authored(mdl, "normalmap_texture")
    if texture is None:
        return

    base = material.GetPath()
    image = UsdShade.Shader.Define(stage, base.AppendChild("mtlx_normal_image"))
    image.CreateIdAttr("ND_image_vector3")
    file_input = image.CreateInput("file", Sdf.ValueTypeNames.Asset)
    file_input.Set(Sdf.AssetPath(texture.resolvedPath or texture.path))
    file_input.GetAttr().SetColorSpace(_DATA_SPACE)
    image.CreateInput("uaddressmode", Sdf.ValueTypeNames.String).Set("periodic")
    image.CreateInput("vaddressmode", Sdf.ValueTypeNames.String).Set("periodic")
    image.CreateOutput("out", Sdf.ValueTypeNames.Float3)

    normalmap = UsdShade.Shader.Define(stage, base.AppendChild("mtlx_normalmap"))
    normalmap.CreateIdAttr("ND_normalmap")
    normalmap.CreateInput("in", Sdf.ValueTypeNames.Float3).ConnectToSource(
        image.ConnectableAPI(), "out"
    )
    normalmap.CreateOutput("out", Sdf.ValueTypeNames.Float3)
    # OmniPBR's bump_factor is deliberately not applied: assets author 0.1, and scaling by
    # it flattened the map so the MaterialX look lost the surface detail the MDL shows,
    # while full strength came out deeper and darker than the MDL. Matched by eye against
    # the MDL render, not derived from the shader.
    strength = _NORMAL_STRENGTH
    # OmniPBR defaults to flip U off / flip V on, and that default is what an ordinary
    # (OpenGL, green-up) map looks right with -- MaterialX's own convention. So only a
    # departure from the default is a real flip: a negative scale on that axis.
    flip_u = _authored(mdl, "flip_tangent_u")
    flip_v = _authored(mdl, "flip_tangent_v")
    flip_u = False if flip_u is None else bool(flip_u)
    flip_v = True if flip_v is None else bool(flip_v)
    sign_x = -1.0 if flip_u else 1.0
    sign_y = 1.0 if flip_v else -1.0
    if sign_x < 0 or sign_y < 0:
        normalmap.CreateIdAttr("ND_normalmap_vector2")
        normalmap.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(
            Gf.Vec2f(sign_x * strength, sign_y * strength)
        )
    elif strength != 1.0:
        normalmap.CreateInput("scale", Sdf.ValueTypeNames.Float).Set(strength)
    surface.CreateInput("normal", Sdf.ValueTypeNames.Float3).ConnectToSource(
        normalmap.ConnectableAPI(), "out"
    )


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
        file_input = image.CreateInput("file", Sdf.ValueTypeNames.Asset)
        file_input.Set(Sdf.AssetPath(resolved))
        # Colour maps are sRGB. Without this tag V-Ray still assumes sRGB, but Storm and
        # Aurora read the map as linear and the whole material comes out too bright.
        file_input.GetAttr().SetColorSpace("srgb_texture")
        # The assets carry no explicit wrap mode and their maps are laid out in UV 0-1.
        image.CreateInput("uaddressmode", Sdf.ValueTypeNames.String).Set("periodic")
        image.CreateInput("vaddressmode", Sdf.ValueTypeNames.String).Set("periodic")
        surface.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            image.ConnectableAPI(), "out"
        )
    elif colour is not None:
        surface.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).Set(colour)

    _connect_orm(stage, material, mdl, surface)
    _connect_normal(stage, material, mdl, surface)

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

    # SimReady assets put their materials inside instanceable prims. Instance proxies are
    # read-only and their descendants ignore stronger opinions, so an mtlx surface authored
    # there would never compose. De-instance the owners (in this chunk's layer) first.
    # Instances nest (a mesh instance holding a materials instance), so peel the outermost
    # editable one each pass until no material is behind a proxy.
    while True:
        owners = set()
        for prim in Usd.PrimRange(root, Usd.TraverseInstanceProxies()):
            if prim.GetTypeName() == "Material" and (prim.IsInstance() or prim.IsInstanceProxy()):
                owner = prim
                while owner and not (owner.IsInstance() and not owner.IsInstanceProxy()):
                    owner = owner.GetParent()
                if owner:
                    owners.add(owner.GetPath())
        if not owners:
            break
        for path in owners:
            stage.GetPrimAtPath(path).SetInstanceable(False)

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
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MTLX,
        icon="sphere",
        description="""
        Gives MDL (Omniverse) materials a MaterialX surface, so other renderers shade them too.

        Assets authored for Omniverse bind their looks through MDL (OmniPBR.mdl,
        OmniGlass.mdl), and every other renderer -- V-Ray, Storm, Karma, Cycles -- finds no
        surface it can build and falls back to grey. This authors a MaterialX standard_surface
        from the MDL parameters that carry the look and connects it as the material's mtlx
        surface output, beside the MDL one rather than replacing it.
        """,
    )
