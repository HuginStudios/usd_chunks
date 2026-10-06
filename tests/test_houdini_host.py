"""The Houdini host, driven through Houdini 21's hython: chunks as LOPs with native parms.

`hou` only exists inside Houdini, so the checks run in one hython subprocess that reports
what it saw as JSON, and the asserts are made here. Skipped where Houdini 21 is not
installed (HFS, else the default Windows install), or where usd_chunks' dependencies are
not in the dependency dir for hython to find (hosts/houdini/install.py puts them there).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HDA = REPO / "usd_chunks" / "hosts" / "houdini" / "otls" / "usdchunks_chunk.hda"


def _hython() -> str | None:
    candidates = []
    if hfs := os.environ.get("HFS"):
        candidates.append(os.path.join(hfs, "bin", "hython.exe" if sys.platform == "win32" else "hython"))
    candidates += sorted(glob.glob(r"C:\Program Files\Side Effects Software\Houdini 21.0.*\bin\hython.exe"))
    return next((c for c in reversed(candidates) if os.path.isfile(c)), None)


def _dependency_dir() -> str:
    base = os.environ.get("USD_CHUNK_DEPENDENCY_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"), "usd_chunks", "installed_deps")
    return os.path.join(base, "py311")  # Houdini 21's Python


SCRIPT = textwrap.dedent(r"""
    import json, os, sys
    sys.path.append(os.environ["USD_CHUNKS_TEST_DEPS"])
    import hou
    hou.hda.installFile(os.environ["USD_CHUNKS_TEST_HDA"])
    from usd_chunks.hosts.houdini import lop, tools
    from usd_chunks.hosts.houdini.scene import ensure_scene
    from usd_chunks import chunk_registry

    out = {}
    scene = ensure_scene()
    net = hou.node("/stage")
    cube = net.createNode("cube")
    mesh = lop.create(net, "mesh")
    mesh.setInput(0, cube)
    out["mesh_parms"] = [p.name() for p in mesh.parms()]
    mesh.parm("mesh_type").set("torus")
    stage = mesh.stage()
    out["errors"] = list(mesh.errors())
    out["prims"] = [str(p.GetPath()) for p in stage.Traverse()]
    scene.flush_pending()
    out["cube_folder_hidden"] = mesh.parmTuple("cube").isHidden()
    out["torus_folder_hidden"] = mesh.parmTuple("torus").isHidden()

    count = mesh.cookCount()
    mesh.parm("torus_radius").set(4)
    points = mesh.stage().GetPrimAtPath("/mesh").GetAttribute("points").Get()
    out["recooked"] = mesh.cookCount() > count
    out["max_x"] = max(p[0] for p in points)

    xform = lop.create(net, "transform")
    xform.setInput(0, mesh)
    xform.parm("primpath_location").set("/mesh")
    xform.parm("tx").set(5)
    xform.parm("px").set(3)
    xform.stage()
    scene.flush_pending()
    out["xform_layer"] = lop._outputs[xform.sessionId()].ExportToString()
    xform.parm("center_pivot").pressButton()
    scene.flush_pending()
    out["pivot_after_center"] = xform.evalParm("px")

    out["menu"] = lop.menu_items(mesh, "mesh_type")[:4]

    sub = lop.create(net, "sublayer")
    sub.parm("imports").set(2)
    sub.stage()
    out["rows"] = lop.chunk_for(sub).get_param("imports").size
    out["row_parms"] = [p.name() for p in sub.parms() if p.name().startswith("imports_")]

    xml = tools.shelf_xml(chunk_registry.ChunkRegistry().get_menu_entries())
    out["mesh_tool"] = "<toolSubmenu>usdchunks/Modelling</toolSubmenu>" in xml and "'mesh'" in xml

    key = sub.sessionId()
    sub.destroy()
    out["deleted_dropped"] = key not in lop._chunks

    hip = os.environ["USD_CHUNKS_TEST_HIP"]
    hou.hipFile.save(hip)
    hou.hipFile.clear(suppress_save_prompt=True)
    lop._chunks.clear()
    hou.hipFile.load(hip)
    reloaded = hou.node("/stage/mesh")
    points = reloaded.stage().GetPrimAtPath("/mesh").GetAttribute("points").Get()
    out["reloaded_max_x"] = max(p[0] for p in points)

    from usd_chunks.hosts.houdini import chunkgroup
    created = chunkgroup.import_file(os.environ["USD_CHUNKS_TEST_NETWORK"], hou.node("/stage"))
    group = created[0]
    out["import_top"] = [n.name() for n in created]
    out["import_group_parms"] = [p.name() for p in group.spareParms()]
    out["import_children"] = sorted(c.name() for c in group.children())
    out["import_wires"] = {c.name(): [i.name() for i in c.inputs() if i] for c in group.children()}
    out["import_output"] = [i.name() for i in group.node("output0").inputs() if i]
    out["import_exprs"] = {
        "floors": group.node("House").parm("floors").expression(),
        "primpath": group.node("Move").parm("primpath_location").expression(),
    }
    out["import_values"] = [group.node("Ground").evalParm("prim_path"), group.node("Move").evalParm("tx")]
    stage = group.stage()
    out["import_errors"] = [n.path() for n in group.allSubChildren() if n.errors()]
    ground = stage.GetPrimAtPath("/ground")
    out["import_ground"] = bool(ground) and bool(ground.GetAttribute("xformOpOrder").Get())
    out["import_house"] = bool(stage.GetPrimAtPath("/House"))
    out["import_positions"] = {n: list(group.node(n).position()) for n in ("House", "Ground", "Move")}
    out["import_type"] = group.type().name()
    out["import_editable"] = group.isEditable()

    from pxr import Usd
    exported = os.path.join(os.path.dirname(os.environ["USD_CHUNKS_TEST_HIP"]), "exported.usda")
    chunkgroup.export(group, exported)
    net = Usd.Stage.Open(exported)
    prims = {str(p.GetPath()): p for p in net.Traverse() if p.GetAttribute("info:chunkType")}
    out["export_prims"] = sorted((path, str(p.GetAttribute("info:chunkType").Get())) for path, p in prims.items())
    move = prims.get("/chunks/Net/Move")
    out["export_wire"] = [str(c) for c in move.GetAttribute("inputs:primpath_location").GetConnections()] if move else []
    out["export_exec"] = [str(c) for c in move.GetAttribute("inputs:chunk").GetConnections()] if move else []
    out["export_loft"] = net.GetPrimAtPath("/chunks/Net").GetAttribute("inputs:House_floors").Get()
    out["export_positions"] = {n: list(prims[f"/chunks/Net/{n}"].GetAttribute("ui:nodegraph:node:pos").Get())
                               for n in ("House", "Ground", "Move")}
    from pxr import UsdGeom
    out["export_space"] = [str(UsdGeom.GetStageUpAxis(net)), UsdGeom.GetStageMetersPerUnit(net)]
    again = chunkgroup.import_file(exported, hou.node("/stage"))[0]
    out["reimport_children"] = sorted(c.name() for c in again.children())
    out["reimport_exprs"] = [again.node("Move").parm("primpath_location").expression(),
                             again.node("House").parm("floors").expression()]
    # Network space: the group cooks Z-up in centimetres and comes out in Houdini's Y-up
    # metres, so its House is the one Houdini builds itself, a hundredth the size.
    from usd_chunks.hosts.houdini import space
    out["group_space"] = [group.evalParm(space.UP_PARM), group.evalParm(space.MPU_PARM)]
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])

    def size(stage, path):
        return list(cache.ComputeWorldBound(stage.GetPrimAtPath(path)).ComputeAlignedRange().GetSize())

    cache.Clear()
    out["house_in_group"] = size(group.stage(), "/House")
    native = lop.create(hou.node("/stage"), "house", name="NativeHouse")
    cache.Clear()
    out["house_native"] = size(native.stage(), "/House")
    upstream = hou.node("/stage").createNode("cube", "Upstream")
    group.setInput(0, upstream)
    cache.Clear()
    out["upstream_before"] = size(upstream.stage(), "/cube1")
    cache.Clear()
    out["upstream_through_group"] = size(group.stage(), "/cube1")
    group.setInput(0, None)

    group.createNode("cube", "SolarisCube")
    try:
        chunkgroup.export(group, exported)
        out["export_refused"] = ""
    except chunkgroup.ExportError as exc:
        out["export_refused"] = str(exc)

    print("USD_CHUNKS_RESULT" + json.dumps(out))
""")


#: A saved network, as Qt writes one: a chunkgroup whose public param feeds a chunk, an
#: exec chain, an expression wire between two chunks, and a View on the last.
NETWORK = textwrap.dedent("""    #usda 1.0
    (
        metersPerUnit = 0.01
        upAxis = "Z"
    )

    def Scope "chunks"
    {
        def Chunk "Net"
        {
            custom int chunk:activeView = 1
            uniform token info:chunkType = "chunkgroup"
            custom int inputs:House_floors = 2
            uniform token outputs:chunk

            def Chunk "House"
            {
                uniform token info:chunkType = "house"
                int inputs:footprint_grp:floors.connect = </chunks/Net.inputs:House_floors>
                uniform token outputs:chunk
                uniform float2 ui:nodegraph:node:pos = (0, 0)
            }

            def Chunk "Ground"
            {
                uniform token info:chunkType = "mesh"
                token inputs:chunk.connect = </chunks/Net/House.outputs:chunk>
                string inputs:mesh_type = "grid"
                string inputs:prim_path = "/ground"
                uniform token outputs:chunk
                uniform float2 ui:nodegraph:node:pos = (260, 0)
            }

            def Chunk "Move"
            {
                uniform token info:chunkType = "transform"
                token inputs:chunk.connect = </chunks/Net/Ground.outputs:chunk>
                string inputs:primpath_location = "@Ground.prim_path@"
                string inputs:primpath_location.connect = </chunks/Net/Ground.inputs:prim_path>
                float inputs:translate_grp:tx = 3
                uniform token outputs:chunk
                uniform float2 ui:nodegraph:node:pos = (520, 0)
            }

            def Chunk "View1"
            {
                uniform token info:chunkType = "view"
                token inputs:chunk.connect = </chunks/Net/Move.outputs:chunk>
                custom uniform int view:slot = 1
            }
        }
    }
""")


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    hython = _hython()
    if hython is None:
        pytest.skip("Houdini 21 hython not found")
    deps = _dependency_dir()
    if not os.path.isfile(os.path.join(deps, "typing_extensions.py")):
        pytest.skip("usd_chunks dependencies not installed for Houdini (run hosts/houdini/install.py)")
    tmp = tmp_path_factory.mktemp("houdini")
    network = tmp / "network.usda"
    network.write_text(NETWORK)
    script = tmp / "check.py"
    script.write_text(SCRIPT)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(REPO),
        "USD_CHUNK_DIRS": str(REPO / "chunks"),
        "USD_CHUNKS_TEST_DEPS": deps,
        "USD_CHUNKS_TEST_HDA": str(HDA),
        "USD_CHUNKS_TEST_HIP": str(tmp / "scene.hip"),
        "USD_CHUNKS_TEST_NETWORK": str(network),
    })
    proc = subprocess.run([hython, str(script)], env=env, capture_output=True, text=True, timeout=600)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("USD_CHUNKS_RESULT")), None)
    assert line, f"hython failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    return json.loads(line[len("USD_CHUNKS_RESULT"):])


def test_chunk_params_are_native_parms(result):
    for name in ("prim_path", "mesh_type", "torus_radius"):
        assert name in result["mesh_parms"]


def test_a_chunk_cooks_its_layer_over_the_lop_input(result):
    assert result["errors"] == []
    assert "/cube1" in result["prims"] and "/mesh" in result["prims"]


def test_a_menu_hides_and_shows_folders_on_the_node(result):
    assert result["cube_folder_hidden"] is True
    assert result["torus_folder_hidden"] is False


def test_a_parm_change_recooks(result):
    assert result["recooked"]
    assert result["max_x"] == pytest.approx(4.4, abs=1e-3)


def test_chunks_wire_through_lop_inputs(result):
    assert 'over "mesh"' in result["xform_layer"]
    assert "(5, 0, 0, 1)" in result["xform_layer"]


def test_a_button_runs_its_callback_and_shows_what_it_set(result):
    assert result["pivot_after_center"] == pytest.approx(0.0, abs=1e-6)


def test_menus_ask_the_chunk_for_items(result):
    assert result["menu"] == ["none", "none", "cube", "cube"]


def test_a_list_group_is_a_multiparm(result):
    assert result["rows"] == 2
    assert result["row_parms"] == ["imports_sublayer_path1", "imports_sublayer_path2"]


def test_the_tab_menu_lists_chunks_under_usdchunks(result):
    assert result["mesh_tool"]


def test_deleting_a_node_drops_its_chunk(result):
    assert result["deleted_dropped"]


def test_a_saved_hip_cooks_the_same_after_reload(result):
    assert result["reloaded_max_x"] == pytest.approx(4.4, abs=1e-3)



def test_a_network_imports_as_a_subnet_of_chunk_lops(result):
    assert result["import_top"] == ["Net"]
    assert result["import_children"] == ["Ground", "House", "Move", "output0"]
    assert result["import_errors"] == []


def test_a_groups_public_params_are_parms_on_the_subnet(result):
    assert result["import_group_parms"] == ["House_floors"]
    assert result["import_exprs"]["floors"] == 'ch("../House_floors")'


def test_exec_wires_become_lop_wires_and_the_view_the_output(result):
    assert result["import_wires"]["Ground"] == ["House"]
    assert result["import_wires"]["Move"] == ["Ground"]
    assert result["import_output"] == ["Move"]


def test_a_param_wire_becomes_a_channel_reference(result):
    assert result["import_exprs"]["primpath"] == 'chs("../Ground/prim_path")'


def test_saved_values_land_on_the_parms_and_cook(result):
    assert result["import_values"] == ["/ground", 3.0]
    assert result["import_ground"] and result["import_house"]


def test_a_network_imports_as_a_chunkgroup_node_you_can_edit_inside(result):
    assert result["import_type"] == "usdchunks::chunkgroup"
    assert result["import_editable"]


def test_a_chunkgroup_exports_as_a_network(result):
    assert result["export_prims"] == [
        ["/chunks/Net", "chunkgroup"], ["/chunks/Net/Ground", "mesh"], ["/chunks/Net/House", "house"],
        ["/chunks/Net/Move", "transform"], ["/chunks/Net/View1", "view"]]
    assert result["export_exec"] == ["/chunks/Net/Ground.outputs:chunk"]
    assert result["export_wire"] == ["/chunks/Net/Ground.inputs:prim_path"]
    assert result["export_loft"] == 2


def test_an_exported_network_imports_back_the_same(result):
    assert result["reimport_children"] == ["Ground", "House", "Move", "output0"]
    assert result["reimport_exprs"] == ['chs("../Ground/prim_path")', 'ch("../House_floors")']


def test_export_refuses_a_solaris_node_inside_and_names_it(result):
    assert "SolarisCube" in result["export_refused"]


def test_a_left_to_right_graph_reads_top_to_bottom_in_houdini(result):
    # House -> Ground -> Move run left to right at x = 0, 260, 520; Houdini flows down.
    xs = [result["import_positions"][n][0] for n in ("House", "Ground", "Move")]
    ys = [result["import_positions"][n][1] for n in ("House", "Ground", "Move")]
    assert xs == pytest.approx([0, 0, 0])
    assert ys[0] > ys[1] > ys[2]


def test_positions_survive_the_round_trip(result):
    assert result["export_positions"]["House"] == pytest.approx([0, 0], abs=1e-3)
    assert result["export_positions"]["Ground"] == pytest.approx([260, 0], abs=1e-3)
    assert result["export_positions"]["Move"] == pytest.approx([520, 0], abs=1e-3)


def test_a_chunkgroup_keeps_the_space_its_network_was_authored_in(result):
    assert result["group_space"] == ["Z", pytest.approx(0.01)]
    assert result["export_space"] == ["Z", pytest.approx(0.01)]


def test_a_chunkgroup_turns_its_network_space_into_houdinis(result):
    # Z-up centimetres in, Y-up metres out: the same house Houdini builds, at 1/100.
    assert result["house_in_group"] == pytest.approx([v * 0.01 for v in result["house_native"]], rel=1e-3)


def test_upstream_content_passes_through_a_chunkgroup_unchanged(result):
    assert result["upstream_through_group"] == pytest.approx(result["upstream_before"], rel=1e-6)
