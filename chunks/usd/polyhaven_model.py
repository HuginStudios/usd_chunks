"""Poly Haven Model chunk -- brings in a free Poly Haven model (CC0) by name: rocks, plants, props.

The menu is Poly Haven's catalog, with pictures. The model is downloaded once into a per-user
folder (in the background in usdview; at once in a script) and referenced at Location. Pick
one Part of a set (rock_moss_set_01's rocks) or All. Its MaterialX looks render black in
Storm and crash Aurora, so each material is rebuilt as a UsdPreviewSurface from the model's
own textures, under <Location>/Looks.

Styles use it for their real assets (usd_chunks/styles.py), as hidden prototypes a Scatter
copies.
"""

from __future__ import annotations

import logging
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

from pxr import Sdf, UsdGeom, UsdShade

from usd_chunks import chunk, polyhaven

logger = logging.getLogger(__name__)

ALL = "All"

_catalog_loading = False
_menus: weakref.WeakSet = weakref.WeakSet()
_thumbs: dict[str, str] = {}
_thumb_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="polyhaven-thumb")
_models: dict[str, tuple[float, polyhaven.Model]] = {}


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _model_info(path) -> polyhaven.Model:
    """read_model, once per file version."""
    key, stamp = str(path), path.stat().st_mtime
    if key not in _models or _models[key][0] != stamp:
        _models[key] = (stamp, polyhaven.read_model(path))
    return _models[key][1]


def _names() -> list[str]:
    return sorted(polyhaven.cached_catalog())


def _load_catalog(node) -> None:
    """Fetch the catalog and its pictures on workers; hand them to every menu on the main thread."""
    global _catalog_loading
    scene = node.get_scene()
    if _catalog_loading or scene is None or polyhaven.headless(scene):
        return
    _catalog_loading = True

    def work():
        try:
            names = sorted(polyhaven.catalog())
        except Exception:  # noqa: BLE001 - offline: the menu keeps what it has
            logger.warning("Could not fetch the Poly Haven catalog", exc_info=True)
            names = []
        scene.call_later(lambda: _catalog_ready(names, scene))

    threading.Thread(target=work, daemon=True).start()


def _catalog_ready(names: list[str], scene) -> None:
    global _catalog_loading
    _catalog_loading = False
    for menu in list(_menus):
        menu.set_items(names)
    lock = threading.Lock()

    def fetched(name, future):
        if future.exception() is None:
            with lock:
                _thumbs[name] = str(future.result())

    futures = [(_thumb_pool.submit(polyhaven.thumbnail, n), n) for n in names if n not in _thumbs]
    for future, name in futures:
        future.add_done_callback(lambda f, n=name: fetched(n, f))

    def flush():
        with lock:
            images = dict(_thumbs)
        for menu in list(_menus):
            if menu.item_images != images:
                menu.item_images = images
        if any(not f.done() for f, _ in futures):
            scene.call_later(flush, 1.0)

    scene.call_later(flush, 1.0)


def menu_items(node, param_name):
    """The catalog for Model, the downloaded model's parts for Part (chunk.Menu.refresh_items)."""
    if param_name == "model":
        if (menu := node.get_param("model")) is not None:
            _menus.add(menu)
        names = _names()
        if not names:
            _load_catalog(node)
            return None
        return names
    if param_name == "part":
        name = node.get_param("model").value
        path = polyhaven.cached_model(name, node.get_param("resolution").value) if name else None
        return [ALL, *(_model_info(path).parts if path else [])]
    return None


def execute(node) -> None:
    stage = node.get_stage()
    if stage is None:
        return
    is_s, is_b = chunk.is_param_t(str), chunk.is_param_t(bool)
    name = (node.get_param("model", is_s).value or "").strip()
    res = node.get_param("resolution", is_s).value or "1k"
    if not name:
        _report(node, "pick a model")
        return
    location = (node.get_param("primpath_location", is_s).value or "").strip() or f"/{name}"
    path = polyhaven.cached_model(name, res)
    if path is None:
        if not polyhaven.headless(node.get_scene()):
            polyhaven.fetch(node, ("model", name, res), lambda: polyhaven.download_model(name, res), _report)
            _report(node, f"downloading {name} from polyhaven.com…")
            return
        try:
            path = polyhaven.download_model(name, res)
        except Exception as exc:  # noqa: BLE001 - offline, or no such model
            _report(node, f"download failed: {exc}")
            return
    info = _model_info(path)
    part = node.get_param("part", is_s).value or ALL
    if part != ALL and part not in info.parts:
        part = info.parts[0] if len(info.parts) == 1 else ALL
    source = info.root if part == ALL else f"{info.root}/{part}"

    prim = UsdGeom.Xform.Define(stage, location).GetPrim()
    prim.GetReferences().AddReference(Sdf.Reference(str(path), source))
    if node.get_param("at_origin", is_b).value:
        # Poly Haven lays a set's parts out side by side; without their own placement each
        # sits at the origin, where a Scatter's prototype should be. All of a set: every part
        # there, so a Scatter row of <Location>/<model>* copies each one as its own prototype.
        for target in ([prim] if part != ALL else [stage.OverridePrim(f"{location}/{p}") for p in info.parts]):
            UsdGeom.Xformable(target).ClearXformOpOrder()

    prefix = "" if part == ALL else part
    looks = 0
    for material, prims in sorted(info.bound.items()):
        inside = [p for p in prims if part == ALL or p == prefix or p.startswith(prefix + "/")]
        if not inside:
            continue
        look = polyhaven.author_look(stage, f"{location}/Looks/{material}", info.textures.get(material, {}),
                                     info.uv)
        for rel in inside:
            rel = rel[len(prefix):].lstrip("/") if prefix else rel
            target = stage.OverridePrim(f"{location}/{rel}" if rel and rel != "." else location)
            UsdShade.MaterialBindingAPI.Apply(target).Bind(look)
        looks += 1
    parts = f"{part} of {len(info.parts)} parts" if info.parts else part
    _report(node, f"{name} ({res}) · {parts} · {looks} look{'s' if looks != 1 else ''}")


def _on_loaded(owner, notice, sender):
    """Start the catalog, so the menu has its pictures by the time anyone opens it."""
    if (menu := owner.get_param("model")) is not None:
        _menus.add(menu)
        if _names():
            menu.set_items(_names())
            menu.item_images = dict(_thumbs)
    _load_catalog(owner)


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Poly Haven Model")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api.add_param(chunk.ImageMenu(
        "model", "Model", tile_size=96, dynamic_items=True, flags=labeled,
        tooltip="A free model from polyhaven.com (CC0). Downloaded once, then read from your cache.",
    ))
    chunk_api.add_param(chunk.Menu(
        "part", "Part", menu_items=[ALL], default_value=ALL, dynamic_items=True, flags=labeled,
        tooltip="One part of a set (a rock of a rock set), or All of it as Poly Haven lays it out.",
    ))
    chunk_api.add_param(chunk.Menu("resolution", "Textures", menu_items=list(polyhaven.RESOLUTIONS),
                                   default_value="1k", flags=labeled))
    chunk_api.add_param(chunk.LineEdit(
        "primpath_location", "Location", flags=labeled,
        tooltip="Where the model goes. Empty: /<model>.",
    ))
    chunk_api.add_param(chunk.Checkbox(
        "at_origin", "Part at the Origin", default_value=True, flags=labeled,
        tooltip="Each part without the place Poly Haven gave it in the set: at the origin, ready to be "
        "copied. With All, every part is there: a Scatter row of <Location>/<model>* copies each one.",
    ))
    chunk_api.add_param(chunk.Label("readout", "pick a model", default_value="pick a model"))
    chunk_api.add_callback(_on_loaded, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="download",
        description="""
        Brings in a free Poly Haven model by name: rocks, plants, branches, crates (CC0).

        Pick a model from the picture menu; it is downloaded once into your usd_chunks
        cache (in the background in usdview) and referenced at Location. Part picks one piece
        of a set, put at the origin, ready for a Scatter to copy. The model's looks are made
        UsdPreviewSurface from its own textures, so Storm and Aurora draw them.
        """,
    )
