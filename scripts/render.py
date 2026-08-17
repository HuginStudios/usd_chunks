"""Render chunk — the ROP. Runs a renderer over the composed stage using the
UsdRender.Settings prim a Render Settings chunk authored.

The render itself happens in commit(), not execute(): execute is the cook, re-run
constantly while the DAG settles, whereas a render must happen once, after the
numbers have stopped moving. That also means `usdchunks run network.usda` renders
without any extra flag, since the CLI calls commit_all() once the solve is done.

Requires: pxr (OpenUSD), usd_chunks, and a renderer on PATH (usdrecord by default).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

from pxr import UsdUtils

from usd_chunks import chunk


logger = logging.getLogger(__name__)


def _staging_dir(stage):
    """Where to write the flattened stage handed to the renderer.

    Next to the root layer when there is one, so relative asset paths inside the
    network still resolve; the system temp dir only when the stage has never been
    saved, where relative paths are already unresolvable anyway.
    """
    root_layer = stage.GetRootLayer()
    if root_layer and root_layer.realPath:
        return os.path.dirname(root_layer.realPath)
    return tempfile.gettempdir()


def _export_for_render(stage):
    """Flatten the layer stack to a file the renderer can open.

    FlattenLayerStack rather than Stage.Export: it collapses the local layers but
    leaves references to external assets as references, so a network referencing a
    large asset writes a small file instead of inlining the whole thing.
    """
    flat = UsdUtils.FlattenLayerStack(stage)
    fd, path = tempfile.mkstemp(suffix=".usda", prefix="usdchunks_render_", dir=_staging_dir(stage))
    os.close(fd)
    flat.Export(path)
    return path


def _build_argv(node, scene_path):
    command = node.get_param("render_command").value or "usdrecord"
    argv = [command]

    if renderer := node.get_param("renderer").value:
        argv += ["--renderer", renderer]
    if frames := node.get_param("frames").value:
        argv += ["--frames", frames]
    if camera := node.get_param("camera").value:
        argv += ["--camera", camera]
    if width := int(node.get_param("image_width").value or 0):
        argv += ["--imageWidth", str(width)]
    if extra := node.get_param("extra_args").value:
        argv += extra.split()

    argv.append(scene_path)
    argv.append(node.get_param("output_image").value or "render.####.png")
    return argv


def _render(node):
    stage = node.get_stage()
    if not stage:
        return

    command = node.get_param("render_command").value or "usdrecord"
    if shutil.which(command) is None:
        logger.error(
            "Render: %r not found on PATH. usd-core wheels do not ship usdrecord — "
            "point Render Command at a full USD build's usdrecord, or at husk or "
            "another renderer entry point.",
            command,
        )
        return

    scene_path = _export_for_render(stage)
    argv = _build_argv(node, scene_path)
    keep = node.get_param("keep_scene_file").value

    logger.info("Render: %s", " ".join(argv))
    try:
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("Render failed (exit %s): %s", result.returncode, result.stderr.strip())
            return
        node.get_param("output").value = node.get_param("output_image").value
        logger.info("Render wrote %s", node.get_param("output_image").value)
    finally:
        if not keep:
            try:
                os.remove(scene_path)
            except OSError:
                pass
        else:
            logger.info("Render: kept scene file %s", scene_path)


def _render_clicked(owner, notice, sender):
    _render(owner.get_chunk())


def main():
    chunk_api = chunk.Chunk("Render")

    chunk_api.add_params([
        chunk.LineEdit("settings_path", "Render Settings", flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.LineEdit("render_command", "Render Command", default_value="usdrecord"),
        chunk.LineEdit("renderer", "Renderer", default_value="GL"),
        chunk.LineEdit("output_image", "Output Image", default_value="render.####.png"),
        chunk.LineEdit("frames", "Frames"),
        chunk.LineEdit(
            "camera", "Camera",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        ),
        chunk.Slider("image_width", "Image Width", default_value=0, is_int=True, min_value=0, max_value=16384),
        chunk.LineEdit("extra_args", "Extra Args"),
        chunk.Checkbox("render_on_commit", "Render on Commit", default_value=True),
        chunk.Checkbox("keep_scene_file", "Keep Scene File", default_value=False),
        chunk.LineEdit(
            "output", "Output",
            flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
        ),
    ])

    render_btn = chunk.Button("render_now", "Render Now")
    render_btn.add_callback(_render_clicked)
    chunk_api.add_param(render_btn)

    return chunk_api


def execute(node):
    """The cook authors nothing — rendering happens in commit()."""
    return


def commit(node):
    if not node.get_param("render_on_commit").value:
        return
    _render(node)


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Render chunk — the ROP. Runs a renderer over the composed stage, using the
        UsdRender.Settings prim that a Render Settings chunk authored.

        The render runs in the reserved commit() hook rather than in execute().
        execute is the cook: cheap, idempotent, and re-run constantly while the
        DAG settles. A render must happen once, after the values feeding it have
        stopped moving. Because the CLI calls commit_all() when a solve finishes,
        `usdchunks run network.usda` renders with no extra flag — and parameter
        overrides on that command line reach the render the same as any other
        param.

        Parameters:
          - Render Settings: path of the RenderSettings prim to render. Wire a
            Render Settings chunk's Reference output in here. May be left empty
            when that chunk has set the stage's renderSettingsPrimPath metadatum,
            which it does by default.
          - Render Command: the executable to run, "usdrecord" by default. The
            usd-core wheels on PyPI do not ship usdrecord, so point this at a full
            USD build's usdrecord, at husk, or at another renderer entry point.
          - Renderer: the Hydra render delegate, passed as --renderer. "GL" is
            Storm. Other values depend on what is installed — Embree, Prman,
            Arnold, Cycles.
          - Output Image: the file the renderer writes. Frame-numbering patterns
            (render.####.png) are passed through for the renderer to expand.
          - Frames: frame range, passed as --frames. Empty renders the default
            frame.
          - Camera: camera prim to render from, overriding the settings prim.
          - Image Width: overrides the settings resolution width. 0 leaves it to
            the settings prim.
          - Extra Args: additional flags appended to the command, split on
            whitespace.
          - Render on Commit: whether `usdchunks run` renders. On by default. Turn
            it off for a network where rendering should only ever be explicit.
          - Keep Scene File: keep the flattened .usda handed to the renderer
            instead of deleting it. Useful when a render fails and the question is
            what the renderer was actually given.
          - Render Now: render immediately, ignoring Render on Commit.
          - Output (output): the image path written by the last successful render.

        The stage is flattened with UsdUtils.FlattenLayerStack, which collapses the
        local layers but leaves references to external assets as references — so a
        network referencing a large asset stages a small file rather than inlining
        it. That file is written next to the root layer so relative asset paths
        still resolve, and removed afterwards unless Keep Scene File is set.
        """,
    )
