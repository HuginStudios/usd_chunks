"""Camera nodes — create camera, set active camera."""

from __future__ import annotations

from pxr import UsdGeom

from ._base import UsdNodeBase, PORT_COLORS


class CreateCameraNode(UsdNodeBase):
    NODE_NAME = "Create Camera"
    CATEGORY = "Camera"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("path", "Path", text="/Camera")
        self.add_text_input("focal_length", "Focal Length", text="50")
        self.add_text_input("h_aperture", "Horizontal Aperture", text="36")
        self.add_text_input("near_clip", "Near Clip", text="0.1")
        self.add_text_input("far_clip", "Far Clip", text="10000")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/Camera"
        cam = UsdGeom.Camera.Define(stage, p)
        cam.GetFocalLengthAttr().Set(float(self.get_property("focal_length") or 50))
        cam.GetHorizontalApertureAttr().Set(
            float(self.get_property("h_aperture") or 36)
        )
        cam.GetClippingRangeAttr().Set(
            (
                float(self.get_property("near_clip") or 0.1),
                float(self.get_property("far_clip") or 10000),
            )
        )
        return {"prim": cam.GetPrim()}


class SetActiveCameraNode(UsdNodeBase):
    """Set the active camera in usdview viewport."""

    NODE_NAME = "Set Active Camera"
    CATEGORY = "Camera"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("camera_path", "Camera Path (if no input)", text="")

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if not prim:
            cp = (self.get_property("camera_path") or "").strip()
            if cp:
                prim = stage.GetPrimAtPath(cp)
        if prim and prim.IsValid():
            try:
                from ..graph_host import GraphHost

                GraphHost.current().set_camera_prim(prim)
            except Exception:
                pass
        return {}
