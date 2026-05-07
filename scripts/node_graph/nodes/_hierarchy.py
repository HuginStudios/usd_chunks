"""Hierarchy nodes — create, delete, reparent, duplicate, rename prims."""

from __future__ import annotations

from pxr import Sdf

from ._base import UsdNodeBase, PORT_COLORS


class CreatePrimNode(UsdNodeBase):
    """Create/define a prim at a path with a type."""

    NODE_NAME = "Create Prim"
    CATEGORY = "Hierarchy"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("prim_path", "Prim Path", text="/NewPrim")
        self.add_text_input("prim_type", "Type", text="Xform")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        path = self.get_property("prim_path") or "/NewPrim"
        typ = self.get_property("prim_type") or "Xform"
        prim = stage.DefinePrim(path, typ)
        return {"prim": prim if prim.IsValid() else None}


class DeletePrimNode(UsdNodeBase):
    """Remove a prim from the stage."""

    NODE_NAME = "Delete Prim"
    CATEGORY = "Hierarchy"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        if prim and prim.IsValid():
            stage.RemovePrim(prim.GetPath())
        return {}


class ReparentPrimNode(UsdNodeBase):
    """Move a prim under a new parent (via rename)."""

    NODE_NAME = "Reparent Prim"
    CATEGORY = "Hierarchy"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("new_parent", "New Parent Path", text="/")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        parent = self.get_property("new_parent") or "/"
        if not prim:
            return {"prim": None}
        old_path = prim.GetPath()
        new_path = Sdf.Path(parent).AppendChild(old_path.name)
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(old_path, new_path)
        stage.GetEditTarget().GetLayer().Apply(edit)
        return {"prim": stage.GetPrimAtPath(new_path)}


class DuplicatePrimNode(UsdNodeBase):
    """Duplicate a prim to a new path."""

    NODE_NAME = "Duplicate Prim"
    CATEGORY = "Hierarchy"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("dest_path", "Destination Path", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        dest = self.get_property("dest_path")
        if not prim or not dest:
            return {"prim": None}
        Sdf.CopySpec(
            stage.GetEditTarget().GetLayer(),
            prim.GetPath(),
            stage.GetEditTarget().GetLayer(),
            Sdf.Path(dest),
        )
        return {"prim": stage.GetPrimAtPath(dest)}


class RenamePrimNode(UsdNodeBase):
    """Rename a prim (change its name, keep same parent)."""

    NODE_NAME = "Rename Prim"
    CATEGORY = "Hierarchy"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("prim", color=PORT_COLORS["prim"])
        self.add_text_input("new_name", "New Name", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        prim = self._get_input_value("prim")
        new_name = (self.get_property("new_name") or "").strip()
        if not prim or not new_name:
            return {"prim": prim}
        old_path = prim.GetPath()
        new_path = old_path.GetParentPath().AppendChild(new_name)
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(old_path, new_path)
        stage.GetEditTarget().GetLayer().Apply(edit)
        return {"prim": stage.GetPrimAtPath(new_path)}
