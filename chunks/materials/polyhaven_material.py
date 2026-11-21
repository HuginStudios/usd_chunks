"""Poly Haven Material chunk -- a free Poly Haven texture (CC0) as a material on prims: brick, concrete, mud.

The menu is Poly Haven's texture catalog, with pictures. The texture's colour, roughness and
normal images are downloaded once into a per-user folder (in the background in usdview; at
once in a script) and made into a UsdPreviewSurface at Material Path, which Storm and Aurora
both draw, then bound to Bind To. Tiling repeats it across the mesh's UVs.

Styles use it for their real looks: concrete debris, brick, planks, rubble ground.
"""

from __future__ import annotations

import threading
import weakref

from pxr import Sdf, UsdShade

from usd_chunks import chunk, chunk_utils, polyhaven

_STRENGTH = {"Stronger than Descendants": UsdShade.Tokens.strongerThanDescendants,
             "Weaker than Descendants": UsdShade.Tokens.weakerThanDescendants}

_catalog_loading = False
_menus: weakref.WeakSet = weakref.WeakSet()
_thumbs: dict[str, str] = {}


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _names() -> list[str]:
    return sorted(polyhaven.cached_catalog("textures"))


def _load_catalog(node) -> None:
    """The texture catalog and its pictures, fetched on a worker, for every menu."""
    global _catalog_loading
    scene = node.get_scene()
    if _catalog_loading or polyhaven.headless(scene):
        return
    _catalog_loading = True

    def work():
        try:
            names = sorted(polyhaven.catalog("textures"))
        except Exception:  # noqa: BLE001 - offline: the menu keeps what it has
            names = []
        for name in names:
            try:
                _thumbs[name] = str(polyhaven.thumbnail(name))
            except Exception:  # noqa: BLE001 - a missing picture is only a missing picture
                continue

        def ready():
            global _catalog_loading
            _catalog_loading = False
            for menu in list(_menus):
                menu.set_items(names)
                menu.item_images = dict(_thumbs)

        scene.call_later(ready)

    threading.Thread(target=work, daemon=True).start()


def menu_items(node, param_name):
    """The texture catalog for the Texture menu (chunk.Menu.refresh_items)."""
    if param_name != "texture":
        return None
    if (menu := node.get_param("texture")) is not None:
        _menus.add(menu)
    names = _names()
    if not names:
        _load_catalog(node)
        return None
    return names


def execute(node) -> None:
    stage = node.get_stage()
    if stage is None:
        return
    is_f, is_s = chunk.is_param_t(float), chunk.is_param_t(str)
    name = (node.get_param("texture", is_s).value or "").strip()
    res = node.get_param("resolution", is_s).value or "1k"
    if not name:
        _report(node, "pick a texture")
        return
    files = polyhaven.cached_texture(name, res)
    if files is None:
        if not polyhaven.headless(node.get_scene()):
            polyhaven.fetch(node, ("texture", name, res), lambda: polyhaven.download_texture(name, res), _report)
            _report(node, f"downloading {name} from polyhaven.com…")
            return
        try:
            files = polyhaven.download_texture(name, res)
        except Exception as exc:  # noqa: BLE001 - offline, or no such texture
            _report(node, f"download failed: {exc}")
            return
    path = (node.get_param("material_path", is_s).value or "").strip() or f"/materials/{name}"
    tiling = max(1e-3, node.get_param("tiling", is_f).value)
    material = polyhaven.author_texture_look(stage, path, {k: v.as_posix() for k, v in files.items()}, tiling,
                                             node.get_param("uv", is_s).value or "st")
    strength = _STRENGTH.get(node.get_param("strength", is_s).value, UsdShade.Tokens.strongerThanDescendants)
    bound = 0
    for target in chunk_utils.resolve_prim_paths(stage, node.get_param("primpath_location", is_s).value or ""):
        prim = stage.GetPrimAtPath(target)
        if not prim:
            continue
        UsdShade.MaterialBindingAPI.Apply(stage.OverridePrim(Sdf.Path(target))).Bind(material, strength)
        bound += 1
    _report(node, f"{name} ({res}) at {path}" + (f" · on {bound} prim{'s' if bound != 1 else ''}" if bound else ""))


def _on_loaded(owner, notice, sender):
    if (menu := owner.get_param("texture")) is not None:
        _menus.add(menu)
        if _names():
            menu.set_items(_names())
            menu.item_images = dict(_thumbs)
    _load_catalog(owner)


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Poly Haven Material")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api.add_param(chunk.ImageMenu(
        "texture", "Texture", tile_size=96, dynamic_items=True, flags=labeled,
        tooltip="A free texture from polyhaven.com (CC0). Downloaded once, then read from your cache.",
    ))
    chunk_api.add_param(chunk.Menu("resolution", "Resolution", menu_items=list(polyhaven.RESOLUTIONS),
                                   default_value="1k", flags=labeled))
    chunk_api.add_param(chunk.LineEdit(
        "primpath_location", "Bind To", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prims that get the material. Comma-separate several. Empty: the material only.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "material_path", "Material Path", flags=labeled,
        tooltip="Where the material is written. Empty: /materials/<texture>.",
    ))
    chunk_api.add_param(chunk.Slider("tiling", "Tiling", min_value=0.1, max_value=50.0, default_value=1.0,
                                     tooltip="How many times the texture repeats across the mesh's UVs."))
    chunk_api.add_param(chunk.Menu("strength", "Strength", menu_items=list(_STRENGTH),
                                   default_value="Stronger than Descendants", flags=labeled,
                                   tooltip="Stronger: beats the materials bound below the prim, such as an "
                                   "asset's own. Weaker: they keep theirs."))
    chunk_api.add_param(chunk.LineEdit("uv", "UV Primvar", default_value="st", flags=labeled))
    chunk_api.add_param(chunk.Label("readout", "pick a texture", default_value="pick a texture"))
    chunk_api.add_callback(_on_loaded, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MATERIALS,
        icon="palette",
        description="""
        Puts a free Poly Haven texture on prims as a material: brick, concrete, mud (CC0).

        Pick a texture from the picture menu; its colour, roughness and normal images are
        downloaded once into your usd_chunks cache (in the background in usdview) and made
        into a UsdPreviewSurface at Material Path, bound to Bind To. Tiling repeats it across
        the mesh's UVs. Storm and Aurora both draw it.
        """,
    )
