"""Regenerate otls/usdchunks_chunk.hda, the usd_chunks node types. Run with hython::

    hython usd_chunks/hosts/houdini/build_hda.py

The library holds two types:

- ``usdchunks::chunk`` -- one chunk: a subnet with a Python Script LOP calling lop.cook, two
  hidden parms naming the chunk, and an OnDeleted hook.
- ``usdchunks::chunkgroup`` -- a chunkgroup: an empty subnet whose contents are the user's
  (each node is unlocked on creation), with an Export Network button. Its public params are
  added per node, as spare parms.

The asset is committed in expanded form (a directory of text files, as `hotl -t` writes
it), which Houdini loads like a .hda file and git can diff. It holds no logic of its own --
everything else is Python in this package -- so it rarely needs rebuilding: only when one
of the things listed above changes.
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile

import hou

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

from usd_chunks.hosts.houdini import chunkgroup, lop, parms, space, tools  # noqa: E402

OUT = os.path.join(HERE, "otls", "usdchunks_chunk.hda")
ON_DELETED = "from usd_chunks.hosts.houdini import lop\nlop.on_deleted(kwargs)\n"


def _empty_subnet(network: hou.Node, name: str) -> hou.Node:
    subnet = network.createNode("subnet", name)
    for child in subnet.children():
        if child.type().name() != "output":
            child.destroy()
    return subnet


def _finish(definition: hou.HDADefinition, group: hou.ParmTemplateGroup, comment: str) -> None:
    definition.setParmTemplateGroup(group)
    definition.setIcon(tools.ICON)
    definition.setComment(comment)
    options = definition.options()
    options.setCompressContents(False)  # plain text, so a rebuild diffs
    definition.setOptions(options)


def _chunk_type(network: hou.Node, library: str) -> None:
    subnet = _empty_subnet(network, "usdchunks_chunk")
    cook = subnet.createNode("pythonscript", lop.COOK_NODE)
    cook.parm("python").set(lop.COOK_CODE)
    cook.setInput(0, subnet.indirectInputs()[0])
    output = next(c for c in subnet.children() if c.type().name() == "output")
    output.setInput(0, cook)
    subnet.layoutChildren()

    asset = subnet.createDigitalAsset(
        name=lop.HDA_TYPE, hda_file_name=library, description="USD Chunk",
        min_num_inputs=0, max_num_inputs=1, ignore_external_references=True)
    definition = asset.type().definition()
    group = hou.ParmTemplateGroup()
    group.append(hou.StringParmTemplate(parms.TYPE_PARM, "Chunk Module", 1, is_hidden=True))
    group.append(hou.StringParmTemplate(parms.VARIANT_PARM, "Chunk Variant", 1, is_hidden=True))
    definition.addSection("OnDeleted", ON_DELETED)
    definition.setExtraFileOption("OnDeleted/IsPython", True)
    _finish(definition, group, "A usd_chunks chunk. Made from the TAB menu's usdchunks entries.")
    definition.save(library, asset)


def _chunkgroup_type(network: hou.Node, library: str) -> None:
    subnet = _empty_subnet(network, "usdchunks_chunkgroup")
    asset = subnet.createDigitalAsset(
        name=chunkgroup.GROUP_TYPE, hda_file_name=library, description="USD Chunkgroup",
        min_num_inputs=0, max_num_inputs=1, ignore_external_references=True)
    definition = asset.type().definition()
    group = hou.ParmTemplateGroup()
    group.append(hou.ButtonParmTemplate(
        chunkgroup.EXPORT_PARM, "Export Network...", script_callback=chunkgroup.EXPORT_SCRIPT,
        script_callback_language=hou.scriptLanguage.Python,
        help="Write this chunkgroup as a usd_chunks network (.usda) that usdview, Blender "
             "and the usdchunks CLI open. Only chunk nodes can be inside it."))
    network_space = hou.FolderParmTemplate(
        "network_space", "Network Space", folder_type=hou.folderType.Collapsible)
    network_space.addParmTemplate(hou.StringParmTemplate(
        space.UP_PARM, "Up Axis", 1, default_value=("Y",), menu_items=("Y", "Z"),
        menu_labels=("Y", "Z"), menu_type=hou.menuType.Normal,
        help="The up axis this network was authored in. Its chunks cook in it, and the "
             "result is turned into Houdini's. Set from the file on import."))
    network_space.addParmTemplate(hou.FloatParmTemplate(
        space.MPU_PARM, "Meters per Unit", 1, default_value=(1.0,), min=0.001, max=1.0,
        min_is_strict=True,
        help="The units this network was authored in (0.01 = centimetres). Its result is "
             "scaled into Houdini's. Set from the file on import."))
    group.append(network_space)
    _finish(definition, group, "A usd_chunks chunkgroup: chunk nodes, kept together, "
                               "exported as one network.")
    definition.save(library, asset)


def build() -> str:
    network = hou.node("/stage") or hou.node("/obj").createNode("lopnet")
    library = os.path.join(tempfile.mkdtemp(), "usdchunks_chunk.hda")
    _chunk_type(network, library)
    _chunkgroup_type(network, library)

    if os.path.exists(OUT):
        shutil.rmtree(OUT) if os.path.isdir(OUT) else os.remove(OUT)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    hou.hda.expandToDirectory(library, OUT)
    return OUT


if __name__ == "__main__":
    print("wrote", build())
