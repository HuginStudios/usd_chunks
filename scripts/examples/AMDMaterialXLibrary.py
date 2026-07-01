import os
import re
import json
import zipfile
import io
import threading
import requests

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_core
from plugins.chunk_usd import ChunkStageSync
from PySide6 import QtWidgets, QtCore, QtGui

API_ROOT = "https://api.matlib.gpuopen.com/api"
CACHE_FILE_NAME = "gpuopen_materials.json"

_all_materials: list[dict] = []
_results_by_title: dict[str, dict] = {}
_selected_title: str = ""
_thumbnail_cache: dict[str, bytes] = {}  # render_id → jpeg bytes

THUMB_SIZE = 90


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


class _ThumbnailLoader(QtCore.QObject):
    loaded = QtCore.Signal(str, bytes)  # render_id, jpeg bytes

    def fetch(self, render_id: str):
        def _work():
            if render_id in _thumbnail_cache:
                self.loaded.emit(render_id, _thumbnail_cache[render_id])
                return
            try:
                url = f"{API_ROOT}/renders/{render_id}/download_thumbnail/"
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    _thumbnail_cache[render_id] = r.content
                    self.loaded.emit(render_id, r.content)
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()


# Thumbnail tile


class _FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._items: list[QtWidgets.QLayoutItem] = []
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def clear(self):
        while self._items:
            item = self.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QtCore.QRect(0, 0, width, 0), dry_run=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, dry_run=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())

    def expandingDirections(self):
        return QtCore.Qt.Orientations(QtCore.Qt.Orientation(0))

    def _layout(self, rect: QtCore.QRect, dry_run: bool) -> int:
        sp = self.spacing()
        x, y, row_h = rect.x(), rect.y(), 0
        for item in self._items:
            w = item.sizeHint().width()
            h = item.sizeHint().height()
            if x + w > rect.right() and row_h:
                x = rect.x()
                y += row_h + sp
                row_h = 0
            if not dry_run:
                item.setGeometry(QtCore.QRect(x, y, w, h))
            x += w + sp
            row_h = max(row_h, h)
        return y + row_h - rect.y()


class _MaterialTile(QtWidgets.QFrame):
    clicked_title = QtCore.Signal(str)

    def __init__(self, title: str, render_id: str, loader: _ThumbnailLoader):
        super().__init__()
        self._title = title
        self._render_id = render_id

        self.setFixedWidth(THUMB_SIZE + 8)
        self.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        self._thumb = QtWidgets.QLabel()
        self._thumb.setFixedSize(THUMB_SIZE, THUMB_SIZE)
        self._thumb.setScaledContents(True)
        self._thumb.setStyleSheet("background:#333;")
        layout.addWidget(self._thumb)

        lbl = QtWidgets.QLabel(title)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(THUMB_SIZE + 4)
        lbl.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(lbl)

        self.setSelected(False)

        if render_id:
            loader.loaded.connect(self._on_loaded)
            loader.fetch(render_id)

    def _on_loaded(self, render_id: str, data: bytes):
        if render_id == self._render_id:
            px = QtGui.QPixmap()
            px.loadFromData(data)
            self._thumb.setPixmap(px)

    def setSelected(self, selected: bool):
        color = "#4a90d9" if selected else "#2a2a2a"
        self.setStyleSheet(f"QFrame {{ border: 2px solid {color}; border-radius: 3px; }}")

    def mousePressEvent(self, event):
        self.clicked_title.emit(self._title)
        super().mousePressEvent(event)


# Grid widget


class MaterialGridWidget(chunk_core.ChunkParamWidgetBase, QtWidgets.QWidget):
    _catalog_ready = QtCore.Signal(list)

    def __init__(self, param: chunk.Custom):
        super().__init__(param)

        self._loader = _ThumbnailLoader()
        self._tiles: dict[str, _MaterialTile] = {}
        self._page_items: list[tuple[str, dict]] = []
        self._page_offset = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Search MaterialX Library…")
        outer.addWidget(self._search)

        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        container = QtWidgets.QWidget()
        self._flow = _FlowLayout(container, spacing=4)
        self._scroll.setWidget(container)
        outer.addWidget(self._scroll)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._do_search)
        self._search.textChanged.connect(self._timer.start)

        self._catalog_ready.connect(self._on_catalog_ready)

        self.setFixedHeight(360)
        self.setup_chunk_widget()

    # ChunkParamWidgetBase interface
    def get_value(self):
        return _selected_title

    def set_value(self, _v):
        pass

    def set_label(self, _t):
        pass

    # Public
    def fetch_catalog_async(self, cache_dir: str):
        def _work():
            try:
                materials = _fetch_all_materials(cache_dir)
                self._catalog_ready.emit(materials)
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    # Private
    def _on_catalog_ready(self, materials: list):
        global _all_materials
        _all_materials = materials
        self._do_search()
        # Restore previously picked selection from the param
        c = self.param.get_chunk()
        pt = c.get_param("picked_title") if c else None
        if pt and pt.value:
            self._restore_selection(pt.value)

    def _restore_selection(self, title: str):
        if title in self._tiles:
            self._set_selected(title)
        else:
            # Tile may not be loaded yet — scroll-load until found or exhausted
            while title not in self._tiles and self._page_offset < len(self._page_items):
                self._load_next_page()
            if title in self._tiles:
                self._set_selected(title)

    def _do_search(self):
        global _results_by_title
        term = self._search.text().lower()
        hits = [m for m in _all_materials if term in m.get("title", "").lower()]
        _results_by_title = {m["title"]: m for m in hits}
        self._reset_pages(_results_by_title)

    def _reset_pages(self, results: dict[str, dict]):
        global _selected_title
        self._flow.clear()
        self._tiles.clear()
        _selected_title = ""
        self._page_items = list(results.items())
        self._page_offset = 0
        self._load_next_page()

    def _load_next_page(self):
        page = self._page_items[self._page_offset : self._page_offset + 10]
        if not page:
            return
        for title, mat in page:
            render_ids = mat.get("renders_order") or mat.get("renders") or []
            rid = render_ids[0] if render_ids else ""
            tile = _MaterialTile(title, rid, self._loader)
            tile.clicked_title.connect(self._on_tile_clicked)
            self._tiles[title] = tile
            self._flow.addWidget(tile)
        self._page_offset += len(page)
        if not _selected_title and self._tiles:
            self._set_selected(next(iter(self._tiles)))

    def _on_scroll(self, value: int):
        bar = self._scroll.verticalScrollBar()
        if value >= bar.maximum() - 50:
            self._load_next_page()

    def _on_tile_clicked(self, title: str):
        self._set_selected(title)
        self.param.send_self()

    def _set_selected(self, title: str):
        global _selected_title
        _selected_title = title
        for t, tile in self._tiles.items():
            tile.setSelected(t == title)


# Chunk callbacks


def _on_chunk_loaded(owner, notice, sender):
    cache_param = owner.get_param("cache_dir", chunk.is_param_t(str))
    grid_param = owner.get_param("material_grid", chunk.is_inst(chunk.Custom))

    cache_dir = cache_param.value or os.path.expanduser("~/.gpuopen_cache")

    if isinstance(grid_param.custom_widget, MaterialGridWidget):
        grid_param.custom_widget.fetch_catalog_async(cache_dir)


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


def _create_child(module_key: str, parent):
    """Create a child chunk and attach it to parent inside the user's edit context.

    add_param is called inside ChunkStageSync._chunk_prim_edit_context() so that
    the Chunk prim authored by _define_prim lands on the user's persistent edit
    layer (session layer), not on the parent context's anonymous execute layer.
    """
    with ChunkStageSync()._chunk_prim_edit_context():
        mod = chunk_registry.ChunkRegistry().get_chunk_module(module_key)
        if not mod:
            return None
        created = mod.create() if mod.loaded else mod.load()
        child = created[0] if created else None
        if child:
            parent.add_param(child)
    return child


def _download_and_apply(node, title: str) -> str:
    resolution = node.get_param("resolution", chunk.is_inst(chunk.Menu)).value
    mat_scope = (node.get_param("mat_scope", chunk.is_param_t(str)).value or "/materials").rstrip("/")
    cache_dir = node.get_param("cache_dir", chunk.is_param_t(str)).value or os.path.expanduser("~/.gpuopen_cache")

    mat = _results_by_title.get(title) or next(
        (m for m in _fetch_all_materials(cache_dir) if m.get("title") == title), None
    )
    if not mat:
        return f"Material '{title}' not found in catalog"

    try:
        pkg = _pick_package(mat.get("packages", []), resolution)
    except Exception as exc:
        return f"Package lookup failed: {exc}"

    if not pkg:
        return "No package found"
    file_url = pkg.get("file_url")
    if not file_url:
        return "Package has no download URL"

    mat_dir = os.path.join(cache_dir, _safe_prim_name(f"{title}_{pkg.get('label', resolution)}"))
    os.makedirs(mat_dir, exist_ok=True)

    try:
        resp = requests.get(file_url, timeout=120)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            z.extractall(mat_dir)
            names = z.namelist()
    except Exception as exc:
        return f"Download failed: {exc}"

    mtlx_path = next(
        (os.path.join(mat_dir, n).replace("\\", "/") for n in names if n.endswith(".mtlx")),
        None,
    )
    if not mtlx_path:
        return "No .mtlx file in package"

    mat_name = _safe_prim_name(title)
    mat_path = f"{mat_scope}/{mat_name}"
    mtlx_mat_name = mat.get("mtlx_material_name", mat_name)

    # Children must be added to the CONTEXT *before* their param values are set.
    # Setting a value triggers an immediate self-execute (reference.execute /
    # bind_material.execute), which clears the stage's current edit-target layer
    # on the assumption it owns a dedicated layer — a layer that add_param's
    # CONTEXT branch is what creates. Setting values first leaves that Clear()
    # hitting whatever layer happens to be the edit target instead (e.g. the
    # parent's), wiping the child's own params before they're ever read back.
    ref_chunk = node.get_param("Reference", chunk.is_inst(chunk.Chunk))
    if not ref_chunk:
        ref_chunk = _create_child("reference", node)
    if ref_chunk:
        # file_path's file_changed callback auto-derives primpath_location from
        # the filename, so set file_path first — otherwise that auto-suggestion
        # overwrites the explicit mat_path we set below.
        ref_chunk.get_param("file_path").value = mtlx_path
        ref_chunk.get_param("import_primpath").value = f"/MaterialX/Materials/{mtlx_mat_name}"
        ref_chunk.get_param("prim_type").value = "Material"
        ref_chunk.get_param("primpath_location").value = mat_path

    bind_chunk = node.get_param("BindMaterial", chunk.is_inst(chunk.Chunk))
    if not bind_chunk:
        bind_chunk = _create_child("bind_material", node)
    if bind_chunk:
        bind_chunk.get_param("material_path").value = mat_path
        bind_chunk.get_param("primpath_location").value = node.get_param("target_primpath").value

    node.get_param("resolved_material_path").value = mat_path
    return f"Ready — {mat_path}"


def execute(node):
    """Re-applies the stored picked material, (re)creating the Reference and
    BindMaterial children if they're missing — e.g. after a reload where the
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


def apply_material(owner, _notice, _sender):
    title = _selected_title
    if not title:
        status_param = owner.get_param("status", chunk.is_inst(chunk.Label))
        if status_param:
            status_param.value = "Select a material first"
        return
    owner.get_param("picked_title").value = title
    execute(owner)


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
    grid_param = chunk.Custom("material_grid")
    grid_param.custom_widget = MaterialGridWidget(grid_param)

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

    chunk_api.add_params([prim_path, mat_scope, grid_param, resolution, picked_title, material_path, status, advanced, apply_btn])

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
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.EXAMPLES,
        requirements=["requests"],
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
