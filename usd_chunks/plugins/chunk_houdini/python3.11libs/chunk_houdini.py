"""The Houdini host: runs chunks inside a LOP network, one LOP node per chunk.

Each chunk's params become the node's parameter interface, and the node's editable
stage is the stage the chunk authors.
"""
from __future__ import annotations

from typing_extensions import override
from usd_chunks import chunk, chunk_registry, chunk_core
from usd_chunks.chunk_flags import ChunkFlags


from pxr import Usd
import hou


class HoudiniSceneProxy(chunk_core.SceneSender):
    """The SceneSender for one Houdini LOP node, reading the stage and selection from it.

    Args:
        lopnode: The LOP node hosting the chunk.
    """
    def __init__(self, lopnode: hou.LopNode, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.node = lopnode
        self.chunk: chunk.Chunk | None = None

    @override
    def get_stage(self) -> Usd.Stage:
        if not self.node:
            raise AttributeError("No valid node. Could not get stage")

        if stage := self.node.editableStage():
            return stage

        if stage := self.node.stage():
            return stage

        raise AttributeError("Could not get stage")

    def get_selected_prims(self) -> list[Usd.Prim]:
        """The prims matched by the node's "prims" selection parameter.

        Returns:
            list[Usd.Prim]: Selected prims
        """
        res = []

        param = self.node.parm("prims")
        if not param:
            return []

        sel = hou.LopSelectionRule(param.evalAsString())

        for path in sel.expandedPaths(self.node):
            res.append(self.get_stage().GetPrimAtPath(path))
        return res

    def get_selected_property(self) -> Usd.Property:
        """Not supported in Houdini.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError


def parm_template_from_chunk_param(param: chunk.ChunkParamBase):
    """The Houdini parm template for one chunk param, and the callback code that syncs it.

    Args:
        param: The param.

    Returns:
        (parm template, callback code), or (None, None) for a param with no template.
    """
    if isinstance(param, chunk_core.ProxyParam):
        return None, None

    supported_types = ChunkFlags.ParamTypeToTypes(param.evaluate_type())
    fallback = supported_types[0]() if supported_types else 0
    default = param.default_value if param.default_value is not None else fallback

    parm = None
    snip = f"parent.evalParm('{param.name}')"

    if ChunkFlags.ParamType.FLOAT in param.evaluate_type():
        parm = hou.FloatParmTemplate(param.name, param.get_label(), 1, default_value=(default,))
    elif ChunkFlags.ParamType.INT in param.evaluate_type():
        parm = hou.IntParmTemplate(param.name, param.get_label(), 1, default_value=(default,))
    elif ChunkFlags.ParamType.BOOL in param.evaluate_type():
        parm = hou.ToggleParmTemplate(param.name, param.get_label(), default)
    elif ChunkFlags.ParamType.STRING_MENU in param.evaluate_type():
        snip = f"parent.parm('{param.name}').evalAsString()"
        parm = hou.MenuParmTemplate(param.name, param.get_label(), param.menu_items, param.menu_items, 0)

    return parm, snip


def parm_template_from_chunk_group(group: chunk_core.ChunkContainerBase):
    """A Houdini folder parm template for a chunk group, with its children inside.

    Args:
        group: The group or chunk.

    Returns:
        (folder template, callback code).
    """
    folder_type = hou.folderType.Collapsible if not isinstance(group, chunk.ChunkBase) else hou.folderType.Simple
    grp = hou.FolderParmTemplate(group.name, group.get_label(), folder_type=folder_type)
    code = ""

    for param in group.get_my_params():
        if isinstance(param, chunk_core.ProxyParam):
            continue

        if isinstance(param, chunk.ChunkParamBase):
            templ, eval_str = parm_template_from_chunk_param(param)
            if templ:
                grp.addParmTemplate(templ)
                code += f"c.get_param('{param.name}').value = {eval_str}\n"

        elif isinstance(param, chunk_core.ChunkContainerBase):
            templ, eval_codes = parm_template_from_chunk_group(param)
            if templ:
                grp.addParmTemplate(templ)
                code += eval_codes
    return grp, code


def handle_state_changed(owner: chunk.Chunk, notice, sender):
    """STATE_CHANGED callback for a chunk hosted on a LOP node. Nothing is mirrored yet.

    It only looks up the chunk's node; reflecting hidden or disabled state onto the node's
    parameters is still to be written.

    Args:
        owner: The chunk, whose user_data holds its node.
        notice: The STATE_CHANGED notice.
        sender: The sender.
    """
    node: hou.LopNode | None = owner.user_data.get("node")
    if not node:
        return

    # ln, ls = owner.get_last_change()
    # if chunk_notice.is_notice_type(notice, chunk_notice.ChunkNoticeAlias.STATE_CHANGED):
    #     for i, c in enumerate(owner.get_dag_node().iter_changes()):
    #         print(" " * i, c[1].name)


_py_code_template = """\
from usd_chunks import chunk, chunk_registry
import chunk_houdini

reg = chunk_registry.ChunkRegistry()
parent = hou.pwd().parent()
c = reg.get_chunk(parent.evalParm("chunk_name"))

c.add_callback(chunk_houdini.handle_state_changed, notice_alias=chunk.ChunkNoticeAlias.STATE_CHANGED)

"""


def create_lop_node(chunk_name: str):
    """Build the LOP subnet that hosts a chunk under /stage, replacing one of the same name.

    The subnet holds a Python Script node whose code wires the chunk's callbacks, and gets
    the chunk's params as its parameter interface. Does nothing without a /stage network.

    Args:
        chunk_name: The chunk module's name.

    Raises:
        ModuleNotFoundError: No chunk module has that name.
    """
    root = hou.node("/stage")
    if not root:
        return

    node_name = "_".join(chunk_name.split()).split(".")[-1]

    if old_node := root.node(node_name):
        old_node.destroy()

    node: hou.LopNode = root.createNode("subnet", node_name)  # type: ignore
    py_node: hou.LopNode = node.createNode("pythonscript", "callbacks")  # type: ignore

    py_node.setInput(0, node.indirectInputs()[0])
    node.subnetOutputs()[0].setInput(0, py_node)

    scene = HoudiniSceneProxy(py_node)

    reg = chunk_registry.ChunkRegistry()
    if reg.initialized:
        reg.scene.clear()

    reg.initialize(scene, "chunk_houdini")

    module = reg.get_chunk_module(chunk_name)
    if not module:
        raise ModuleNotFoundError("Could not find chunk module:", chunk_name)

    if not (chunks := module.load()):
        return

    for c in chunks:
        c.user_data["node"] = node
    reg.scene.add_chunks(chunks)

    code = _py_code_template

    grp = node.parmTemplateGroup()

    for c in chunks:
        folder, eval_strs = parm_template_from_chunk_group(c)
        code += eval_strs
        py_node.parm("python").set(code)
        name_param = hou.StringParmTemplate("chunk_name", "chunk", 1, [c.name], is_hidden=True)
        grp.addParmTemplate(name_param)
        grp.addParmTemplate(folder)
        node.setParmTemplateGroup(grp)
