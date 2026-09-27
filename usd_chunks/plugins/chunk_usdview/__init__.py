"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
"""
from __future__ import annotations

import logging

from pxr import Tf, Usd, Gf, Sdf, UsdGeom
from pxr.Usdviewq.plugin import PluginContainer
from pxr.Usdviewq import usdviewApi as UsdviewModule
from pxr.Usdviewq.qt import QtCore

from usd_chunks import chunk_registry, chunk_core, shader_defs
from usd_chunks.plugins import chunk_qt, chunk_window_state, chunk_qt_layer_stack
from usd_chunks import chunk_stage


logger = logging.getLogger(__name__)


def _prim_clipping(prim):
    """`(near, far)` of a camera prim, or None when there is no such camera to read."""
    try:
        clip = UsdGeom.Camera(prim).GetClippingRangeAttr().Get()
    except Exception:  # noqa: BLE001 - a stand-in, a dead prim: nothing to put on
        return None
    if clip is None:
        return None
    return (float(clip[0]), float(clip[1]))


def _clip_overrides(free_cam):
    """The free camera's near/far overrides as `(near, far)`, None each where it has none."""
    return (getattr(free_cam, "overrideNear", None), getattr(free_cam, "overrideFar", None))


def _set_clip_overrides(free_cam, clip):
    """Put `(near, far)` on the free camera as its overrides; None clears them both.

    Straight onto the FreeCamera, like the fov: its override setters emit nothing, so this
    is never mistaken for a camera move.
    """
    near, far = clip if clip is not None else (None, None)
    if not hasattr(free_cam, "overrideNear"):
        return
    if _clip_overrides(free_cam) != (near, far):
        free_cam.overrideNear = near
        free_cam.overrideFar = far


class UsdviewScene(chunk_core.SceneSender):
    """This class is responsible for sending some UsdView related notices. In other DCC's, this class should not be used
    TODO: Make this abstract and move implementation to a UsdView related location
    """

    def __init__(self, usdviewAPI: UsdviewModule.UsdviewApi):
        self.usdviewAPI: UsdviewModule.UsdviewApi = usdviewAPI

        # Usdview stubs are not up to date, so need to ignore typing
        data_model = self.usdviewAPI.dataModel

        selection_model = data_model.selection  # type: ignore
        selection_model.signalPrimSelectionChanged.connect(self.primSelectionChangedCallback)
        selection_model.signalPropSelectionChanged.connect(self.propSelectionChangedCallback)

        view_settings = data_model.viewSettings  # type: ignore
        view_settings.signalSettingChanged.connect(self._on_view_settings_changed)

        data_model.signalStageReplaced.connect(self._on_stage_replaced)  # type: ignore

        self._current_cam = None

        # Adds a delay to the set_viewport_camera as a workaround to remove thread racing.
        # Initialised here as well as on first use: _on_view_settings_changed reads it to tell
        # a free camera we caused to be rebuilt from one the user owns, and that runs on any
        # view setting change, long before anything asks for a viewport camera.
        self._snap_prim = None
        #: The user's own free-camera field of view, held across usdview rebuilding it.
        self._free_camera_fov = None
        #: The user's own free-camera near/far overrides (None each when usdview's defaults),
        #: held for the same reason; while our prim is viewed the free camera wears the prim's.
        self._free_camera_clip = None
        #: Set by release_viewport_camera: the next free camera usdview builds is the first
        #: one after our prim let go of the view, and gets the user's lens put back.
        self._restore_lens = False
        self._camera_snap_timer = QtCore.QTimer()
        self._camera_snap_timer.setSingleShot(True)
        self._camera_snap_timer.timeout.connect(self._on_snap_timer)

        super().__init__()

    def get_current_frame(self) -> float:
        return self.usdviewAPI.dataModel.currentFrame

    def get_end_frame(self) -> float:
        return self.usdviewAPI.dataModel.frameRangeEnd

    def get_start_frame(self) -> float:
        return self.usdviewAPI.dataModel.frameRangeBegin

    def set_current_frame(self, frame: float):
        appCtrl = self.usdviewAPI._UsdviewApi__appController
        appCtrl.setFrame(frame)
        QtCore.QCoreApplication.processEvents()

    def set_end_frame(self, frame: float):
        stage = self.get_stage()
        if not stage:
            return

        appCtrl = getattr(self.usdviewAPI, "_UsdviewApi__appController")
        with Usd.EditContext(self.get_stage(), stage.GetSessionLayer()):
            stage.SetMetadata("endFrame", frame)
            self.usdviewAPI.dataModel.frameRangeEnd = frame
            appCtrl.realEndTimeCode = frame
        appCtrl._UpdateTimeSamples()

    def set_start_frame(self, frame: float):
        stage = self.get_stage()
        if not stage:
            return

        appCtrl = getattr(self.usdviewAPI, "_UsdviewApi__appController")
        with Usd.EditContext(self.get_stage(), stage.GetSessionLayer()):
            stage.SetMetadata("startFrame", frame)
            self.usdviewAPI.dataModel.frameRangeBegin = frame
            appCtrl.realStartTimeCode = frame
        appCtrl._UpdateTimeSamples()

    def should_install_package(self, package_name: str) -> bool:
        return chunk_qt.confirm_dial(f"Install package dependency {package_name}?")

    def primSelectionChangedCallback(self, added, removed):
        if not added and not removed:
            return
        return super().primSelectionChangedCallback()

    def get_stage(self) -> Usd.Stage:
        return self.usdviewAPI.stage  # type: ignore

    def get_selected_prims(self) -> list[Usd.Prim]:
        return self.usdviewAPI.selectedPrims  # type: ignore

    def select_prims(self, paths) -> bool:
        stage = self.get_stage()
        if not stage:
            return False
        # usdview's selection raises on a path with no prim behind it, so only real ones go in.
        targets = [p for p in (Sdf.Path(str(p)) for p in paths) if stage.GetPrimAtPath(p)]
        if not targets:
            return False
        selection = self.usdviewAPI.dataModel.selection  # type: ignore
        # One selection-changed signal for the lot, not one per path.
        with selection.batchPrimChanges:
            selection.clearPrims()
            for path in targets:
                selection.addPrimPath(path)
        return True

    def get_selected_property(self) -> Usd.Property:
        return self.usdviewAPI.property  # type: ignore

    def _get_ctrl(self):
        try:
            return self.usdviewAPI._UsdviewApi__appController
        except Exception:
            return None

    def refresh_timeline(self, start: float, end: float) -> None:
        ctrl = self._get_ctrl()
        if ctrl:
            try:
                ctrl.realStartTimeCode = start
                ctrl.realEndTimeCode = end
                ctrl._UpdateTimeSamples()
            except Exception:
                pass

    def _get_stage_view(self):
        ctrl = self._get_ctrl()
        return getattr(ctrl, "_stageView", None) if ctrl else None

    def get_viewport_renderer(self) -> str:
        view = self._get_stage_view()
        if view is None:
            return ""
        try:
            return str(view.GetCurrentRendererId() or "")
        except Exception:
            return ""

    def set_viewport_renderer_settings(self, settings) -> None:
        """Hand settings to the delegate usdview is drawing with.

        StageView.SetRendererSetting is the same call usdview's own Hydra Settings menu
        makes, so this reaches the delegate exactly where the built-in UI does -- and
        the two do overwrite each other, last write winning, since there is only one
        delegate between them.

        Each call redraws the viewport, so the mapping handed in is expected to hold
        only what actually changed. One bad setting is skipped rather than abandoning
        the rest: keys come from what a delegate said it accepts, but a value can still
        be one it refuses.
        """
        view = self._get_stage_view()
        if view is None:
            return

        for key, value in settings.items():
            try:
                view.SetRendererSetting(Tf.Token(key), value)
            except Exception as exc:  # noqa: BLE001 - a value this delegate will not take
                logger.debug("Could not set renderer setting %s=%r: %s", key, value, exc)

    def _get_free_camera(self):
        app = self.usdviewAPI._UsdviewApi__appController
        return app._dataModel.viewSettings.freeCamera

    def get_active_camera(self):
        free_cam = self._get_free_camera()
        if free_cam is None:
            return None
        return free_cam.computeGfCamera(Gf.BBox3d())

    def _set_viewport_camera(self, prim):
        app = self.usdviewAPI._UsdviewApi__appController
        app._dataModel.viewSettings.cameraPrim = prim

    def set_viewport_camera(self, prim) -> None:
        # Before pointing the viewport at a prim, while the free camera is still the user's
        # own. From here on usdview rebuilds it from that prim on every navigation, so this is
        # the last moment its lens means anything.
        self._remember_free_camera_lens()
        self._snap_prim = prim
        self._camera_snap_timer.start(150)

    def _remember_free_camera_lens(self) -> None:
        """Snapshot the free camera's lens, but only while it is genuinely the user's.

        Skipped whenever a camera prim is already in view, because the free camera is then a
        copy of *that* camera and remembering it would bake the leak in permanently instead of
        undoing it -- the lock re-asserts the prim after every move, so this is called
        repeatedly while a chunk camera is up. Skipped just the same mid-navigation while our
        prim holds the lock: usdview has switched to a free camera for the drag, but that
        camera was built from the prim and wears its lens, not the user's.
        """
        if self._snap_prim is not None:
            return
        try:
            if self.usdviewAPI.dataModel.viewSettings.cameraPrim is not None:
                return
            free_cam = self._get_free_camera()
            fov = getattr(free_cam, "fov", None)
        except Exception:  # noqa: BLE001 - a lens is never worth raising into the host over
            return
        if fov is not None:
            self._free_camera_fov = fov
        self._free_camera_clip = _clip_overrides(free_cam)

    def _on_view_settings_changed(self):
        current_cam = self._get_free_camera()

        if current_cam == self._current_cam:
            return

        if self._current_cam is not None:
            self._current_cam.signalFrustumChanged.disconnect(self.activeCameraChangedCallback)

        if current_cam is not None:
            self._current_cam = current_cam
            # Before connecting, deliberately: the fov setter emits signalFrustumChanged, and
            # hearing our own correction come back as a camera move would write the prim and
            # re-arm the snap, i.e. the very loop this is here to break.
            self._reconcile_free_camera_lens(current_cam)
            self._current_cam.signalFrustumChanged.connect(self.activeCameraChangedCallback)

    def _reconcile_free_camera_lens(self, free_cam) -> None:
        """Keep the user's own free-camera lens across usdview rebuilding it.

        usdview cannot tumble a camera prim, so any navigation while one is the viewport
        camera calls `switchToFreeCamera()`, which builds a **new** FreeCamera with
        `FreeCamera.FromGfCamera()` -- copying that prim's focal length and aperture. With
        `lock_to_freecam` re-asserting the prim after every move, that happens on every
        navigation, so the user's navigation lens is repainted with the chunk camera's each
        time -- a 50mm on a 13.3mm aperture makes the free camera a 15-degree telephoto.

        Which way to go is decided by whether a prim of ours is being viewed. If none is, this
        free camera is the user's and its lens is the thing worth remembering. If one is, the
        rebuild is a consequence of our own snapping, and the copied lens is left exactly as
        usdview made it.

        Left, not undone. Putting the user's lens back on every rebuild would make the lock
        pop: every navigation would start by zooming out to the user's lens, and every pause
        would end with a snap back in to the prim's. While the lock
        holds, the free camera IS the chunk camera being flown -- seeing its lens is the
        point -- so the two must agree, and the copy usdview makes is the agreement. The
        user's own lens comes back once, on the first rebuild after release_viewport_camera.

        Clipping is the one thing the copy does *not* carry, so it is put on here. FromGfCamera
        takes the prim's clippingRange along, but the very next computeGfCamera resets near and
        far to usdview's defaults unless the free camera holds an override -- so a camera
        clipped at 0.1..50 in the prim was flown at 1..2000000 the moment the user dragged.
        The prim's range goes on as the override, and the user's own overrides (the Camera >
        Set Clipping dialog) come back with their lens on release.
        """
        if self._snap_prim is not None:
            _set_clip_overrides(free_cam, _prim_clipping(self._snap_prim))
            return

        if self._restore_lens:
            self._restore_lens = False
            # Compared rather than set unconditionally: the setter emits regardless, and an
            # unnecessary emit here is an unnecessary cook downstream.
            if self._free_camera_fov is not None and (
                abs(getattr(free_cam, "fov", self._free_camera_fov) - self._free_camera_fov) > 1e-6
            ):
                free_cam.fov = self._free_camera_fov
            _set_clip_overrides(free_cam, self._free_camera_clip)
            return

        self._free_camera_fov = getattr(free_cam, "fov", None)
        self._free_camera_clip = _clip_overrides(free_cam)

    def release_viewport_camera(self) -> None:
        """Our prim stops asserting itself as the viewport camera.

        The view is left where it is -- still through the prim, until the user navigates --
        but from here on a free camera usdview builds is the user's again, so the lens
        remembered at set_viewport_camera goes back on the first one. Idempotent: the
        camera chunk calls this from a callback that also fires on every camera move.
        """
        if self._snap_prim is None:
            return
        self._camera_snap_timer.stop()
        self._snap_prim = None
        # Unconditional, not waiting on a remembered fov: the prim's clipping was put on by us,
        # so even a user whose lens was never learned needs it taken off again.
        # A fov never learned is still left alone -- see _reconcile_free_camera_lens.
        self._restore_lens = True

    def _on_snap_timer(self):
        # IsValid: the stage can be closed between the snap being queued and this firing
        # (File > Open, or usdview's --quitAfterStartup), and usdview raises on an expired
        # prim -- which, from a timer slot, takes the application down with it.
        if self._snap_prim is not None and self._snap_prim.IsValid():
            self._set_viewport_camera(self._snap_prim)

    def _on_stage_replaced(self):
        self.notify_stage_replaced(self.get_stage())


def launch_chunks(usdviewApi) -> None:
    """Launch the UI Creator plugin.

    Args:
        usdviewApi: The Usdview API instance to use for module creation.
    """
    # The order of these does not matter. We can initialize the QT widgets before the ChunkRegistry since
    # the QT widget subscribes to the CHUNK_LOADED notice, and will add chunks when that notice is received.

    scene = UsdviewScene(usdviewApi)

    chunk_qt.build_chunk_docker(usdviewApi.qMainWindow)
    # After the dock exists, before the stage is built: restoreState can only place docks
    # that are already there, and doing it here means the window is the right size for the
    # first thing drawn into it rather than after.
    chunk_window_state.install(usdviewApi.qMainWindow)
    chunk_registry.ChunkRegistry().initialize(scene, "chunk_usdview")
    chunk_stage.initialize(usdviewApi.stage, scene)
    # The first chunk menu otherwise pays for building the shader registry (seconds).
    shader_defs.warm_registry_in_background()


class ChunksPluginContainer(PluginContainer):
    def openModuleSettings(self, usdviewApi):
        dial = chunk_qt.ChunkModuleLoader(usdviewApi.qMainWindow)
        dial.show()

    def reloadChunks(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()
        reg.reload_modules()

    def saveLayers(self, usdviewApi):
        reg = chunk_stage.ChunkLayerManager()
        # flt = "*.usda *.usd *.usdc"
        if not (path := chunk_qt.file_dial(directory=True)):
            return

        reg.save_layers(path)

    def saveScene(self, usdviewApi):
        """The "nice save flow" -- the chunk graph plus correct up-axis/units.
        See chunk_qt.save_scene_dialog.
        """
        chunk_qt.save_scene_dialog()

    def saveChunkGroup(self, usdviewApi):
        """Export the current chunk graph as one clean, portable USD layer.
        See chunk_qt.save_chunkgroup_dialog.
        """
        chunk_qt.save_chunkgroup_dialog()

    def importChunk(self, usdviewApi):
        reg = chunk_registry.ChunkRegistry()

        flt = "*" + chunk_registry.PY_MODULE_EXT
        if file := chunk_qt.file_dial(flt, create_new=False):
            reg.import_chunk(file)

    def openLayerStack(self, usdviewApi):
        """Open the global Layer Stack inspector window."""
        # Keep a reference so the window isn't garbage-collected while open.
        self._layerStackWin = chunk_qt_layer_stack.open_window(usdviewApi)

    def registerPlugins(self, plugRegistry, plugCtx) -> None:
        """Register the UI Creator plugin with the plugin registry.

        Args:
            plugRegistry: The plugin registry to register the UI Creator plugin with.
            plugCtx: The Usdview API instance to use for module creation.
        """
        self._openModuleSettingsCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.openModuleSettings", "Modules", self.openModuleSettings
        )
        self._saveLayersCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.saveLayers", "Save Layers", self.saveLayers
        )

        self._saveSceneCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.saveScene", "Save Scene", self.saveScene
        )

        self._saveChunkGroupCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.saveChunkGroup", "Save ChunkGroup", self.saveChunkGroup
        )

        self._importChunkCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.importChunk", "Import Module", self.importChunk
        )

        self._reloadChunkCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.reloadChunk", "Reload Chunks", self.reloadChunks
        )

        self._layerStackCommand = plugRegistry.registerCommandPlugin(
            "ChunksPlugin.openLayerStack", "Layer Stack", self.openLayerStack
        )

        launch_chunks(plugCtx)

    def configureView(self, plugRegistry, plugUIBuilder) -> None:
        menu = plugUIBuilder.findOrCreateMenu("Chunks")
        menu.addItem(self._openModuleSettingsCommand)
        menu.addItem(self._importChunkCommand)
        menu.addSeparator()
        menu.addItem(self._saveLayersCommand)
        menu.addSeparator()
        menu.addItem(self._saveSceneCommand)
        menu.addItem(self._saveChunkGroupCommand)
        menu.addSeparator()
        menu.addItem(self._layerStackCommand)
        menu.addItem(self._reloadChunkCommand, "SHIFT+R")


Tf.Type.Define(ChunksPluginContainer)
