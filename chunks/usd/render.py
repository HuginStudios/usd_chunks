"""Render chunk -- the ROP. Renders the live stage in process via Hydra, using the
UsdRender.Settings prim a Render Settings chunk authored.

Rendering happens in commit(), not execute(): execute is the cook, re-run constantly
while the DAG settles, whereas a render must happen once, after the numbers have
stopped moving. That also means `usdchunks run network.usda` renders without any extra
flag, since the CLI calls commit_all() once the solve is done.

In process rather than shelling out to usdrecord: the stage is rendered as composed, so
there is no flattened temp file to stage, no relative asset paths to break, and no
dependency on a script being found and launchable on PATH.

Requires: pxr (OpenUSD) with UsdAppUtils and UsdImagingGL -- a full USD build. The
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

# Neither ships in the usd-core wheels. Each function that needs one checks for None and
# falls back, so a headless install still loads this chunk.
try:
    from pxr import UsdAppUtils
    from pxr.UsdAppUtils import rendererArgs
except ImportError:
    UsdAppUtils = rendererArgs = None
try:
    from pxr import UsdImagingGL
except ImportError:
    UsdImagingGL = None

from usd_chunks import chunk, chunk_utils


# Parented under "usd_chunks" rather than left at the bare module name. A chunk module
# is imported via spec_from_file_location under its own file name, so getLogger(__name__)
# lands outside the package tree -- where it inherits neither the handler nor the INFO
# level set up in usd_chunks/__init__.py, and only WARNING and above escape, through
# logging's lastResort handler. That silently hid every diagnostic this module logs at
# INFO, including which camera the render chose and why, which is the one line that
# explains a frame that came out looking nothing like the viewport. Also picks up
# USD_CHUNK_LOG_LEVEL, so DEBUG here is reachable the same way it is everywhere else.
logger = logging.getLogger(f"usd_chunks.{__name__}")

_FALLBACK_RENDERERS = ["GL", "Embree", "Prman", "Arnold", "VRay", "Cycles"]

_MODES = ["Hydra", "Command"]

_DIRECTLY_EXECUTABLE = (".exe", ".bat", ".cmd", ".com")

_COMPLEXITIES = {"low": 1.0, "medium": 1.1, "high": 1.2, "very high": 1.3}

_COLOR_CORRECTION_MODES = ["disabled", "sRGB", "openColorIO"]

_FRAME_PLACEHOLDER = re.compile(r"#+")

#: Formats written as 8-bit, where the renderer's output has to be display-referred to
#: look right -- so losing colour correction on the way out is visible rather than
#: something comp puts back.
_DISPLAY_REFERRED = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".gif"}


def _available_renderers():
    """Hydra render delegates this USD build can actually load.

    UsdAppUtils.rendererArgs is what usdrecord builds its own --renderer choices from,
    so its values are the canonical ones. UsdImagingGL is the same information one
    level down, used when UsdAppUtils is absent. Neither ships in the usd-core wheels,
    so a headless install falls back to a static list rather than an empty menu.
    """
    if rendererArgs is not None:
        try:
            if names := list(rendererArgs.GetAllPluginArguments()):
                return names
        except Exception:
            pass

    if UsdImagingGL is not None:
        try:
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
    if rendererArgs is not None:
        try:
            return rendererArgs.GetPluginIdFromArgument(renderer_name)
        except Exception:
            pass

    if UsdImagingGL is not None:
        try:
            for plugin in UsdImagingGL.Engine.GetRendererPlugins():
                if UsdImagingGL.Engine.GetRendererDisplayName(plugin) == renderer_name:
                    return plugin
        except Exception:
            pass

    return None


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


def _ensure_parent_dir(path):
    """Create the directory an image is about to be written into.

    Renderers do not create it. V-Ray reports the product it could not write and
    OpenEXR fails with "Unable to open file for write", both a directory late rather
    than a render that went wrong.
    """
    parent = os.path.dirname(path)
    if not parent or os.path.isdir(parent):
        return
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        logger.warning("Render: could not create %s (%s)", parent, exc)


def _settings_product_names(stage, settings_path):
    """The file each RenderProduct on this settings prim names, in order.

    Empty when nothing is wired, which is the signal that the render settings cannot
    decide the outputs on their own and an Output Image is still required. A product
    with no productName authored -- a Render Product chunk whose Output File is blank
    -- contributes no name, because there is nothing to write it to.

    Forwarded targets rather than direct ones, which is what UsdRender itself and
    FrameRecorder both read: a products relationship may point at another relationship
    rather than at the product prims, and taking the direct targets then finds prims
    that are not products and misses the ones that are.
    """
    if not settings_path:
        return []

    prim = stage.GetPrimAtPath(Sdf.Path(settings_path))
    if not prim or not prim.IsValid():
        return []

    names = []
    for target in UsdRender.Settings(prim).GetProductsRel().GetForwardedTargets():
        product_prim = stage.GetPrimAtPath(target)
        if not product_prim or not product_prim.IsValid():
            continue
        name = UsdRender.Product(product_prim).GetProductNameAttr().Get()
        if name:
            names.append(str(name))
    return names


def _settings_var_count(stage, settings_path):
    """How many render vars the settings prim's products ask for, in total.

    Only used to decide whether losing them is worth saying anything about, so
    duplicates across products are not worth resolving.
    """
    if not settings_path:
        return 0

    prim = stage.GetPrimAtPath(Sdf.Path(settings_path))
    if not prim or not prim.IsValid():
        return 0

    count = 0
    for target in UsdRender.Settings(prim).GetProductsRel().GetForwardedTargets():
        product_prim = stage.GetPrimAtPath(target)
        if not product_prim or not product_prim.IsValid():
            continue
        count += len(UsdRender.Product(product_prim).GetOrderedVarsRel().GetForwardedTargets())
    return count


def _hydra_reads_render_products():
    """Whether this USD build lets the render products decide a render's outputs.

    Stock UsdAppUtils.FrameRecorder rejects an empty output path outright -- "Invalid
    empty output image path" -- and binds no AOVs from a settings prim's render vars,
    so both the products route and the AOVs need a build carrying the render-settings
    work in UsdAppUtils.

    SetExtraAovs is the marker for it. It arrives with the same change, and unlike the
    two behaviours themselves it can be tested for without doing a render. Checked on
    the class rather than an instance so the answer is available before a recorder --
    and its render delegate -- has been built.
    """
    if UsdAppUtils is None:
        return False
    return hasattr(UsdAppUtils.FrameRecorder, "SetExtraAovs")


def _prepare_product_dirs(stage, settings_path):
    """Create the directories the settings' RenderProducts will write into.

    The delegate writes those files itself, so their paths never pass through here --
    but they fail the same way, and the renderer will not make the folder either. One
    interpretation is enough now that output paths resolve against the working directory
    too: it is the same base USD gives a relative productName, so the directory prepared
    here is the directory the delegate writes into.
    """
    for name in _settings_product_names(stage, settings_path):
        _ensure_parent_dir(chunk_utils.cwd_relative(name))


def _resolve_settings_path(node, stage):
    """The RenderSettings prim to render: explicit, via a render pass, or the stage's.

    The same order usdrecord resolves it in. Falling back to the stage metadatum
    matters because that is what a Render Settings chunk publishes by default, so a
    network that wired nothing still renders the settings it authored.
    """
    path = (node.get_param("settings_path").value or "").strip()
    if path:
        return path

    if pass_path := _resolve_pass_path(node, stage):
        render_pass = UsdRender.Pass(stage.GetPrimAtPath(Sdf.Path(pass_path)))
        targets = render_pass.GetRenderSourceRel().GetTargets() if render_pass else []
        if targets:
            if len(targets) > 1:
                logger.warning(
                    "Render: render pass %s has several sources; using %s.", pass_path, targets[0]
                )
            return str(targets[0])

    stage_settings = UsdRender.Settings.GetStageRenderSettings(stage)
    return str(stage_settings.GetPath()) if stage_settings else ""


def _resolve_pass_path(node, stage):
    """The RenderPass prim to drive the render with, if one is named and real."""
    path = (node.get_param("pass_path").value or "").strip()
    if not path:
        return ""

    prim = stage.GetPrimAtPath(Sdf.Path(path))
    if not prim or not prim.IsValid():
        logger.warning("Render: no render pass at %s.", path)
        return ""
    return path


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
    settings name, else the first camera on the stage.

    Which of the three it was is said out loud, because the last one is nobody's
    decision -- it is whichever camera Traverse happens to reach first, and there is no
    reason for that to be the one the viewport is showing. A frame that comes out
    looking nothing like the viewport is then something the log accounts for rather
    than a mystery to go hunting for.
    """
    if camera := _camera_at(stage, node.get_param("camera").value):
        source = "set on this chunk"
    elif camera := _settings_camera(stage, settings_path):
        source = "named by Render Settings"
    else:
        camera = next(
            (UsdGeom.Camera(prim) for prim in stage.Traverse() if prim.IsA(UsdGeom.Camera)),
            None,
        )
        if camera is None:
            return None
        source = "first camera found on the stage, nothing chose it"

    logger.info("Render: rendering from <%s>, %s.", camera.GetPath(), source)
    return camera


def _why_refused(renderer, use_gpu):
    """The likely reason a delegate would not load, phrased as something to do about it.

    Turning the GPU off does not just remove a feature: a delegate that renders on the
    GPU stops being a supported plugin at all, and the engine then reports having no
    plugins rather than naming the one it rejected.
    """
    if not use_gpu:
        return (
            f"With Use GPU off, a GPU-only delegate is not available -- Storm is one. "
            f"Turn Use GPU back on to render with {renderer}, or choose a delegate that "
            f"renders on the CPU."
        )
    return (
        "Check that the delegate is installed and that this host has an OpenGL context; "
        "with Use GPU on, one is required even for a delegate that does not use it."
    )


def _make_recorder(node, settings_path="", pass_path=""):
    """A configured FrameRecorder, or None when this build cannot render."""
    if UsdAppUtils is None:
        logger.error(
            "Render: UsdAppUtils is not available. Rendering needs a full USD build — "
            "the usd-core wheels ship neither UsdAppUtils nor UsdImagingGL."
        )
        return None

    renderer = node.get_param("renderer").value
    plugin = _plugin_id(renderer)
    if renderer and not plugin:
        logger.warning("Render: no Hydra plugin named %r; using the default delegate.", renderer)

    use_gpu = bool(node.get_param("use_gpu").value)
    if use_gpu and not _gl_context_possible():
        logger.warning(
            "Render: no OpenGL context is available here, so Use GPU is being ignored "
            "and this render runs on the CPU. Delegates that only render on the GPU "
            "(Storm) are unavailable in this process."
        )
        use_gpu = False

    # The plugin and the GPU switch go through the constructor when this USD offers it,
    # because whether the engine is GPU-backed is decided as it is built and cannot be
    # turned off afterwards. With the GPU enabled the recorder needs a real GL context
    # even for a delegate that does not render on the GPU, which is what makes a
    # headless run die where the same render works inside usdview. usdrecord solves it
    # the same way, with --disableGpu.
    recorder = None
    if plugin:
        try:
            recorder = UsdAppUtils.FrameRecorder(plugin, use_gpu)
        except TypeError:
            recorder = None  # older USD: no such constructor
        except Exception as exc:  # noqa: BLE001 - the delegate refused these conditions
            logger.error("Render: %s cannot render here. %s", renderer, _why_refused(renderer, use_gpu))
            logger.debug("Render: SetRendererPlugin(%s) failed: %s", plugin, exc)
            return None

    if recorder is None:
        try:
            recorder = UsdAppUtils.FrameRecorder()
            if plugin:
                recorder.SetRendererPlugin(plugin)
        except Exception as exc:  # noqa: BLE001
            logger.error("Render: %s cannot render here. %s", renderer, _why_refused(renderer, use_gpu))
            logger.debug("Render: recorder setup failed: %s", exc)
            return None

        if not use_gpu:
            logger.warning(
                "Render: this USD build cannot disable the GPU, so a headless run may "
                "fail without an OpenGL context."
            )

    # Pass and settings first, before every other setter -- the order usdrecord uses.
    # These say what is being rendered; the rest adjust how, and a couple of them
    # deliberately override what the settings prim asked for.
    if pass_path and hasattr(recorder, "SetActiveRenderPassPrimPath"):
        recorder.SetActiveRenderPassPrimPath(Sdf.Path(pass_path))

    if settings_path and hasattr(recorder, "SetActiveRenderSettingsPrimPath"):
        recorder.SetActiveRenderSettingsPrimPath(Sdf.Path(settings_path))

    if width := int(node.get_param("image_width").value or 0):
        recorder.SetImageWidth(width)

    recorder.SetComplexity(_COMPLEXITIES.get(node.get_param("complexity").value, 1.0))

    # FrameRecorder lights the scene from the camera unless told not to (usdrecord's
    # --disableCameraLight): a headlight that flattens a lit scene's final render.
    if hasattr(recorder, "SetCameraLightEnabled") and (light := node.get_param("camera_light")) is not None:
        recorder.SetCameraLightEnabled(bool(light.value))

    if purposes := _purposes(node, settings_path):
        if hasattr(recorder, "SetIncludedPurposes"):
            recorder.SetIncludedPurposes(purposes)

    mode = node.get_param("color_correction").value
    if mode and hasattr(recorder, "SetColorCorrectionMode"):
        recorder.SetColorCorrectionMode(mode)

    # Colour correction is a GPU task inside the engine, so with the GPU off the call
    # above is accepted and does nothing. Harmless writing EXR, which is linear and
    # colour-managed downstream anyway; visible writing PNG, which is expected to be
    # display-referred and will come out dark.
    if not use_gpu and mode and mode != "disabled":
        extension = os.path.splitext(node.get_param("output_image").value or "")[1].lower()
        if extension in _DISPLAY_REFERRED:
            logger.warning(
                "Render: Color Correction %r cannot be applied with Use GPU off -- it "
                "runs on the GPU -- so %s will be written uncorrected. Render to .exr "
                "and convert downstream, or turn Use GPU back on where there is a "
                "display to render against.",
                mode,
                extension,
            )

    return recorder


def _purposes(node, settings_path):
    """Purposes to render, from the settings prim the render is driven by.

    The recorder takes its own list and does not read the settings prim for it, so a
    network that says includedPurposes gets ignored unless it is passed through here.
    """
    stage = node.get_stage()
    if not stage or not settings_path:
        return []

    prim = stage.GetPrimAtPath(Sdf.Path(settings_path))
    if not prim or not prim.IsValid():
        return []

    purposes = UsdRender.Settings(prim).GetIncludedPurposesAttr().Get()
    return [str(p) for p in (purposes or [])]


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
    """Render by shelling out, for renderers with their own entry point -- husk, a
    V-Ray standalone -- and when delegate crashes need to stay out of this process.
    """
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
    return UsdAppUtils is not None


def _gl_context_possible():
    """Whether this process could have an OpenGL context for HgiGL to attach to.

    With Use GPU on, HgiGL requires a current GL context even for a delegate that
    renders entirely on the CPU. When there is none it does not raise: it logs
    "HgiGL minimum OpenGL requirements not met" and the process then dies inside the
    delegate with an access violation, which reaches the caller as a bare exit code
    and no Python traceback. That is what `usdchunks run` hits, where the same network
    renders fine inside usdview.

    Hgi has no Python binding in this build, so the context cannot be probed directly.
    Qt is the only thing that creates one in any of these hosts, so the question this
    answers is the conservative half of it -- whether a context could exist at all,
    not whether one is current. A GUI host always answers yes and keeps the behaviour
    it has today; only a process with no Qt application, which is to say the CLI, can
    answer no. Guessing wrong in that direction costs a CPU render rather than a crash.
    """
    # Looked up rather than imported: a QApplication can only exist if the host has already
    # imported Qt, and the CLI has no reason to load it just to learn it is absent.
    QtWidgets = sys.modules.get("PySide6.QtWidgets")
    if QtWidgets is None:
        return False
    return QtWidgets.QApplication.instance() is not None


def _render(node):
    # Empty is meaningful in Hydra mode: it hands the choice of outputs to the render
    # products, which is the only way FrameRecorder writes more than one AOV. Naming an
    # image here is what turns that off, so the check belongs where the path is used
    # rather than up front.
    output_image = node.get_param("output_image").value or ""
    # Same substitution the Render Product does, for the same reason -- see
    # chunk_utils.resolve_output_path. An output path is the one string in a shot graph that
    # has to differ per shot.
    output_image = chunk_utils.resolve_output_path(output_image, node.get_stage(), logger)

    mode = node.get_param("mode").value
    if mode == "Hydra" and not _hydra_available():
        logger.warning(
            "Render: UsdAppUtils is unavailable, so in-process rendering is not "
            "possible in this build — falling back to Render Command."
        )
        mode = "Command"

    if mode == "Command":
        if not output_image:
            logger.error("Render: no output image path set.")
            return
        _render_command(node, output_image)
    else:
        _render_hydra(node, output_image)


def _render_hydra(node, output_image):
    stage = node.get_stage()
    if not stage:
        return

    try:
        frames = chunk_utils.parse_frames(node.get_param("frames").value)
    except ValueError as exc:
        logger.error("Render: %s", exc)
        return

    pass_path = _resolve_pass_path(node, stage)
    settings_path = _resolve_settings_path(node, stage)

    camera = _resolve_camera(node, stage, settings_path)
    if camera is None:
        logger.error("Render: no camera to render from — set Camera, or point Render Settings at one.")
        return

    # An empty Output Image means "the render products decide". FrameRecorder only
    # takes that route when the path it is handed is empty -- given one, it writes that
    # single image and never looks at the products, which is why a network with four
    # RenderVars wired up still produced one beauty frame. Refuse the combination that
    # cannot work rather than render into nothing.
    products = _settings_product_names(stage, settings_path)
    if not output_image and not products:
        logger.error(
            "Render: no Output Image, and no Render Settings prim naming render "
            "products to write instead. Set one or the other."
        )
        return

    # Refused up front rather than left to fail inside Record(), which reports only
    # "Invalid empty output image path" -- true, but no help at all in working out that
    # the build is what is missing rather than the network.
    if not output_image and not _hydra_reads_render_products():
        logger.error(
            "Render: this USD build cannot let the render products decide the outputs, "
            "so Output Image is required here. Set one, or use Command mode to render "
            "through an external tool that reads the products itself."
        )
        return

    # AOVs go the same way and take no error with them: without the render vars bound,
    # a network describing twenty channels renders one beauty frame and says nothing.
    # Worth a word, since the result looks like a renderer that cannot do AOVs.
    if (var_count := _settings_var_count(stage, settings_path)) and not _hydra_reads_render_products():
        logger.warning(
            "Render: this USD build does not bind a settings prim's render vars, so "
            "the %d AOVs on <%s> will not be in the output — only the beauty. Command "
            "mode, or a tool that reads the products, renders them.",
            var_count,
            settings_path,
        )

    recorder = _make_recorder(node, settings_path, pass_path)
    if recorder is None:
        return

    _prepare_product_dirs(stage, settings_path)

    time_codes = [Usd.TimeCode(f) for f in frames] or [Usd.TimeCode.EarliestTime()]
    written = []
    try:
        for time_code in time_codes:
            # Substituted even with no frame spec: a path written as beauty.####.exr
            # should never reach disk with the hashes still in it, and the single
            # render is frame 0 rather than EarliestTime's -inf.
            frame_number = time_code.GetValue() if frames else 0
            path = ""
            if output_image:
                path = chunk_utils.cwd_relative(format_output_path(output_image, frame_number))
                _ensure_parent_dir(path)
            if not recorder.Record(stage, camera, time_code, path):
                logger.error("Render: failed writing %s", path or "the render products")
                return

            # With no path of our own, what landed on disk is whatever the products
            # named -- report those, so Output still says where the images went.
            paths = [path] if path else [
                chunk_utils.cwd_relative(format_output_path(name, frame_number)) for name in products
            ]
            written.extend(paths)
            for wrote in paths:
                logger.info("Render wrote %s", wrote)
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
        chunk.LineEdit("pass_path", "Render Pass", flags=chunk.ChunkFlags.ParamFlags.LABELED),
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
        chunk.Checkbox("use_gpu", "Use GPU", default_value=True),
        chunk.Checkbox(
            "camera_light", "Camera Light", default_value=True,
            tooltip="Light the scene from the camera too, as usdrecord does by default. Off for a lit scene's final render.",
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
    """The cook authors nothing -- rendering happens in commit()."""
    return


def commit(node):
    if not node.get_param("render_on_commit").value:
        return
    _render(node)


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_RENDER,
        icon="film",
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
          - Render Pass: path of a RenderPass prim to drive the render with. Wire a
            Render Pass chunk's Reference output in here. Optional, and worth
            trying when a delegate ignores the settings prim: a renderer may
            implement passes without implementing render settings, and the pass
            also supplies the settings through its renderSource.
          - Render Settings: path of the RenderSettings prim to render. Wire a
            Render Settings chunk's Reference output in here. Left empty, it is
            taken from Render Pass's renderSource, then from the stage's own
            renderSettingsPrimPath, which a Render Settings chunk publishes by
            default — the same order usdrecord resolves it in.

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

            Leave it empty to let the Render Settings prim's products decide the
            outputs instead. That is the only route that writes more than one AOV:
            naming an image here makes FrameRecorder write that single file and
            never consult the products, so a network with several Render Vars
            wired up still produces one beauty frame. With it empty, each product
            is written to the file its productName gives, carrying the AOVs its
            Ordered Vars ask for — subject to the renderer actually producing
            them, which not every delegate does.

            A relative path is resolved against the working directory, the same
            base USD gives a relative productName, and the directory is created if
            it does not exist. The same is done for the directories the settings'
            RenderProducts write into, since the delegate writes those itself and
            no renderer creates the folder for you.
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
          - Color Correction: disabled, sRGB, or openColorIO. Applied by a GPU task
            inside the engine, so it does nothing with Use GPU off, and the chunk
            says so when that would be visible. EXR is unaffected in practice —
            it is linear and colour-managed downstream — while a PNG written
            headless comes out uncorrected.
          - Use GPU: on by default, which is right inside a DCC. Turn it off to
            render headless: with it on the recorder builds a GPU-backed engine
            and needs a real OpenGL context even for a delegate that does not
            render on the GPU, so a farm or a terminal run dies where the same
            render succeeds in usdview. usdrecord has the same switch, spelled
            --disableGpu.
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
