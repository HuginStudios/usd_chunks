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

    def settles(node):
        # Whether node stops cooking once its after-cook updates have run.
        for _ in range(3):
            node.stage()
            scene.flush_pending()
        count = node.cookCount()
        for _ in range(2):
            node.stage()
            scene.flush_pending()
        return node.cookCount() == count

    out["mesh_settles"] = settles(mesh)
    layer = lop.create(net, "layer")
    layer.setInput(0, mesh)
    out["layer_settles"] = settles(layer)

    out["menu"] = lop.menu_items(mesh, "mesh_type")[:4]

    # Params tied to another prim's attribute: a Light's (show_attribute) and a Camera's
    # (connect_usd_attr). Houdini's stage holds no chunk prims to drive them from.
    light = lop.create(net, "light", name="Key")
    for _ in range(3):
        light.stage()
        scene.flush_pending()
    light.parm("intensity").set(7.0)
    for _ in range(3):
        light.stage()
        scene.flush_pending()
    lights = [p for p in light.stage().Traverse() if p.GetTypeName().endswith("Light")]
    out["light_intensity"] = lights[0].GetAttribute("inputs:intensity").Get() if lights else None
    camera = lop.create(net, "camera", name="Cam")
    camera.parm("focalLength").set(35.0)
    camera.stage()
    scene.flush_pending()
    cams = [p for p in camera.stage().Traverse() if p.GetTypeName() == "Camera"]
    out["camera_focal"] = cams[0].GetAttribute("focalLength").Get() if cams else None

    inline = lop.create(net, "inline_usd", name="Inline")
    inline.stage()
    scene.flush_pending()
    out["inline_prims"] = [str(p.GetPath()) for p in inline.stage().Traverse() if p.GetTypeName() == "Sphere"]

    # Two Transform chunks on one prim: each owns an op, so both apply.
    first_move = lop.create(net, "transform", name="MoveA")
    first_move.setInput(0, mesh)
    first_move.parm("primpath_location").set("/mesh")
    first_move.parm("tx").set(1)
    second_move = lop.create(net, "transform", name="MoveB")
    second_move.setInput(0, first_move)
    second_move.parm("primpath_location").set("/mesh")
    second_move.parm("tx").set(2)
    from pxr import UsdGeom as _G
    moved = second_move.stage().GetPrimAtPath("/mesh")
    out["two_moves_ops"] = [str(o) for o in moved.GetAttribute("xformOpOrder").Get() or []]
    out["two_moves_tx"] = _G.Xformable(moved).ComputeLocalToWorldTransform(0).ExtractTranslation()[0]

    # Layers: a Layer chunk starts a new active layer and names it; the chunks after it,
    # through an Edit Target pointing at it, all author into that one layer.
    from usd_chunks.hosts.houdini import layers
    before = lop.create(net, "mesh", name="Before")
    before.parm("prim_path").set("/before")
    new_layer = lop.create(net, "layer", name="Mats")
    new_layer.setInput(0, before)
    new_layer.parm("layer_name").set("mats")
    target = lop.create(net, "edit_target", name="Target")
    target.setInput(0, new_layer)
    target.parm("edit_target").set("mats")
    first = lop.create(net, "mesh", name="First")
    first.setInput(0, target)
    first.parm("prim_path").set("/twice")
    first.parm("mesh_type").set("sphere")
    second = lop.create(net, "mesh", name="Second")
    second.setInput(0, first)
    second.parm("prim_path").set("/twice")
    second.parm("mesh_type").set("cube")
    stage = second.stage()
    named = dict(layers.user_layers(stage))
    out["named_layers"] = list(named)
    mats = named.get("mats")
    out["mats_prims"] = sorted(str(p.path) for p in mats.rootPrims) if mats else []
    out["layer_errors"] = [n.name() for n in (before, new_layer, target, first, second) if n.errors()]
    twice = stage.GetPrimAtPath("/twice")
    out["twice_counts"] = len(twice.GetAttribute("faceVertexCounts").Get() or [])
    out["save_path"] = new_layer.node(lop.NEW_LAYER_NODE).evalParm("savepath")
    scene.flush_pending()
    out["target_menu"] = lop.menu_items(target, "edit_target")[::2]
    later = lop.create(net, "layer", name="Later")
    later.setInput(0, second)
    later.parm("layer_name").set("later")
    back = lop.create(net, "edit_target", name="Back")
    back.setInput(0, later)
    back.parm("edit_target").set("mats")
    back.stage()
    out["older_target_error"] = " ".join(back.errors())

    export = lop.create(net, "export_usd", name="Publish")
    export.setInput(0, second)
    publish_dir = os.path.join(os.path.dirname(os.environ["USD_CHUNKS_TEST_HIP"]), "publish")
    export.parm("output_dir").set(publish_dir)
    export.stage()
    scene.flush_pending()
    out["export_rows"] = [p.eval() for p in export.parms() if p.name().startswith("export_layers_layer_name")]
    export.parm("export_all").pressButton()
    out["published"] = sorted(os.listdir(publish_dir)) if os.path.isdir(publish_dir) else []
    mats_file = os.path.join(publish_dir, "mats.usda")
    out["published_mats"] = open(mats_file).read() if os.path.isfile(mats_file) else ""

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
    out["import_group_parms"] = chunkgroup.public_parms(group)
    out["import_children"] = sorted(c.name() for c in group.children())
    out["import_wires"] = {c.name(): [i.name() for i in c.inputs() if i] for c in group.children()}
    out["import_output"] = [i.name() for i in group.node("output0").inputs() if i]
    out["space_out_input"] = [i.name() for i in group.node("space_out").inputs() if i]
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
    native.parm("floors").set(2)  # as the network's public param sets it
    cache.Clear()
    out["house_native"] = size(native.stage(), "/House")
    upstream = hou.node("/stage").createNode("cube", "Upstream")
    group.setInput(0, upstream)
    cache.Clear()
    out["solaris_space"] = list(space.stage_space(upstream.stage()))
    out["upstream_before"] = size(upstream.stage(), "/Upstream")
    cache.Clear()
    out["upstream_through_group"] = size(group.stage(), "/Upstream")
    group.setInput(0, None)

    plain = chunkgroup.import_file(os.environ["USD_CHUNKS_TEST_NO_VIEW"], hou.node("/stage"))[0]
    inner = plain.node("Inner")
    out["no_view_output"] = [i.name() for i in plain.node("space_out").inputs() if i]
    out["no_view_inner_output"] = [i.name() for i in inner.node("space_out").inputs() if i]
    out["no_view_prims"] = [str(p.GetPath()) for p in plain.stage().Traverse()
                            if not str(p.GetPath()).startswith("/HoudiniLayerInfo")]

    layered = chunkgroup.import_file(os.environ["USD_CHUNKS_TEST_LAYERED"], hou.node("/stage"))[0]
    out["imported_target"] = layered.node("Target").evalParm("edit_target")
    lit = chunkgroup.import_file(os.environ["USD_CHUNKS_TEST_LIT"], hou.node("/stage"))[0]
    for _ in range(3):
        lit.stage()
        scene.flush_pending()
    key = lit.stage().GetPrimAtPath("/lights/key")
    out["imported_intensity"] = key.GetAttribute("inputs:intensity").Get() if key else None
    out["imported_intensity_parm"] = lit.node("Key").evalParm("intensity")
    out["imported_op"] = [str(o) for o in key.GetAttribute("xformOpOrder").Get() or []] if key else []
    layered.stage()
    out["imported_target_errors"] = list(layered.node("Target").errors())

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


#: A network as it is often saved: no View anywhere, in the group or in the group inside it.
#: usd_chunks cooks such a group through the end of its exec chain.
NO_VIEW_NETWORK = textwrap.dedent("""    #usda 1.0
    (
        metersPerUnit = 1
        upAxis = "Y"
    )

    def Scope "chunks"
    {
        def Chunk "Plain"
        {
            uniform token info:chunkType = "chunkgroup"
            uniform token outputs:chunk

            def Chunk "Ball"
            {
                uniform token info:chunkType = "mesh"
                string inputs:mesh_type = "sphere"
                string inputs:prim_path = "/ball"
                uniform token outputs:chunk
            }

            def Chunk "Inner"
            {
                uniform token info:chunkType = "chunkgroup"
                token inputs:chunk.connect = </chunks/Plain/Ball.outputs:chunk>
                uniform token outputs:chunk

                def Chunk "Move"
                {
                    uniform token info:chunkType = "transform"
                    string inputs:primpath_location = "/ball"
                    float inputs:translate_grp:tx = 3
                    uniform token outputs:chunk
                }
            }
        }
    }
""")


#: A light whose intensity lives in a param its cook builds, then a Transform that recorded
#: its op name, as the sh010 light scene saves them.
LIT_NETWORK = textwrap.dedent("""    #usda 1.0
    (
        metersPerUnit = 1
        upAxis = "Y"
    )

    def Scope "chunks"
    {
        def Chunk "Lit"
        {
            uniform token info:chunkType = "chunkgroup"
            uniform token outputs:chunk

            def Chunk "Key"
            {
                uniform token info:chunkType = "light"
                string inputs:light_type = "DistantLight"
                string inputs:primpath_location = "/lights/key"
                float inputs:attr_grp:inputs:intensity = 2.5
                uniform token outputs:chunk
            }

            def Chunk "Aim"
            {
                custom uniform string chunk:xformOpId = "aim_key_5c63f397"
                uniform token info:chunkType = "transform"
                token inputs:chunk.connect = </chunks/Lit/Key.outputs:chunk>
                string inputs:primpath_location = "/lights/key"
                uniform token outputs:chunk
            }
        }
    }
""")


#: A Layer and an Edit Target pointing at it by name, as lookdev saves them.
LAYERED_NETWORK = textwrap.dedent("""    #usda 1.0
    (
        metersPerUnit = 1
        upAxis = "Y"
    )

    def Scope "chunks"
    {
        def Chunk "Layered"
        {
            uniform token info:chunkType = "chunkgroup"
            uniform token outputs:chunk

            def Chunk "Mats"
            {
                uniform token info:chunkType = "layer"
                string inputs:layer_name = "mats"
                uniform token outputs:chunk
            }

            def Chunk "Target"
            {
                uniform token info:chunkType = "edit_target"
                token inputs:chunk.connect = </chunks/Layered/Mats.outputs:chunk>
                string inputs:edit_target = "mats"
                uniform token outputs:chunk
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
    no_view = tmp / "no_view.usda"
    no_view.write_text(NO_VIEW_NETWORK)
    layered = tmp / "layered.usda"
    layered.write_text(LAYERED_NETWORK)
    lit = tmp / "lit.usda"
    lit.write_text(LIT_NETWORK)
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
        "USD_CHUNKS_TEST_NO_VIEW": str(no_view),
        "USD_CHUNKS_TEST_LAYERED": str(layered),
        "USD_CHUNKS_TEST_LIT": str(lit),
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


def test_a_chunk_node_stops_cooking_once_its_parms_are_shown(result):
    assert result["mesh_settles"]


def test_a_chunk_writing_session_state_stops_cooking(result):
    # Layer writes its new layer's id, which differs every cook, into a hidden param.
    assert result["layer_settles"]


def test_a_layer_and_edit_target_send_the_chunks_after_them_into_one_named_layer(result):
    assert result["layer_errors"] == []
    assert result["named_layers"] == ["mats"]
    assert result["mats_prims"] == ["/HoudiniLayerInfo", "/twice"]


def test_edit_target_offers_the_named_layers(result):
    assert "mats" in result["target_menu"]


def test_a_later_chunk_wins_over_an_earlier_one_in_the_same_layer(result):
    assert result["twice_counts"] == 6  # the cube, over the sphere before it


def test_a_layer_gets_a_save_path_for_a_usd_rop(result):
    assert result["save_path"].replace("\\", "/").endswith("/usd/mats.usda")


def test_an_edit_target_pointing_at_an_older_layer_says_it_is_not_supported(result):
    assert "not the newest layer" in result["older_target_error"]


def test_export_usd_lists_and_writes_only_the_named_layers(result):
    assert result["export_rows"] == ["mats"]
    assert result["published"] == ["mats.usda"]
    assert 'def Mesh "twice"' in result["published_mats"]
    assert "HoudiniLayerInfo" not in result["published_mats"]  # Houdini's bookkeeping, as a ROP drops it


def test_a_lights_attribute_parm_drives_the_light(result):
    assert result["light_intensity"] == pytest.approx(7.0)


def test_a_cameras_parm_drives_the_camera(result):
    assert result["camera_focal"] == pytest.approx(35.0)


def test_inline_usd_cooks_its_text_into_the_node(result):
    # It wrote its layer from a callback only; a node's cook starts from an empty layer.
    assert result["inline_prims"] == ["/sphere"]


def test_two_transforms_on_one_prim_each_own_an_op(result):
    assert len(set(result["two_moves_ops"])) == 2
    assert result["two_moves_tx"] == pytest.approx(3.0)


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
    assert result["import_children"] == ["Ground", "House", "Move", "output0", "space_in", "space_out"]
    assert result["import_errors"] == []


def test_a_groups_public_params_are_parms_on_the_subnet(result):
    assert result["import_group_parms"] == ["House_floors"]
    assert result["import_exprs"]["floors"] == 'ch("../House_floors")'


def test_exec_wires_become_lop_wires_and_the_view_the_output(result):
    assert result["import_wires"]["Ground"] == ["House"]
    assert result["import_wires"]["Move"] == ["Ground"]
    assert result["import_output"] == ["space_out"]
    assert result["space_out_input"] == ["Move"]


def test_a_group_with_no_view_outputs_the_end_of_its_chain(result):
    assert result["no_view_output"] == ["Inner"]
    assert result["no_view_inner_output"] == ["Move"]
    assert "/ball" in result["no_view_prims"]


def test_an_imported_light_keeps_the_intensity_its_file_saved(result):
    # The param only exists once the light has cooked, after import set the others.
    assert result["imported_intensity"] == pytest.approx(2.5)
    assert result["imported_intensity_parm"] == pytest.approx(2.5)


def test_an_imported_transform_keeps_the_op_name_its_file_recorded(result):
    assert result["imported_op"] == ["xformOp:transform:aim_key_5c63f397"]


def test_an_imported_edit_target_keeps_the_layer_it_names(result):
    # Its menu lists layers only once a cook has seen them; the saved name must survive that.
    assert result["imported_target"] == "mats"
    assert result["imported_target_errors"] == []


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
    assert result["reimport_children"] == ["Ground", "House", "Move", "output0", "space_in", "space_out"]
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


def test_a_stage_that_sets_no_units_is_in_houdinis_metres(result):
    # A Solaris stage authors no upAxis or metersPerUnit: it is Houdini's Y-up metres, not
    # USD's centimetre fallback.
    assert result["solaris_space"] == ["Y", 1.0]
