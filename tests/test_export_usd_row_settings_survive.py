"""Export USD's per-layer settings (Export, Bake, File Name) come back as they were saved.

Saved, reopened, they came back scrambled. Three things reset them:

- Restoring a saved Export checkbox ran its callback, which resets that row's Bake from
  Bake all: a Bake saved on came back off.
- The rows were rebuilt while the load was still restoring them, from a stage not yet full.
- A layer that drops out of the stage for a moment (a recook moving layers around) lost its
  row, and came back on the next refresh with the defaults.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene, chunk


def _rows(scene):
    rows = scene.get_chunk("export").get_param("export_layers", chunk.is_inst(chunk.ListGroup))

    def get(name, i):
        return rows.get_indexed_param(rows.get_param(name), i).value

    return rows, {get("layer_name", i): (get("export_enabled", i), get("bake", i), get("file_name", i))
                  for i in range(rows.size)}


WANT = {
    "look": (True, True, "materials_v009"),
    "fx": (False, False, ""),
    "light": (True, False, "lights_v002.usdc"),
    "dept_file.usda": (False, False, ""),
}


def test_row_settings_survive_save_and_open(tmp_path) -> None:
    dept = Sdf.Layer.CreateNew(str(tmp_path / "dept_file.usda"))
    Sdf.CreatePrimInLayer(dept, "/x").specifier = Sdf.SpecifierDef
    dept.Save()
    network = str(tmp_path / "network_scene.usda")

    with Scene() as scene:
        subs = scene.create.sublayer(name="subs")
        imports = subs.get_param("imports", chunk.is_inst(chunk.ListGroup))
        imports.size = 1
        imports.get_indexed_param("sublayer_path", 0, chunk.is_param_t(str)).value = str(tmp_path / "dept_file.usda")
        last = subs
        for name in ("look", "fx", "light"):
            layer = scene.create.layer(last, layer_name=name, name=f"layer_{name}")
            last = scene.create.edit_target(layer, edit_target=name, name=f"target_{name}")
        scene.create.export_usd(last, output_dir=str(tmp_path), name="export")
        rows, _ = _rows(scene)
        for i in range(rows.size):
            name = rows.get_indexed_param(rows.get_param("layer_name"), i).value
            if name in WANT:
                export, bake, file_name = WANT[name]
                rows.get_indexed_param(rows.get_param("export_enabled"), i).value = export
                rows.get_indexed_param(rows.get_param("bake"), i).value = bake
                rows.get_indexed_param(rows.get_param("file_name"), i).value = file_name
        assert {k: v for k, v in _rows(scene)[1].items() if k in WANT} == WANT
        scene.save(network)

    with Scene.open(network) as scene:
        assert {k: v for k, v in _rows(scene)[1].items() if k in WANT} == WANT
