from usd_chunks import chunk, chunk_registry, chunk_notice, chunk_core
from typing import List, Optional

from pxr import Usd
import hou


class HoudiniSceneProxy(chunk_notice.SceneSender):
    def __init__(self, lopnode: hou.LopNode, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.node = lopnode
        self.chunk: Optional[chunk.Chunk] = None

    def get_stage(self) -> Usd.Stage:
        if not self.node:
            raise AttributeError("No valid node. Could not get stage")

        if stage := self.node.editableStage():
            return stage

        if stage := self.node.stage():
            return stage

        raise AttributeError("Could not get stage")

    def get_selected_prims(self) -> List[Usd.Prim]:
        """
        Get the selected prims in the scene

        Returns:
            List[Usd.Prim]: Selected prims
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
        """
        Get the selected property in the scene

        Returns:
            Usd.Property: Selected property
        """
        raise NotImplementedError


def parm_template_from_chunk_param(param: chunk.ChunkParamBase):
    if isinstance(param, chunk_core.ProxyParam):
        return None, None

    supported_types = chunk.ChunkFlags.ParamTypeToTypes(param.evaluate_type())
    fallback = supported_types[0]() if supported_types else 0
    default = param.default_value if param.default_value is not None else fallback

    parm = None
    snip = f"parent.evalParm('{param.name}')"

    if chunk.ChunkFlags.ParamType.FLOAT in param.evaluate_type():
        parm = hou.FloatParmTemplate(param.name, param.get_label(), 1, default_value=(default,))
    elif chunk.ChunkFlags.ParamType.INT in param.evaluate_type():
        parm = hou.IntParmTemplate(param.name, param.get_label(), 1, default_value=(default,))
    elif chunk.ChunkFlags.ParamType.BOOL in param.evaluate_type():
        parm = hou.ToggleParmTemplate(param.name, param.get_label(), default)
    elif chunk.ChunkFlags.ParamType.STRING_MENU in param.evaluate_type():
        snip = f"parent.parm('{param.name}').evalAsString()"
        parm = hou.MenuParmTemplate(param.name, param.get_label(), param.menu_items, param.menu_items, 0)

    return parm, snip


def parm_template_from_chunk_group(group: chunk_core.ChunkContainerBase):
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

    node: Optional[hou.LopNode] = owner.user_data.get("node")
    if not node:
        return

    # ln, ls = owner.get_last_change()
    if chunk_notice.is_notice_type(notice, chunk_notice.ChunkNoticeAlias.STATE_CHANGED):
        for i, c in enumerate(owner.iter_changes()):
            print(" " * i, c[1].name)


_py_code_template = """\
from usd_chunks import chunk, chunk_registry
import chunk_houdini

reg = chunk_registry.ChunkRegistry()
parent = hou.pwd().parent()
c = reg.get_chunk(parent.evalParm("chunk_name"))

c.add_callback(chunk_houdini.handle_state_changed, notice_alias=chunk.ChunkNoticeAlias.STATE_CHANGED)

"""


def create_lop_node(chunk_name: str):
    root = hou.node("/stage")
    if not root:
        return

    node_name = "_".join(chunk_name.split()).split(".")[-1]
    
    if old_node := root.node(node_name):
        old_node.destroy()

    node: hou.LopNode = root.createNode("subnet", node_name)
    py_node = node.createNode("pythonscript", "callbacks")

    py_node.setInput(0, node.indirectInputs()[0])
    node.subnetOutputs()[0].setInput(0, py_node)

    scene = HoudiniSceneProxy(py_node)

    reg = chunk_registry.ChunkRegistry()
    if reg.initialized:
        for name, mod in reg.registered_modules.items():
            reg.unload_chunk_module(mod)

    reg.initialize(scene, "chunk_houdini", unloaded=True)

    module = reg.get_chunk_module(chunk_name)
    if not module:
        raise ModuleNotFoundError("Could not find chunk module:", chunk_name)

    if not module.load() or not module.loaded_chunk:
        return

    module.loaded_chunk.user_data["node"] = node
    scene.chunk = module.loaded_chunk

    code = _py_code_template

    grp = node.parmTemplateGroup()

    folder, eval_strs = parm_template_from_chunk_group(module.loaded_chunk)
    code += eval_strs
    py_node.parm("python").set(code)

    name_param = hou.StringParmTemplate("chunk_name", "chunk", 1, [module.loaded_chunk.name], is_hidden=True)
    grp.addParmTemplate(name_param)
    grp.addParmTemplate(folder)
    node.setParmTemplateGroup(grp)
