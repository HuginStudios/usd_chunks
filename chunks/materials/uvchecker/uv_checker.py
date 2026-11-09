from __future__ import annotations
import logging
import os
from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_utils

try:
    from PySide6 import QtWidgets
except ImportError:  # Blender has no Qt: the chunk still works, the error goes to the log
    QtWidgets = None

logger = logging.getLogger(__name__)


def _apply_material(owner: chunk.Chunk, prims) -> None:
    """Core work: build the UV checker material and bind it to `prims`. Shared by the
    interactive Button callback (apply, below -- validates and pops a dialog first) and
    the reserved execute(node) hook (which must never show a modal dialog mid-load).
    """
    # Access the parameter inside the group
    x_scale_param = owner.get_param("uvscale", chunk.is_inst(chunk.Vector))[0]
    y_scale_param = owner.get_param("uvscale", chunk.is_inst(chunk.Vector))[1]

    x_scale = x_scale_param.value
    y_scale = y_scale_param.value

    filebrowser = owner.get_param("browse", chunk.is_param_t(str))
    checker_file = filebrowser.value

    stage = owner.get_stage()
    if not stage:
        return

    path_to_uv = checker_file or os.path.join(os.path.dirname(__file__), "CustomUVChecker_byValle_2K.png")
    chunk_utils.bind_uv_checker(stage, prims, path_to_uv, (x_scale, y_scale))


def apply(owner: chunk.Chunk, notice, sender):
    # The "prim" param (not live viewport selection) so this is replayable on reload --
    # nothing is selected in the viewport at load time, but the param's own stored path
    # restores normally like any other value.
    prim_param = owner.get_param("prim", chunk.is_inst(chunk.LineEdit))
    prims = prim_param.get_prims() if prim_param else []
    stage = owner.get_stage()

    if not prims or (stage and prims == stage.GetPseudoRoot()):
        msg = "Prim not selected."
        if prims:
            msg = "Cannot apply material on the top PseudoRoot!"
        if QtWidgets is None:
            logger.warning("UV Checker: %s", msg)
            return
        error_dlg = QtWidgets.QMessageBox()
        error_dlg.setWindowTitle("Error")
        error_dlg.setText(msg)
        error_dlg.exec_()
        return

    _apply_material(owner, prims)


def execute(node: chunk.Chunk) -> None:
    """Reserved top-level reactive hook -- load_chunks calls this once per chunk after
    bulk-restoring its param values, so a reloaded UV Checker actually reapplies the
    material instead of leaving the target prim unbound (apply is a Button callback,
    which load's value-restore deliberately does not trigger). Calls the shared core
    directly rather than apply() itself -- apply()'s own validation pops a modal
    QMessageBox on an empty selection, which must never happen mid-load.
    """
    prim_param = node.get_param("prim", chunk.is_inst(chunk.LineEdit))
    prims = prim_param.get_prims() if prim_param else []
    if not prims:
        return
    _apply_material(node, prims)


def clear(owner: chunk.Chunk, notice, sender):
    """Remove the assign checker edits"""
    chunk_registry.ChunkLayerManager().clear_layer(owner)


def scale(owner: chunk.Vector, notice, sender):
    stage = owner.get_stage()
    if not stage:
        return None

    base_material_grp = "/materials"
    material_path = f"{base_material_grp}/UVCheckerMaterial"
    transform2d_path = f"{material_path}/transform2d"

    prim = stage.GetPrimAtPath(transform2d_path)
    if not prim:
        return

    scale_attr = prim.GetAttribute("inputs:scale")
    scale_val = scale_attr.Get()

    # idx = 1 if param.name == "scale1" else 0
    scale_val[0] = owner[0].value
    scale_val[1] = owner[1].value
    scale_attr.Set(scale_val)


def main():
    chunk_api = chunk.Chunk("UV Checker")

    prim_param = chunk.LineEdit(
        "prim",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prim to visualize UVs on — pick it in the viewport or type a path.",
    )

    spinbox = chunk.Spinbox(default_value=1)
    vec = chunk.Vector("uvscale", "UV Scale", param=spinbox, length=2, suffixes="UV")

    # Adds a callback that runs when any of the spinboxes changes
    vec.add_callback(scale)

    filebrowser = chunk.FileBrowser("browse")

    visUVsButton = chunk.Button("apply", "Visualize")
    clearUVsButton = chunk.Button("clear", "Clear Vis")

    chunk_api.add_params([prim_param, vec, filebrowser, visUVsButton, clearUVsButton])

    # Setting the sender explicitly. The callback will then get params as such:
    # 1 - chunk_api (the chunk)
    # 2 - Tf.Notice (the notice)
    # 3 - ChunkParam (the button)

    chunk_api.add_callback(apply, sender=visUVsButton, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_CHANGED)
    chunk_api.add_callback(clear, sender=clearUVsButton)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MATERIALS,
        icon="image",
        description="""
        Binds a UV checker texture material to prims, to inspect their UV layout.

        UV Scale sets how often the checker repeats in U and V, and the file browser picks
        the checker image. Apply builds the material and binds it; Clear Vis clears this
        chunk's layer, removing it again.
        """,
    )
