"""Writes examples/assets/motor.step, the CAD file examples/scripts/cad_to_render.py imports.

    pip install cadquery-ocp
    python examples/assets/make_motor_step.py

A small motor assembly in millimetres: a base plate with four bolt holes, a motor body, an
end cap, a shaft and one bolt placed four times, each part with its own colour. The file is
committed, so you only need this to change the model.
"""
from __future__ import annotations

from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_StepModelType
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool


def cylinder(radius, height, at=(0.0, 0.0, 0.0), axis=(0.0, 0.0, 1.0)):
    """A cylinder in millimetres, its base centred on *at*, growing along *axis*."""
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*at), gp_Dir(*axis)), radius, height).Shape()


def write_step(path: Path) -> None:
    """Write a small motor assembly as STEP, in millimetres, with a colour per part."""
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    app.NewDocument(TCollection_ExtendedString("BinXCAF"), doc)
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colours = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    # Base plate with a bolt hole in each corner.
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-110, -70, 0), 220, 140, 12).Shape()
    for x in (-90, 90):
        for y in (-50, 50):
            plate = BRepAlgoAPI_Cut(plate, cylinder(7, 12, (x, y, 0))).Shape()

    # Motor: a body lying along X, an end cap and a shaft sticking out of the front.
    body = cylinder(48, 130, (-75, 0, 62), (1, 0, 0))
    body = BRepAlgoAPI_Fuse(body, BRepPrimAPI_MakeBox(gp_Pnt(-60, -40, 12), 100, 80, 20).Shape()).Shape()
    cap = cylinder(50, 18, (-93, 0, 62), (1, 0, 0))
    shaft = cylinder(9, 55, (55, 0, 62), (1, 0, 0))

    # One bolt, placed four times: CAD Import tessellates it once and instances it.
    bolt = BRepAlgoAPI_Fuse(cylinder(11, 7, (0, 0, 12)), cylinder(6, 18, (0, 0, -4))).Shape()

    def add(shape, name, rgb):
        label = shapes.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        colours.SetColor(label, Quantity_Color(*rgb, Quantity_TOC_RGB), XCAFDoc_ColorType.XCAFDoc_ColorSurf)
        return label

    parts = {
        "Plate": add(plate, "Plate", (0.6, 0.6, 0.62)),
        "Body": add(body, "Body", (0.75, 0.08, 0.05)),
        "Cap": add(cap, "Cap", (0.05, 0.05, 0.05)),
        "Shaft": add(shaft, "Shaft", (0.8, 0.8, 0.8)),
        "Bolt": add(bolt, "Bolt", (0.8, 0.8, 0.8)),
    }

    def at(x=0.0, y=0.0, z=0.0):
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(x, y, z))
        return TopLoc_Location(trsf)

    assembly = shapes.NewShape()
    TDataStd_Name.Set_s(assembly, TCollection_ExtendedString("Motor"))
    placements = [("plate", "Plate", at()), ("body", "Body", at()), ("cap", "Cap", at()),
                  ("shaft", "Shaft", at())]
    placements += [(f"bolt_{i}", "Bolt", at(x, y)) for i, (x, y) in
                   enumerate(((-90, -50), (-90, 50), (90, -50), (90, 50)))]
    for name, part, where in placements:
        component = shapes.AddComponent(assembly, parts[part], where)
        TDataStd_Name.Set_s(component, TCollection_ExtendedString(name))
    shapes.UpdateAssemblies()

    writer = STEPCAFControl_Writer()
    writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))


if __name__ == "__main__":
    write_step(Path(__file__).resolve().parent / "motor.step")
