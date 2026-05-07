"""Lighting nodes — rect, sphere, distant, dome, disk lights."""
from __future__ import annotations

from pxr import Gf, Sdf, UsdLux

from ._base import UsdNodeBase, PORT_COLORS


class CreateRectLightNode(UsdNodeBase):
    NODE_NAME = "Create Rect Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/RectLight")
        self.add_text_input("w", "Width", text="2.0")
        self.add_text_input("h", "Height", text="2.0")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_text_input("color_r", "Color R", text="1.0")
        self.add_text_input("color_g", "Color G", text="1.0")
        self.add_text_input("color_b", "Color B", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/RectLight"
        light = UsdLux.RectLight.Define(stage, p)
        light.GetWidthAttr().Set(float(self.get_property("w") or 2))
        light.GetHeightAttr().Set(float(self.get_property("h") or 2))
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        r = float(self.get_property("color_r") or 1)
        g = float(self.get_property("color_g") or 1)
        b = float(self.get_property("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        return {"prim": light.GetPrim()}


class CreateSphereLightNode(UsdNodeBase):
    NODE_NAME = "Create Sphere Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/SphereLight")
        self.add_text_input("radius", "Radius", text="0.5")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_text_input("color_r", "Color R", text="1.0")
        self.add_text_input("color_g", "Color G", text="1.0")
        self.add_text_input("color_b", "Color B", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/SphereLight"
        light = UsdLux.SphereLight.Define(stage, p)
        light.GetRadiusAttr().Set(float(self.get_property("radius") or 0.5))
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        r = float(self.get_property("color_r") or 1)
        g = float(self.get_property("color_g") or 1)
        b = float(self.get_property("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        return {"prim": light.GetPrim()}


class CreateDistantLightNode(UsdNodeBase):
    NODE_NAME = "Create Distant Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/DistantLight")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_text_input("angle", "Angle", text="0.53")
        self.add_text_input("color_r", "Color R", text="1.0")
        self.add_text_input("color_g", "Color G", text="1.0")
        self.add_text_input("color_b", "Color B", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/DistantLight"
        light = UsdLux.DistantLight.Define(stage, p)
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        light.GetAngleAttr().Set(float(self.get_property("angle") or 0.53))
        r = float(self.get_property("color_r") or 1)
        g = float(self.get_property("color_g") or 1)
        b = float(self.get_property("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        return {"prim": light.GetPrim()}


class CreateDomeLightNode(UsdNodeBase):
    NODE_NAME = "Create Dome Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/DomeLight")
        self.add_text_input("intensity", "Intensity", text="1.0")
        self.add_text_input("texture", "Texture File (HDRI)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/DomeLight"
        light = UsdLux.DomeLight.Define(stage, p)
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 1))
        tex = (self.get_property("texture") or "").strip()
        if tex:
            light.GetTextureFileAttr().Set(Sdf.AssetPath(tex))
        return {"prim": light.GetPrim()}


class CreateDiskLightNode(UsdNodeBase):
    NODE_NAME = "Create Disk Light"
    CATEGORY = "Lighting"
    def __init__(self):
        super().__init__()
        self.add_text_input("path", "Path", text="/DiskLight")
        self.add_text_input("radius", "Radius", text="1.0")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_output("prim", color=PORT_COLORS["prim"])
    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/DiskLight"
        light = UsdLux.DiskLight.Define(stage, p)
        light.GetRadiusAttr().Set(float(self.get_property("radius") or 1))
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        return {"prim": light.GetPrim()}
