"""Render chunk — the ROP. Renders the live stage in process via Hydra, using the
UsdRender.Settings prim a Render Settings chunk authored.

Rendering happens in commit(), not execute(): execute is the cook, re-run constantly
while the DAG settles, whereas a render must happen once, after the numbers have
stopped moving. That also means `usdchunks run network.usda` renders without any extra
flag, since the CLI calls commit_all() once the solve is done.

In process rather than shelling out to usdrecord: the stage is rendered as composed, so
there is no flattened temp file to stage, no relative asset paths to break, and no
dependency on a script being found and launchable on PATH.

Requires: pxr (OpenUSD) with UsdAppUtils and UsdImagingGL — a full USD build. The
usd-core wheels ship neither, so rendering is unavailable there.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

from pxr import Sdf, Usd, UsdGeom, UsdRender, UsdUtils

from usd_chunks import chunk


logger = logging.getLogger(__name__)

_FALLBACK_RENDERERS = ["GL", "Embree", "Prman", "Arnold", "VRay", "Cycles"]

_MODES = ["Hydra", "Command"]

_DIRECTLY_EXECUTABLE = (".exe", ".bat", ".cmd", ".com")

_COMPLEXITIES = {"low": 1.0, "medium": 1.1, "high": 1.2, "very high": 1.3}

_COLOR_CORRECTION_MODES = ["disabled", "sRGB", "openColorIO"]

_FRAME_PLACEHOLDER = re.compile(r"#+")


def _available_renderers():
    """Hydra render delegates this USD build can actually load.

    UsdAppUtils.rendererArgs is what usdrecord builds its own --renderer choices from,
    so its values are the canonical ones. UsdImagingGL is the same information one
    level down, used when UsdAppUtils is absent. Neither ships in the usd-core wheels,
    so a headless install falls back to a static list rather than an empty menu.
    """
    try:
        from pxr.UsdAppUtils import rendererArgs

        if names := list(rendererArgs.GetAllPluginArguments()):
            return names
    except Exception:
        pass

    try:
        from pxr import UsdImagingGL

        names = [
            UsdImagingGL.Engine.GetRendererDisplayName(plugin)
            for plugin in UsdImagingGL.Engine.GetRendererPlugins()
        ]
        if names := [n for n in names if n]:
            return names
    except Exception:
        pass

    return list(_FALLBACK_RENDERERS)


def _plugin_id(renderer_name):
    """Hydra plugin id for a menu entry.

    The menu shows argument names ("GL"), while FrameRecorder wants plugin ids
    ("HdStormRendererPlugin"), so the two have to be mapped rather than passed through.
    """
    try:
        from pxr.UsdAppUtils import rendererArgs

        return rendererArgs.GetPluginIdFromArgument(renderer_name)
    except Exception:
        pass

    try:
        from pxr import UsdImagingGL

        for plugin in UsdImagingGL.Engine.GetRendererPlugins():
            if UsdImagingGL.Engine.GetRendererDisplayName(plugin) == renderer_name:
                return plugin
    except Exception:
        pass

    return None


def parse_frames(spec):
    """Frame numbers named by a usdrecord-style frame spec.

    Accepts comma-separated entries, each a single frame, a "start:end" range, or a
    "start:endxstride" strided range. An empty spec means a single render at the
    stage's default values rather than at a frame, and is reported as an empty list.
    """
    if not spec or not spec.strip():
        return []

    frames = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue

        stride = 1.0
        if "x" in entry:
            entry, _, stride_text = entry.partition("x")
            stride = float(stride_text)
        if stride <= 0:
            raise ValueError(f"frame stride must be positive: {spec!r}")

        if ":" not in entry:
            frames.append(float(entry))
            continue

        start_text, _, end_text = entry.partition(":")
        start, end = float(start_text), float(end_text)
        if end < start:
            raise ValueError(f"frame range ends before it starts: {entry!r}")

        current = start
        while current <= end + 1e-9:
            frames.append(current)
            current += stride

    return frames


def format_output_path(path, frame=None):
    """Substitute a frame number into an output path.

    Understands both conventions this audience uses: a {frame} field, and a run of
    hashes padded to its own width, so render.####.exr and render.{frame}.exr both
    work. Returns the path untouched when there is no frame to substitute.
    """
    if frame is None:
        return path

    number = int(round(frame))
    if "{frame}" in path:
        path = path.replace("{frame}", str(number))
    return _FRAME_PLACEHOLDER.sub(lambda m: str(number).zfill(len(m.group(0))), path)


def _camera_at(stage, path):
    if not path:
        return None
    prim = stage.GetPrimAtPath(Sdf.Path(path))
    if not prim or not prim.IsValid():
        return None
    camera = UsdGeom.Camera(prim)
    return camera or None


def _resolve_settings_path(node, stage):
    """The RenderSettings prim to render, as an explicit path or the stage's own.

    Falling back to the stage metadatum matters because that is what a Render Settings
    chunk publishes by default, so a network that wired nothing still renders the
    settings it authored rather than silently ignoring them.
    """
    path = (node.get_param("settings_path").value or "").strip()
    if path:
        return path

    stage_settings = UsdRender.Settings.GetStageRenderSettings(stage)
    return str(stage_settings.GetPath()) if stage_settings else ""


def _settings_camera(stage, settings_path):
    """The camera a RenderSettings prim points at, if it names one."""
    if not settings_path:
        return None

    prim = stage.GetPrimAtPath(Sdf.Path(settings_path))
    if not prim or not prim.IsValid():
        return None

    targets = UsdRender.Settings(prim).GetCameraRel().GetTargets()
    return _camera_at(stage, targets[0]) if targets else None


def _resolve_camera(node, stage, settings_path=""):
    """The camera to render from: the chunk's own override, else the one the render
    settings name, else the first camera on the stage."""
    if camera := _camera_at(stage, node.get_param("camera").value):
        return camera
    if camera := _settings_camera(stage, settings_path):
        return camera
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Camera):
            return UsdGeom.Camera(prim)
    return None


def _make_recorder(node, settings_path=""):
    """A configured FrameRecorder, or None when this build cannot render."""
    try:
        from pxr import UsdAppUtils
    except ImportError:
        logger.error(
            "Render: UsdAppUtils is not available. Rendering needs a full USD build — "
            "the usd-core wheels ship neither UsdAppUtils nor UsdImagingGL."
        )
        return None

    recorder = UsdAppUtils.FrameRecorder()

    renderer = node.get_param("renderer").value
    if plugin := _plugin_id(renderer):
        recorder.SetRendererPlugin(plugin)
    elif renderer:
        logger.warning("Render: no Hydra plugin named %r; using the default delegate.", renderer)

    if width := int(node.get_param("image_width").value or 0):
        recorder.SetImageWidth(width)

    recorder.SetComplexity(_COMPLEXITIES.get(node.get_param("complexity").value, 1.0))

    mode = node.get_param("color_correction").value
    if mode and hasattr(recorder, "SetColorCorrectionMode"):
        recorder.SetColorCorrectionMode(mode)

    # Hand the delegate the settings prim rather than only reading a camera off it.
    # A delegate implementing HdRenderSettings then walks its RenderProducts and
    # RenderVars and writes those files itself -- which is what makes the AOVs a
    # network authors actually come out, instead of a single beauty frame. Guarded by
    # hasattr because older USD builds have no such hook.
    if settings_path and hasattr(recorder, "SetActiveRenderSettingsPrimPath"):
        recorder.SetActiveRenderSettingsPrimPath(Sdf.Path(settings_path))

    return recorder


def _resolve_command(command):
    """Resolve command to the argv prefix that will actually launch it, or None.

    USD ships usdrecord (and usdcat, usdzip, ...) as extension-less Python scripts.
    shutil.which finds them, but Windows cannot CreateProcess a file it has no
    association for, so invoking the resolved path directly fails with WinError 2
    even though the tool is plainly installed. Anything not directly executable is
    therefore run through this interpreter instead.
    """
    resolved = shutil.which(command)
    if resolved is None:
        return None

    ext = os.path.splitext(resolved)[1].lower()
    if ext == ".py" or (os.name == "nt" and ext not in _DIRECTLY_EXECUTABLE):
        return [sys.executable, resolved]
    return [resolved]


def _staging_dir(stage):
    """Where to write the flattened stage handed to an external renderer.

    Next to the root layer when there is one, so relative asset paths inside the
    network still resolve; the system temp dir only when the stage has never been
    saved, where relative paths are already unresolvable anyway.
    """
    root_layer = stage.GetRootLayer()
    if root_layer and root_layer.realPath:
        return os.path.dirname(root_layer.realPath)
    return tempfile.gettempdir()


def _export_for_render(stage):
    """Flatten the layer stack to a file an external renderer can open.

    FlattenLayerStack rather than Stage.Export: it collapses the local layers but
    leaves references to external assets as references, so a network referencing a
    large asset writes a small file instead of inlining the whole thing.
    """
    flat = UsdUtils.FlattenLayerStack(stage)
    fd, path = tempfile.mkstemp(suffix=".usda", prefix="usdchunks_render_", dir=_staging_dir(stage))
    os.close(fd)
    flat.Export(path)
    return path


def _build_argv(node, prefix, scene_path, output_image):
    argv = list(prefix)

    if renderer := node.get_param("renderer").value:
        argv += ["--renderer", renderer]
    if frames := node.get_param("frames").value:
        argv += ["--frames", frames]
    if camera := node.get_param("camera").value:
        argv += ["--camera", camera]
    if width := int(node.get_param("image_width").value or 0):
        argv += ["--imageWidth", str(width)]
    if complexity := node.get_param("complexity").value:
        argv += ["--complexity", complexity.replace(" ", "")]
    if extra := node.get_param("extra_args").value:
        argv += extra.split()

    argv.append(scene_path)
    argv.append(output_image)
    return argv


def _render_command(node, output_image):
    """Render by shelling out, for renderers with their own entry point — husk, a
    V-Ray standalone — and when delegate crashes need to stay out of this process."""
    stage = node.get_stage()
    command = node.get_param("render_command").value or "usdrecord"
    prefix = _resolve_command(command)
    if prefix is None:
        logger.error(
            "Render: %r not found on PATH. Point Render Command at a full USD build's "
            "usdrecord, at husk, or at another renderer entry point.",
            command,
        )
        return

    scene_path = _export_for_render(stage)
    argv = _build_argv(node, prefix, scene_path, output_image)
    keep = node.get_param("keep_scene_file").value

    logger.info("Render: %s", " ".join(argv))
    try:
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("Render failed (exit %s): %s", result.returncode, result.stderr.strip())
            return
        node.get_param("output").value = output_image
        logger.info("Render wrote %s", output_image)
    except OSError as exc:
        logger.error("Render: could not run %s (%s)", " ".join(argv), exc)
    finally:
        if keep:
            logger.info("Render: kept scene file %s", scene_path)
        else:
            try:
                os.remove(scene_path)
            except OSError:
                pass


def _hydra_available():
    try:
        from pxr import UsdAppUtils  # noqa: F401
    except ImportError:
        return False
    return True


def _render(node):
    output_image = node.get_param("output_image").value
    if not output_image:
        logger.error("Render: no output image path set.")
        return

    mode = node.get_param("mode").value
    if mode == "Hydra" and not _hydra_available():
        logger.warning(
            "Render: UsdAppUtils is unavailable, so in-process rendering is not "
            "possible in this build — falling back to Render Command."
        )
        mode = "Command"

    if mode == "Command":
        _render_command(node, output_image)
    else:
        _render_hydra(node, output_image)


def _render_hydra(node, output_image):
    stage = node.get_stage()
    if not stage:
        return

    try:
        frames = parse_frames(node.get_param("frames").value)
    except ValueError as exc:
        logger.error("Render: %s", exc)
        return

    settings_path = _resolve_settings_path(node, stage)

    camera = _resolve_camera(node, stage, settings_path)
    if camera is None:
        logger.error("Render: no camera to render from — set Camera, or point Render Settings at one.")
        return

    recorder = _make_recorder(node, settings_path)
    if recorder is None:
        return

    time_codes = [Usd.TimeCode(f) for f in frames] or [Usd.TimeCode.EarliestTime()]
    written = []
    try:
        for time_code in time_codes:
            path = format_output_path(output_image, time_code.GetValue() if frames else None)
            if not recorder.Record(stage, camera, time_code, path):
                logger.error("Render: failed writing %s", path)
                return
            written.append(path)
            logger.info("Render wrote %s", path)
    except Exception as exc:
        # A render delegate failing must report something actionable rather than
        # propagate: the Render button dispatches through the notice system, where an
        # exception kills the callback instead of telling the user anything.
        logger.error("Render: %s", exc)
        return
    finally:
        if hasattr(recorder, "DeleteGLResources"):
            recorder.DeleteGLResources()

    node.get_param("output").value = written[-1]


def _render_clicked(owner, notice, sender):
    _render(owner.get_chunk())


def main():
    chunk_api = chunk.Chunk("Render")

    renderers = _available_renderers()
    default_renderer = "GL" if "GL" in renderers else renderers[0]

    chunk_api.add_params([
        chunk.LineEdit("settings_path", "Render Settings", flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Menu("mode", "Mode", menu_items=_MODES, default_value="Hydra"),
        chunk.Menu("renderer", "Renderer", menu_items=renderers, default_value=default_renderer),
        chunk.LineEdit("output_image", "Output Image", default_value="render.####.png"),
        chunk.LineEdit("frames", "Frames"),
        chunk.LineEdit(
            "camera", "Camera",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        ),
        chunk.Slider("image_width", "Image Width", default_value=960, is_int=True, min_value=0, max_value=16384),
        chunk.Menu(
            "complexity", "Complexity",
            menu_items=list(_COMPLEXITIES), default_value="high",
        ),
        chunk.Menu(
            "color_correction", "Color Correction",
            menu_items=_COLOR_CORRECTION_MODES, default_value="sRGB",
        ),
        chunk.Checkbox("render_on_commit", "Render on Commit", default_value=True),
        chunk.LineEdit("render_command", "Render Command", default_value="usdrecord"),
        chunk.LineEdit("extra_args", "Extra Args"),
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
        Render chunk — the ROP. Renders the live stage through Hydra, using the
        UsdRender.Settings prim a Render Settings chunk authored.

        The render runs in the reserved commit() hook rather than in execute().
        execute is the cook: cheap, idempotent, and re-run constantly while the
        DAG settles. A render must happen once, after the values feeding it have
        stopped moving. Because the CLI calls commit_all() when a solve finishes,
        `usdchunks run network.usda` renders with no extra flag — and parameter
        overrides on that command line reach the render like any other param.

        Rendering is in process, via UsdAppUtils.FrameRecorder, rather than by
        shelling out to usdrecord. The stage is rendered as composed, so there is
        no flattened temp file to stage, no relative asset path to break, and no
        dependency on finding and launching a script on PATH.

        Parameters:
          - Render Settings: path of the RenderSettings prim to render. Wire a
            Render Settings chunk's Reference output in here. Left empty, the
            stage's own renderSettingsPrimPath is used, which a Render Settings
            chunk publishes by default.

            In Hydra mode this is handed to the delegate, not just read for a
            camera. A delegate implementing HdRenderSettings then walks the
            RenderProducts and RenderVars it names and writes those files itself,
            which is what makes a network's AOVs come out rather than a single
            beauty frame. What a delegate does with it varies: Storm largely
            ignores products, while hdPrman and hdVRay act on them.
          - Mode: Hydra renders in process through UsdAppUtils.FrameRecorder and is
            the default. Command shells out instead, for renderers with their own
            entry point (husk, a V-Ray standalone) and for keeping a delegate that
            might crash out of this process. Hydra falls back to Command
            automatically when UsdAppUtils is unavailable.
          - Renderer: the Hydra render delegate. Populated from the delegates this
            USD build can actually load, via the same UsdAppUtils.rendererArgs
            list usdrecord builds its own --renderer choices from, falling back to
            UsdImagingGL and then to a static list. "GL" is Storm; what else
            appears depends on what is installed — Embree, Prman, Arnold, VRay,
            Cycles. The menu reflects *this* machine, so a network authored
            elsewhere against a delegate that is not installed here will not offer
            it.
          - Output Image: the file to write. Frame placeholders are substituted
            when Frames is set — both render.####.exr and render.{frame}.exr work,
            with hashes padded to their own width.
          - Frames: which frames to render, usdrecord-style — comma-separated
            entries, each a single frame, a start:end range, or a strided
            start:endxstride range. Empty renders one image at the stage's default
            values.
          - Camera: camera prim to render from. Falls back to the camera named by
            the Render Settings prim, then to the first camera on the stage.
          - Image Width: output width in pixels. Height follows the camera's
            aspect ratio.
          - Complexity: refinement level, matching usdview's — low, medium, high,
            very high.
          - Color Correction: disabled, sRGB, or openColorIO.
          - Render on Commit: whether `usdchunks run` renders. On by default. Turn
            it off for a network where rendering should only ever be explicit.
          - Render Command: Command mode only — the executable to run, usdrecord by
            default. USD ships usdrecord as an extension-less Python script, which
            Windows cannot launch directly, so anything not directly executable is
            run through this interpreter.
          - Extra Args: Command mode only — additional flags appended to the
            command, split on whitespace.
          - Keep Scene File: Command mode only — keep the flattened .usda handed to
            the renderer instead of deleting it, for when a render fails and the
            question is what the renderer was actually given.
          - Render Now: render immediately, ignoring Render on Commit.
          - Output (output): the last image path written.

        Command mode stages the stage with UsdUtils.FlattenLayerStack, which
        collapses the local layers but leaves references to external assets as
        references, written beside the root layer so relative asset paths still
        resolve. Hydra mode needs none of that, since it renders the composed stage
        directly.

        Rendering needs a full USD build: the usd-core wheels ship neither
        UsdAppUtils nor UsdImagingGL, and the chunk reports that rather than
        failing obscurely. Storm additionally needs a usable GL context, so a
        headless farm wants a delegate that does not — Embree and the commercial
        delegates are fine.
        """,
    )
