from __future__ import annotations
import os
import re
import json
import zipfile
import io
import logging
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor

import requests

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_core
from usd_chunks.chunk_stage import chunk_prim_edit_context, connect_exec, get_chunk_prim_path

logger = logging.getLogger(__name__)

API_ROOT = "https://api.matlib.gpuopen.com/api"
CACHE_FILE_NAME = "gpuopen_materials.json"
# Dropped into an extracted package dir when it was downloaded as the fallback for a
# resolution the library does not offer -- see _cached_mtlx.
_FALLBACK_MARKER = ".fallback_for_{resolution}"

_all_materials: list[dict] = []
_results_by_title: dict[str, dict] = {}
_downloading: set[str] = set()  # titles with an in-flight background download
_catalog_loading = False  # a catalog fetch is in flight; one per session is enough
# Every live Material menu. The catalog and thumbnails are per session, not per node, so
# they are handed to every library node's menu as they land.
_menus: weakref.WeakSet = weakref.WeakSet()
_thumb_paths: dict[str, str] = {}  # title → thumbnail file on disk
_thumbs_flush_pending = False

THUMB_SIZE = 85
THUMB_DIR_NAME = "thumbs"
#: How long thumbnails gather before the menus are told: one redraw per batch, not per file.
_THUMB_FLUSH_DELAY = 0.3

# Thumbnails go through one session and a bounded pool. A thread and a bare requests.get
# per tile would mean a fresh SSL context, a DNS lookup and a TCP handshake per thumbnail,
# and hundreds of threads at once on a scene load, all contending for the GIL with the
# main thread that is trying to cook the graph.
_thumb_http = requests.Session()
_thumb_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gpuopen-thumb")
_thumb_futures: dict[str, Future] = {}  # render_id → in-flight fetch, shared by all nodes
_thumb_lock = threading.Lock()


def _safe_prim_name(s: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", s)
    if name and name[0].isdigit():
        name = "_" + name
    return name


def _fetch_all_materials(cache_dir: str) -> list[dict]:
    cache_file = os.path.join(cache_dir, CACHE_FILE_NAME)
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    all_materials = []
    url = f"{API_ROOT}/materials/?limit=100"
    while url:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        all_materials.extend(data.get("results", []))
        url = data.get("next")

    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(all_materials, f, indent=2)
    return all_materials


# Async thumbnail loader


def _thumb_path(cache_dir: str, render_id: str) -> str:
    return os.path.join(cache_dir, THUMB_DIR_NAME, f"{render_id}.jpg")


def _fetch_thumbnail(render_id: str, cache_dir: str) -> bytes:
    """Worker half: disk cache, else one GET on the shared session. b"" on any failure.

    The disk cache is what makes a reopen free: the in-memory dict is empty on every
    startup, so without it every tile of every node re-downloaded its thumbnail each time
    the scene was opened.
    """
    path = _thumb_path(cache_dir, render_id)
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        pass
    try:
        r = _thumb_http.get(f"{API_ROOT}/renders/{render_id}/download_thumbnail/", timeout=10)
    except Exception:
        return b""
    if r.status_code != 200:
        return b""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(r.content)
    except OSError:
        pass
    return r.content


# The catalog and its thumbnails, for every library node's Material menu (an ImageMenu:
# Qt draws it as a grid, Blender as its thumbnail picker, anything else as a dropdown).


def _render_id(material: dict) -> str:
    render_ids = material.get("renders_order") or material.get("renders") or []
    return render_ids[0] if render_ids else ""


def _titles() -> list[str]:
    return [m.get("title", "") for m in _all_materials if m.get("title")]


def _load_catalog(node) -> None:
    """Fetch the catalog on a worker, then hand it to every Material menu on the main thread."""
    global _catalog_loading
    if _all_materials or _catalog_loading:
        return
    scene = node.get_scene()
    if scene is None:
        return
    cache_param = node.get_param("cache_dir", chunk.is_param_t(str))
    cache_dir = (cache_param.value if cache_param else "") or os.path.expanduser("~/.gpuopen_cache")
    _catalog_loading = True

    def _work():
        try:
            materials = _fetch_all_materials(cache_dir)
        except Exception:
            logger.warning("Could not fetch the AMD MaterialX catalog", exc_info=True)
            materials = []
        scene.call_later(lambda: _catalog_ready(materials, cache_dir, scene))

    threading.Thread(target=_work, daemon=True).start()


def _catalog_ready(materials: list, cache_dir: str, scene) -> None:
    global _all_materials, _results_by_title, _catalog_loading
    _catalog_loading = False
    if not materials:
        return
    _all_materials = materials
    _results_by_title = {m["title"]: m for m in materials if m.get("title")}
    titles = _titles()
    for menu in list(_menus):
        menu.set_items(titles)
    _fetch_thumbnails(cache_dir, scene)


def _fetch_thumbnails(cache_dir: str, scene) -> None:
    """Every material's thumbnail, on the bounded pool; already on disk costs a stat.

    The current picks go first, so the pictures a user is looking at arrive before the
    rest of the catalog's.
    """
    picked = {menu.value for menu in _menus if menu.value}
    ordered = sorted(_all_materials, key=lambda m: m.get("title") not in picked)
    for material in ordered:
        title, rid = material.get("title"), _render_id(material)
        if not title or not rid or title in _thumb_paths:
            continue
        path = _thumb_path(cache_dir, rid)
        if os.path.isfile(path):
            _thumb_paths[title] = path
            continue
        with _thumb_lock:
            if rid in _thumb_futures:
                continue
            future = _thumb_pool.submit(_fetch_thumbnail, rid, cache_dir)
            _thumb_futures[rid] = future
        future.add_done_callback(
            lambda f, t=title, r=rid, p=path: _thumbnail_done(f, t, r, p, scene))
    _flush_thumbnails()


def _thumbnail_done(future: Future, title: str, rid: str, path: str, scene) -> None:
    """Pool thread: note the file, and ask the main thread to pass the batch on."""
    global _thumbs_flush_pending
    with _thumb_lock:
        _thumb_futures.pop(rid, None)
        if future.exception() is not None or not future.result():
            return
        _thumb_paths[title] = path
        if _thumbs_flush_pending:
            return
        _thumbs_flush_pending = True
    scene.call_later(_flush_thumbnails, _THUMB_FLUSH_DELAY)


def _flush_thumbnails() -> None:
    """Main thread: give every Material menu the pictures so far."""
    global _thumbs_flush_pending
    with _thumb_lock:
        _thumbs_flush_pending = False
        images = dict(_thumb_paths)
    for menu in list(_menus):
        if menu.item_images != images:
            menu.item_images = images


def menu_items(node, param_name):
    """The catalog's titles for the Material menu (chunk.Menu.refresh_items).

    The first ask starts the fetch and leaves the menu as it is; the titles arrive in
    _catalog_ready, for every node at once.
    """
    if param_name != "material":
        return None
    menu = node.get_param("material")
    if menu is not None:
        _menus.add(menu)
    if not _all_materials:
        _load_catalog(node)
        return None
    return _titles()


# Chunk callbacks


def _on_chunk_loaded(owner, notice, sender):
    """Start the catalog now, so the Material menu is full by the time anyone opens it."""
    menu = owner.get_param("material")
    if menu is not None:
        _menus.add(menu)
        # A network saved before the menu existed knows only what it applied: show that
        # as the pick, so the picture on the node is the material on the prim.
        picked = owner.get_param("picked_title").value
        if not menu.value and picked:
            menu.value = picked
        if _all_materials:
            menu.set_items(_titles())
            menu.item_images = dict(_thumb_paths)
    _load_catalog(owner)


def _pick_package(package_ids: list[str], resolution: str) -> dict | None:
    for pkg_id in package_ids:
        resp = requests.get(f"{API_ROOT}/packages/{pkg_id}", timeout=15)
        if resp.status_code != 200:
            continue
        pkg = resp.json()
        if pkg.get("label", "").lower().startswith(resolution.lower()):
            return pkg
    for pkg_id in package_ids:
        resp = requests.get(f"{API_ROOT}/packages/{pkg_id}", timeout=15)
        if resp.status_code == 200:
            return resp.json()
    return None


def _child_of_type(parent, module_key: str):
    """Parent's first child chunk of this module, whatever it ended up being called.

    By type, not by name: a child is only called "Reference" if nothing else took that
    name first, and looking it up by name made a second one whenever it had been renamed.
    """
    for param in parent.get_my_params():
        cfg = param.get_config() if isinstance(param, chunk.Chunk) else None
        if cfg is not None and cfg.module == module_key:
            return param
    return None


def _create_child(module_key: str, parent):
    """Create a child chunk and attach it to parent inside the user's edit context.

    add_param is called inside chunk_prim_edit_context() so that
    the Chunk prim authored by _define_prim lands on the user's persistent edit
    layer (session layer), not on the parent context's anonymous execute layer.
    """
    with chunk_prim_edit_context():
        mod = chunk_registry.ChunkRegistry().get_chunk_module(module_key)
        if not mod:
            return None
        created = mod.create() if mod.loaded else mod.load()
        child = created[0] if created else None
        if child:
            parent.add_param(child)
    return child


def _cached_mtlx(cache_dir: str, title: str, resolution: str) -> str | None:
    """Path to an already-extracted .mtlx for this material+resolution, or None.

    _download_and_apply extracts each package to <cache_dir>/<Title>_<label>, where a
    label starts with its resolution ("1k_8b", "2k_16b", ...). A dir at the requested
    resolution is a hit. A dir at another resolution is a hit only if _fetch_package left a
    _FALLBACK_MARKER for this resolution in it -- meaning the library had no package at the
    requested resolution and that dir IS what the download would fall back to again. Without
    that, a material that only ships at 1k (Glass, White Plastic) missed the cache on every
    single load: the lookup asked for "<Title>_2k", the download then extracted into
    "<Title>_1k_8b" again, and the next load repeated the whole thing. The marker keeps a
    genuine resolution change honest: picking 4k for a material cached at 1k still misses
    and downloads, unless a previous download already found there is no 4k.
    """
    if not cache_dir or not os.path.isdir(cache_dir):
        return None
    safe_title = re.escape(_safe_prim_name(title))
    exact = re.compile(rf"^{safe_title}_{re.escape(resolution)}")
    any_res = re.compile(rf"^{safe_title}_\d+k(?:_|$)")
    candidates = [e for e in sorted(os.listdir(cache_dir)) if any_res.match(e)]
    candidates.sort(key=lambda e: not exact.match(e))
    for entry in candidates:
        mat_dir = os.path.join(cache_dir, entry)
        if not os.path.isdir(mat_dir):
            continue
        if not exact.match(entry) and not os.path.exists(
            os.path.join(mat_dir, _FALLBACK_MARKER.format(resolution=resolution))
        ):
            continue
        for root, _dirs, files in os.walk(mat_dir):
            for name in sorted(files):
                if name.endswith(".mtlx"):
                    return os.path.join(root, name).replace("\\", "/")
    return None


def _fetch_package(title: str, resolution: str, cache_dir: str) -> tuple[str | None, dict | None, str | None]:
    """Make sure this material's package is on disk. Returns (mtlx_path, mat, error).

    Network and filesystem only -- deliberately touches no USD and no Qt, so it is safe to
    run off the main thread (see apply_material, which does exactly that so the Download &
    Assign button cannot freeze usdview for the length of a download).
    """
    mat = _results_by_title.get(title) or next(
        (m for m in _fetch_all_materials(cache_dir) if m.get("title") == title), None
    )
    if not mat:
        return None, None, f"Material '{title}' not found in catalog"

    # Reuse an already-extracted package before touching the network. _download_and_apply is
    # a reactive hook: it re-runs on every scene load and on every recook (diving into the
    # node and back out is two), so a package-metadata lookup plus a full zip download
    # every time would dominate scene load and every dive. The extracted directory is named
    # after the resolution (the package label starts with it, see _pick_package), so a cache
    # hit here still honours a resolution change -- that just misses and falls through to
    # the download.
    mtlx_path = _cached_mtlx(cache_dir, title, resolution)
    if mtlx_path:
        return mtlx_path, mat, None

    try:
        pkg = _pick_package(mat.get("packages", []), resolution)
    except Exception as exc:
        return None, None, f"Package lookup failed: {exc}"

    if not pkg:
        return None, None, "No package found"
    file_url = pkg.get("file_url")
    if not file_url:
        return None, None, "Package has no download URL"

    mat_dir = os.path.join(cache_dir, _safe_prim_name(f"{title}_{pkg.get('label', resolution)}"))
    os.makedirs(mat_dir, exist_ok=True)

    try:
        resp = requests.get(file_url, timeout=120)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            z.extractall(mat_dir)
            names = z.namelist()
    except Exception as exc:
        return None, None, f"Download failed: {exc}"

    # Record that this dir answers for the requested resolution too, so _cached_mtlx can
    # hit it next time instead of re-downloading the same fallback package.
    if not pkg.get("label", "").lower().startswith(resolution.lower()):
        with open(os.path.join(mat_dir, _FALLBACK_MARKER.format(resolution=resolution)), "w"):
            pass

    mtlx_path = next(
        (os.path.join(mat_dir, n).replace("\\", "/") for n in names if n.endswith(".mtlx")),
        None,
    )
    if not mtlx_path:
        return None, None, "No .mtlx file in package"
    return mtlx_path, mat, None


def _download_and_apply(node, title: str) -> str:
    resolution = node.get_param("resolution", chunk.is_inst(chunk.Menu)).value
    mat_scope = (node.get_param("mat_scope", chunk.is_param_t(str)).value or "/materials").rstrip("/")
    cache_dir = node.get_param("cache_dir", chunk.is_param_t(str)).value or os.path.expanduser("~/.gpuopen_cache")

    mtlx_path, mat, error = _fetch_package(title, resolution, cache_dir)
    if error:
        return error

    mat_name = _safe_prim_name(title)
    mat_path = f"{mat_scope}/{mat_name}"
    mtlx_mat_name = mat.get("mtlx_material_name", mat_name)

    # Children must be added to the CONTEXT *before* their param values are set.
    # Setting a value triggers an immediate self-execute (reference.execute /
    # bind_material.execute), which clears the stage's current edit-target layer
    # on the assumption it owns a dedicated layer -- a layer that add_param's
    # CONTEXT branch is what creates. Setting values first leaves that Clear()
    # hitting whatever layer happens to be the edit target instead (e.g. the
    # parent's), wiping the child's own params before they're ever read back.
    ref_chunk = _child_of_type(node, "reference")
    if not ref_chunk:
        ref_chunk = _create_child("reference", node)
    if ref_chunk:
        # file_path's file_changed callback auto-derives primpath_location from
        # the filename, so set file_path first -- otherwise that auto-suggestion
        # overwrites the explicit mat_path we set below.
        ref_chunk.get_param("file_path").value = mtlx_path
        ref_chunk.get_param("import_primpath").value = f"/MaterialX/Materials/{mtlx_mat_name}"
        ref_chunk.get_param("prim_type").value = "Material"
        ref_chunk.get_param("primpath_location").value = mat_path

    bind_chunk = _child_of_type(node, "bind_material")
    if not bind_chunk:
        bind_chunk = _create_child("bind_material", node)
    if bind_chunk:
        bind_chunk.get_param("material_path").value = mat_path
        bind_chunk.get_param("primpath_location").value = node.get_param("target_primpath").value

    # Wire Reference -> BindMaterial into the exec chain. Both are created here as bare,
    # disconnected chunk prims; view-driven execution (chunk_stage.recook_for_view) mutes
    # anything in this subnet not reachable from its inner view's upstream tree, so an
    # unwired child's authored content silently disappears the first time the subnet
    # recooks. The graph UI's own default-view logic (ChunkGraphView._ensure_default_view)
    # already auto-wires a view to whichever disconnected chunk it finds as the subnet's
    # tail -- which, with two disconnected children, was landing on BindMaterial only,
    # leaving Reference unreachable and looking (and behaving) like it was never connected.
    # Chaining them here means only BindMaterial is ever a tail, so that same auto-view
    # logic picks it up correctly with Reference upstream of it, without this module
    # needing to know anything about views itself.
    if ref_chunk and bind_chunk:
        stage = node.get_stage()
        if stage:
            connect_exec(
                stage,
                chunk_registry.ChunkRegistry().scene,
                get_chunk_prim_path(ref_chunk),
                get_chunk_prim_path(bind_chunk),
            )

    node.get_param("resolved_material_path").value = mat_path
    return f"Ready — {mat_path}"


def execute(node):
    """Re-applies the stored picked material, (re)creating the Reference and
    BindMaterial children if they're missing -- e.g. after a reload where the
    USD prims exist but their backing Python child chunks weren't reattached.
    """
    title = node.get_param("picked_title").value
    if not title:
        return
    status_param = node.get_param("status", chunk.is_inst(chunk.Label))
    if status_param:
        status_param.value = f"Downloading {title}…"
    result = _download_and_apply(node, title)
    if status_param:
        status_param.value = result


def _set_status(owner, text: str) -> None:
    status_param = owner.get_param("status", chunk.is_inst(chunk.Label))
    if status_param:
        status_param.value = text


def apply_material(owner, _notice, _sender):
    """Download & Assign, without freezing the host.

    Only the DOWNLOAD runs on a worker thread; every USD and Qt touch stays on the main
    thread. A download is seconds of network on a cold cache, and doing it inline blocked
    usdview's event loop for the whole time -- no viewport, no interaction, looking hung.

    The split is possible because the two halves are cleanly separable: _fetch_package is
    pure network/filesystem, and the assign is pure USD. So the worker only gets the
    package onto disk, then hands back to the main thread (the host's call_later, read
    here on the main thread before the worker starts) to set picked_title and run the normal
    reactive path. That path calls _fetch_package again, but by then it is a cache hit, so
    it costs nothing and there is exactly one code path that ever authors USD.
    """
    title = owner.get_param("material").value or owner.get_param("picked_title").value or ""
    if not title:
        _set_status(owner, "Select a material first")
        return
    scene = owner.get_scene()
    if scene is None:
        return

    if title in _downloading:
        _set_status(owner, f"Already downloading {title}…")
        return

    # Read every param HERE, on the main thread -- the worker must not touch the chunk.
    resolution = owner.get_param("resolution", chunk.is_inst(chunk.Menu)).value
    cache_dir = owner.get_param("cache_dir", chunk.is_param_t(str)).value or os.path.expanduser(
        "~/.gpuopen_cache"
    )
    _set_status(owner, f"Downloading {title}…")
    _downloading.add(title)
    # Weak, so a node deleted mid-download does not get resurrected by the callback.
    owner_ref = weakref.ref(owner)

    def _work():
        try:
            _, _, error = _fetch_package(title, resolution, cache_dir)
        except Exception as exc:  # never let a worker die silently with the title latched
            error = f"Download failed: {exc}"

        def _finish():
            _downloading.discard(title)
            node = owner_ref()
            if node is None:
                return
            if error:
                _set_status(node, error)
                return
            # Cache is warm now, so this is the fast path.
            node.get_param("picked_title").value = title
            execute(node)

        # Hand back to the main thread: usdview queues it on Qt's loop, Blender on its
        # timers. Never _finish() here -- this is the worker, and it must not touch USD.
        scene.call_later(_finish)

    threading.Thread(target=_work, daemon=True).start()


# Chunk definition


def main():
    chunk_api = chunk.Chunk(
        "AMD MaterialX Library",
        flags=(
            chunk_core.ChunkFlags.GroupFlags.CONTEXT
            | chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE
            | chunk_core.ChunkFlags.GroupFlags.LABELED
        ),
    )

    prim_path = chunk.LineEdit(
        "target_primpath",
        "Prim Path",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    material = chunk.ImageMenu(
        "material", "Material", tile_size=THUMB_SIZE, dynamic_items=True,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="The GPUOpen MaterialX Library. Pick one, then Download & Assign.",
    )

    resolution = chunk.Menu("resolution", "Resolution", menu_items=["2k", "1k", "4k"], default_value="2k")
    status = chunk.Label("status", default_value="")
    apply_btn = chunk.Button("apply_btn", "Download & Assign")

    advanced = chunk.Group(
        "advanced",
        "Advanced",
        flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
    )
    mat_scope = chunk.LineEdit(
        "mat_scope",
        "Material Scope",
        default_value="/",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    advanced.set_state(chunk_core.ChunkFlags.State.COLLAPSED)
    cache_dir = chunk.LineEdit(
        "cache_dir",
        "Cache Dir",
        default_value=os.path.expanduser("~/.gpuopen_cache").replace("\\", "/"),
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    advanced.add_param(cache_dir)

    picked_title = chunk.LineEdit(
        "picked_title",
        "Picked Material",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    material_path = chunk.LineEdit(
        "resolved_material_path",
        "Material Path",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )

    chunk_api.add_params([prim_path, mat_scope, material, resolution, picked_title, material_path, status, advanced, apply_btn])

    chunk_api.add_callback(
        _on_chunk_loaded,
        notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED,
    )
    chunk_api.add_callback(
        apply_material,
        sender=apply_btn,
    )

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MATERIALS,
        icon="sphere",
        requirements=["requests>=2,<3"],
        description="""
        Assign Material chunk: browses the AMD GPUOpen MaterialX library and
        downloads and binds a material to one or more prims on the active stage.

        Parameters:
          - Prim Path: comma-separated list of stage prim paths to bind the
            material to. Supports prim selection and variant context authoring.
          - Material Grid: scrollable tile browser of the GPUOpen catalog.
            Fetched asynchronously on load and filtered by the search field.
            Thumbnails are loaded lazily as tiles come into view.
          - Resolution: preferred texture resolution (2k default, 1k, 4k).
            Falls back to any available package if the requested resolution
            is not found.
          - Status: read-only label showing download/bind progress.
          - Material Scope: USD scope prim under which the material is defined
            (defaults to /materials).

        Advanced:
          - Cache Dir: local directory for the material catalog JSON and
            downloaded material zip files (~/.gpuopen_cache by default).

        On apply, the chunk resolves the best matching package, downloads and
        extracts the zip, defines a UsdShade.Material prim, references the
        extracted .mtlx file into it, and binds it to each target prim with
        strongerThanDescendants strength.
        """,
    )
