"""Material/shading nodes — display color, assign material, preview surface, textures, shaders."""
from __future__ import annotations

import os

from NodeGraphQt.widgets.node_widgets import NodeBaseWidget
from PySide6 import QtWidgets, QtCore, QtGui
from pxr import Gf, Sdf, UsdGeom, UsdShade, Vt

from ._base import UsdNodeBase, PORT_COLORS


class SetDisplayColorNode(UsdNodeBase):
    """Set displayColor on a mesh prim."""
    NODE_NAME = "Set Display Color"
    CATEGORY = "Material"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("color_r", "R", text="0.5")
        self.add_text_input("color_g", "G", text="0.5")
        self.add_text_input("color_b", "B", text="0.5")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prims = self._get_prims("prim")
        if not prims:
            return {"prim": None}
        r = float(self.get_property("color_r") or 0.5)
        g = float(self.get_property("color_g") or 0.5)
        b = float(self.get_property("color_b") or 0.5)
        color = Vt.Vec3fArray([Gf.Vec3f(r, g, b)])
        for prim in prims:
            gprim = UsdGeom.Gprim(prim)
            if gprim:
                gprim.GetDisplayColorAttr().Set(color)
        return {"prim": prims if len(prims) > 1 else prims[0]}

    def to_python(self, var_map: dict) -> str:
        pv = var_map.get("prim", "prim")
        r = self.get_property("color_r") or "0.5"
        g = self.get_property("color_g") or "0.5"
        b = self.get_property("color_b") or "0.5"
        return f'UsdGeom.Gprim({pv}).GetDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f({r}, {g}, {b})]))'


class AssignMaterialNode(UsdNodeBase):
    """Bind an existing material to a prim."""
    NODE_NAME = "Assign Material"
    CATEGORY = "Material"

    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_input("material", color=PORT_COLORS["material"])
        self.add_text_input("material_path", "Material Path", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prims = self._get_prims("prim")
        mat_prim = self._get_input_value("material")
        mat_path = self.get_property("material_path")
        if not prims:
            return {"prim": None}
        if not mat_prim and mat_path:
            mat_prim = stage.GetPrimAtPath(mat_path)
        if mat_prim and mat_prim.IsValid():
            mat = UsdShade.Material(mat_prim)
            for prim in prims:
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(mat)
        return {"prim": prims if len(prims) > 1 else prims[0]}


class CreatePreviewSurfaceNode(UsdNodeBase):
    """Create a UsdPreviewSurface material."""
    NODE_NAME = "Create PreviewSurface"
    CATEGORY = "Material"

    def __init__(self):
        super().__init__()
        self.add_text_input("mat_path", "Material Path", text="/Materials/MyMaterial")
        self.add_text_input("diffuse_r", "Diffuse R", text="0.8")
        self.add_text_input("diffuse_g", "Diffuse G", text="0.8")
        self.add_text_input("diffuse_b", "Diffuse B", text="0.8")
        self.add_text_input("roughness", "Roughness", text="0.5")
        self.add_text_input("metallic", "Metallic", text="0.0")
        self.add_input("diffuse_texture", color=PORT_COLORS["texture"])
        self.add_output("material", color=PORT_COLORS["material"])
        self.add_output("shader", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        mat_path = self.get_property("mat_path") or "/Materials/MyMaterial"
        r = float(self.get_property("diffuse_r") or 0.8)
        g = float(self.get_property("diffuse_g") or 0.8)
        b = float(self.get_property("diffuse_b") or 0.8)
        rough = float(self.get_property("roughness") or 0.5)
        metal = float(self.get_property("metallic") or 0.0)
        tex_path = self._get_input_value("diffuse_texture")

        mat_prim = stage.DefinePrim(mat_path, "Material")
        mat = UsdShade.Material(mat_prim)
        shader = UsdShade.Shader.Define(stage, f"{mat_path}/PBRShader")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metal)

        if tex_path:
            tex_reader = UsdShade.Shader.Define(stage, f"{mat_path}/DiffuseTexture")
            tex_reader.CreateIdAttr("UsdUVTexture")
            tex_reader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(tex_path))
            tex_reader.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
            tex_reader.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
            tex_reader.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
            # UV reader
            uv_reader = UsdShade.Shader.Define(stage, f"{mat_path}/UVReader")
            uv_reader.CreateIdAttr("UsdPrimvarReader_float2")
            uv_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
            uv_reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
            tex_reader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
                uv_reader.ConnectableAPI(), "result")
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
                tex_reader.ConnectableAPI(), "rgb")
        else:
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(r, g, b))

        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        return {"material": mat_prim, "shader": shader.GetPrim()}


class _ThumbnailLabel(QtWidgets.QLabel):
    """QLabel with setTitleAlign for NodeGraphQt compat."""
    def setTitleAlign(self, *a): pass


class _ThumbnailWidget(NodeBaseWidget):
    """Embeds a resizable thumbnail image inside a node."""
    def __init__(self, parent=None, name="thumbnail", label=""):
        super().__init__(parent, name, label)
        self._widget = _ThumbnailLabel()
        self._widget.setMinimumSize(180, 180)
        self._widget.setStyleSheet("background: #111; border: 1px solid #333;")
        self._widget.setAlignment(QtCore.Qt.AlignCenter)
        self._widget.setScaledContents(True)
        self.setWidget(self._widget)
        self._path = ""

    def get_value(self):
        return self._path

    def set_value(self, path=""):
        self._path = path
        if path:
            pixmap = QtGui.QPixmap(path)
            if not pixmap.isNull():
                # Keep original quality, let the widget handle display
                self._widget.setPixmap(pixmap)
                return
        self._widget.setText("No image")


class TextureFileNode(UsdNodeBase):
    """Load a texture file path. Shows preview on the node. Drag corner to resize."""
    NODE_NAME = "Texture File"
    CATEGORY = "Material"
    _thumb_size = 180

    def __init__(self):
        super().__init__()
        self.add_text_input("file_path", "File Path", text="")
        self._thumb = _ThumbnailWidget(self.view, name="thumb")
        self.add_custom_widget(self._thumb)
        self.add_output("texture", color=PORT_COLORS["texture"])
        self.set_property("width", 220)

    def _on_resize(self, width, height=None):
        self._thumb_size = max(80, width - 40)
        self._thumb._widget.setMinimumSize(self._thumb_size, self._thumb_size)
        self._thumb._widget.setMaximumSize(self._thumb_size, self._thumb_size)
        fp = self.get_property("file_path") or ""
        if fp:
            self._thumb.set_value(fp)
        super()._on_resize(width, height)

    def execute(self, stage) -> dict:
        fp = self.get_property("file_path") or ""
        if fp:
            if os.path.isfile(fp):
                self._thumb.set_value(fp)
                self.set_icon(fp)
            self.set_name(f"Texture: {os.path.basename(fp)}")
        return {"texture": fp}


class CreateUVTextureNode(UsdNodeBase):
    """Create a UsdUVTexture shader with tiling controls."""
    NODE_NAME = "Create UV Texture"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Shader Path", text="/Materials/MyMaterial/Texture")
        self.add_input("file", color=PORT_COLORS["texture"])
        self.add_text_input("file_path", "File (if no input)", text="")
        self.add_text_input("scale_s", "Scale S (tile U)", text="1.0")
        self.add_text_input("scale_t", "Scale T (tile V)", text="1.0")
        self.add_text_input("offset_s", "Offset S", text="0.0")
        self.add_text_input("offset_t", "Offset T", text="0.0")
        self.add_text_input("wrap_s", "Wrap S (repeat/clamp/mirror)", text="repeat")
        self.add_text_input("wrap_t", "Wrap T (repeat/clamp/mirror)", text="repeat")
        self.add_output("rgb", color=PORT_COLORS["any"])
        self.add_output("r", color=PORT_COLORS["float"])
        self.add_output("a", color=PORT_COLORS["float"])
        self.add_output("shader_prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Materials/MyMaterial/Texture"
        fp = self._get_input_value("file") or (self.get_property("file_path") or "").strip()
        shader = UsdShade.Shader.Define(stage, p)
        shader.CreateIdAttr("UsdUVTexture")
        if fp:
            shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(fp))
        shader.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set(
            self.get_property("wrap_s") or "repeat")
        shader.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set(
            self.get_property("wrap_t") or "repeat")
        # ST transform via UsdTransform2d
        ss = float(self.get_property("scale_s") or 1)
        st = float(self.get_property("scale_t") or 1)
        os_ = float(self.get_property("offset_s") or 0)
        ot = float(self.get_property("offset_t") or 0)
        if ss != 1 or st != 1 or os_ != 0 or ot != 0:
            parent = str(Sdf.Path(p).GetParentPath())
            # Create primvar reader
            uv_reader = UsdShade.Shader.Define(stage, f"{parent}/UVReader")
            uv_reader.CreateIdAttr("UsdPrimvarReader_float2")
            uv_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
            uv_reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
            # Create transform2d
            xform = UsdShade.Shader.Define(stage, f"{parent}/UVTransform")
            xform.CreateIdAttr("UsdTransform2d")
            xform.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(
                uv_reader.ConnectableAPI(), "result")
            xform.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(ss, st))
            xform.CreateInput("translation", Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(os_, ot))
            xform.CreateOutput("result", Sdf.ValueTypeNames.Float2)
            shader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
                xform.ConnectableAPI(), "result")
        shader.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        shader.CreateOutput("r", Sdf.ValueTypeNames.Float)
        shader.CreateOutput("a", Sdf.ValueTypeNames.Float)
        return {"rgb": f"{p}.rgb", "r": f"{p}.r", "a": f"{p}.a",
                "shader_prim": shader.GetPrim()}


class CreatePrimvarReaderNode(UsdNodeBase):
    """Create a UsdPrimvarReader_float2 for UV coordinates."""
    NODE_NAME = "Create Primvar Reader"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Shader Path", text="/Materials/MyMaterial/UVReader")
        self.add_text_input("varname", "Primvar Name", text="st")
        self.add_output("result", color=PORT_COLORS["any"])
        self.add_output("shader_prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Materials/MyMaterial/UVReader"
        shader = UsdShade.Shader.Define(stage, p)
        shader.CreateIdAttr("UsdPrimvarReader_float2")
        shader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set(
            self.get_property("varname") or "st")
        shader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
        return {"result": f"{p}.result", "shader_prim": shader.GetPrim()}


class GetMaterialShadersNode(UsdNodeBase):
    """Get the surface and displacement shaders from a material."""
    NODE_NAME = "Get Material Shaders"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("material", color=PORT_COLORS["material"])
        self.add_text_input("material_path", "Material Path (if no input)", text="")
        self.add_output("surface", color=PORT_COLORS["prim"])
        self.add_output("displacement", color=PORT_COLORS["prim"])
        self.add_output("shader_list", color=PORT_COLORS["list"])
    def execute(self, stage) -> dict:
        mat_prim = self._get_input_value("material")
        if mat_prim is None:
            mp = (self.get_property("material_path") or "").strip()
            if mp:
                mat_prim = stage.GetPrimAtPath(mp)
        if mat_prim is None:
            return {"surface": None, "displacement": None, "shader_list": []}
        mat = UsdShade.Material(mat_prim)
        surf_out = mat.GetSurfaceOutput()
        disp_out = mat.GetDisplacementOutput()
        surf = disp = None
        if surf_out:
            src = surf_out.GetConnectedSources()
            if src and src[0]:
                surf = src[0][0].source.GetPrim()
        if disp_out:
            src = disp_out.GetConnectedSources()
            if src and src[0]:
                disp = src[0][0].source.GetPrim()
        # List all shader children
        shaders = [c for c in mat_prim.GetChildren()
                   if UsdShade.Shader(c).GetShaderId()]
        return {"surface": surf, "displacement": disp, "shader_list": shaders}


class GetShaderInputNode(UsdNodeBase):
    """Read an input value from a shader (roughness, diffuseColor, file, etc.)."""
    NODE_NAME = "Get Shader Input"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("shader", color=PORT_COLORS["prim"])
        self.add_text_input("input_name", "Input Name", text="diffuseColor")
        self.add_output("value", color=PORT_COLORS["any"])
        self.add_output("connected_source", color=PORT_COLORS["prim"])
        self.add_output("all_inputs", color=PORT_COLORS["list"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("shader")
        if prim is None:
            return {"value": None, "connected_source": None, "all_inputs": []}
        shader = UsdShade.Shader(prim)
        name = (self.get_property("input_name") or "").strip()
        # List all input names
        all_inputs = [i.GetBaseName() for i in shader.GetInputs()]
        if not name:
            return {"value": None, "connected_source": None, "all_inputs": all_inputs}
        inp = shader.GetInput(name)
        if not inp:
            return {"value": None, "connected_source": None, "all_inputs": all_inputs}
        # Check if connected to another shader
        src = inp.GetConnectedSources()
        connected = None
        if src and src[0]:
            connected = src[0][0].source.GetPrim()
        val = inp.Get()
        return {"value": val, "connected_source": connected, "all_inputs": all_inputs}


class SetShaderInputNode(UsdNodeBase):
    """Set an input value on a shader."""
    NODE_NAME = "Set Shader Input"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("shader", color=PORT_COLORS["prim"])
        self.add_input("value", color=PORT_COLORS["any"])
        self.add_text_input("input_name", "Input Name", text="roughness")
        self.add_text_input("manual_value", "Value (if no input)", text="")
        self.add_output("shader", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("shader")
        if prim is None:
            return {"shader": None}
        shader = UsdShade.Shader(prim)
        name = (self.get_property("input_name") or "").strip()
        val = self._get_input_value("value")
        if val is None:
            manual = (self.get_property("manual_value") or "").strip()
            if manual:
                try:
                    val = eval(manual)
                except Exception:
                    val = manual
        if name and val is not None:
            inp = shader.GetInput(name)
            if inp:
                inp.Set(val)
        return {"shader": prim}


class ConnectShaderNode(UsdNodeBase):
    """Connect a shader output to another shader's input."""
    NODE_NAME = "Connect Shaders"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("source_shader", color=PORT_COLORS["prim"])
        self.add_text_input("source_output", "Source Output", text="rgb")
        self.add_input("dest_shader", color=PORT_COLORS["prim"])
        self.add_text_input("dest_input", "Dest Input", text="diffuseColor")
        self.add_output("dest_shader", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        src_prim = self._get_input_value("source_shader")
        dst_prim = self._get_input_value("dest_shader")
        if src_prim is None or dst_prim is None:
            return {"dest_shader": dst_prim}
        src = UsdShade.Shader(src_prim)
        dst = UsdShade.Shader(dst_prim)
        src_out = self.get_property("source_output") or "rgb"
        dst_in = self.get_property("dest_input") or "diffuseColor"
        inp = dst.GetInput(dst_in)
        if inp:
            inp.ConnectToSource(src.ConnectableAPI(), src_out)
        return {"dest_shader": dst_prim}


class SetUVTransformNode(UsdNodeBase):
    """Set UV tiling by inserting/updating a UsdTransform2d between UV reader and texture.
    Connect the texture shader prim — it will find the st source and insert a transform."""
    NODE_NAME = "Set UV Transform"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("shader", color=PORT_COLORS["prim"])
        self.add_text_input("scale_s", "Scale S (tile U)", text="1.0")
        self.add_text_input("scale_t", "Scale T (tile V)", text="1.0")
        self.add_text_input("offset_s", "Offset S", text="0.0")
        self.add_text_input("offset_t", "Offset T", text="0.0")
        self.add_text_input("rotation", "Rotation (degrees)", text="0.0")
        self.add_output("shader", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("shader")
        if prim is None:
            return {"shader": None}
        shader = UsdShade.Shader(prim)
        ss = float(self.get_property("scale_s") or 1)
        st_val = float(self.get_property("scale_t") or 1)
        os_ = float(self.get_property("offset_s") or 0)
        ot = float(self.get_property("offset_t") or 0)
        rot = float(self.get_property("rotation") or 0)

        # Find the st input on this texture shader
        st_inp = shader.GetInput("st")
        if not st_inp:
            return {"shader": prim}

        # Get what's currently connected to st (should be a primvar reader)
        src = st_inp.GetConnectedSources()
        uv_source_shader = None
        uv_source_output = "result"
        if src and src[0]:
            uv_source_shader = src[0][0].source
            uv_source_output = src[0][0].sourceName

        # Create or find the transform2d shader next to this texture
        mat_path = prim.GetParent().GetPath()
        xform_path = f"{mat_path}/UVTransform"
        xform_shader = UsdShade.Shader.Define(stage, xform_path)
        xform_shader.CreateIdAttr("UsdTransform2d")
        xform_shader.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(
            Gf.Vec2f(ss, st_val))
        xform_shader.CreateInput("translation", Sdf.ValueTypeNames.Float2).Set(
            Gf.Vec2f(os_, ot))
        xform_shader.CreateInput("rotation", Sdf.ValueTypeNames.Float).Set(rot)
        xform_shader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

        # Wire: UV reader → Transform2d → Texture st
        if uv_source_shader:
            xform_shader.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(
                uv_source_shader, uv_source_output)
        st_inp.ConnectToSource(xform_shader.ConnectableAPI(), "result")
        return {"shader": prim}


class GetPrimMaterialNode(UsdNodeBase):
    """Get the material bound to a prim, its surface shader, and all texture file paths."""
    NODE_NAME = "Get Prim Material"
    CATEGORY = "Material"
    def __init__(self):
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_output("material", color=PORT_COLORS["material"])
        self.add_output("shader", color=PORT_COLORS["prim"])
        self.add_output("textures", color=PORT_COLORS["list"])
        self.add_output("mat_path", color=PORT_COLORS["string"])
    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim is None:
            return {"material": None, "shader": None, "textures": [], "mat_path": ""}
        # Walk up to find material binding
        binding = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        mat = binding[0] if binding else None
        if not mat or not mat.GetPrim().IsValid():
            return {"material": None, "shader": None, "textures": [], "mat_path": ""}
        mat_prim = mat.GetPrim()
        # Find surface shader
        surf = None
        surf_out = mat.GetSurfaceOutput()
        if surf_out:
            src = surf_out.GetConnectedSources()
            if src and src[0]:
                surf = src[0][0].source.GetPrim()
        # Collect all texture file paths from child shaders
        textures = []
        for child in mat_prim.GetAllChildren():
            shader = UsdShade.Shader(child)
            if not shader:
                continue
            file_inp = shader.GetInput("file")
            if file_inp:
                val = file_inp.Get()
                if val:
                    path = val.path if hasattr(val, 'path') else str(val)
                    if path:
                        textures.append({"shader": str(child.GetPath()), "file": path})
        return {
            "material": mat_prim,
            "shader": surf,
            "textures": textures,
            "mat_path": str(mat_prim.GetPath()),
        }
