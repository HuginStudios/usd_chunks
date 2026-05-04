"""Event/trigger nodes — fire downstream execution on USD and UI events."""
from PySide6 import QtCore
from ._base import UsdNodeBase, PORT_COLORS, TRIGGER


class _TriggerNodeBase(UsdNodeBase):
    """Base for event-driven trigger nodes. Subclasses set the event type."""
    CATEGORY = "Event"
    _active_triggers = {}  # node_id -> (cleanup_fn, refs_to_keep_alive)
    _min_interval_ms = 0  # 0 = no rate limit; subclasses override for high-frequency events

    def __init__(self):
        super().__init__()
        self.add_text_input("enabled", "Enabled (true/false)", text="true")
        self.add_output("trigger", color=PORT_COLORS["trigger"])
        self.add_output("prim", color=PORT_COLORS["prim"])
        self.add_output("event", color=PORT_COLORS["any"])

    def execute(self, stage):
        enabled = (self.get_property("enabled") or "true").lower() in ("true", "1")
        nid = self.id
        if nid in _TriggerNodeBase._active_triggers:
            entry = _TriggerNodeBase._active_triggers[nid]
            if callable(entry):
                entry()
            elif isinstance(entry, tuple):
                entry[0]()
            del _TriggerNodeBase._active_triggers[nid]
        if enabled:
            result = self._subscribe(stage)
            if result:
                _TriggerNodeBase._active_triggers[nid] = result
        return {"trigger": TRIGGER, "prim": None, "event": "armed" if enabled else "disabled"}

    def _subscribe(self, stage):
        """Override: subscribe to event, return cleanup function."""
        return None

    def _trigger_downstream(self):
        """Called when event fires — schedules downstream execution via executor."""
        if self._min_interval_ms > 0:
            now = QtCore.QDateTime.currentMSecsSinceEpoch()
            last = getattr(self, '_last_trigger_ms', 0)
            if now - last < self._min_interval_ms:
                return
            self._last_trigger_ms = now
        graph_widget = self._get_graph_widget()
        if not graph_widget or not graph_widget._stage:
            return
        graph_widget._executor.mark_dirty(self)

    def _get_graph_widget(self):
        import sys
        ref = sys.modules.get('_node_graph_ref')
        return ref.widget if ref and hasattr(ref, 'widget') else None


class OnSelectionChangedNode(_TriggerNodeBase):
    """Triggers when prim selection changes in usdview. Outputs the newly selected prim."""
    NODE_NAME = "On Selection Changed"

    def __init__(self):
        super().__init__()

    def _subscribe(self, stage):
        from usd_chunks import chunk_registry
        try:
            scene = chunk_registry.ChunkRegistry().scene
            ctrl = scene.usdviewAPI._UsdviewApi__appController
            model = ctrl._dataModel

            def _on_sel_changed(*args):
                try:
                    prims = scene.get_selected_prims()
                    self._output_cache = {
                        "trigger": TRIGGER,
                        "prim": prims[0] if prims else None,
                        "event": "selection_changed",
                    }
                    QtCore.QTimer.singleShot(0, self._trigger_downstream)
                except Exception:
                    pass

            model.selection.signalPrimSelectionChanged.connect(_on_sel_changed)
            return lambda: model.selection.signalPrimSelectionChanged.disconnect(_on_sel_changed)
        except Exception:
            return None

    def execute(self, stage):
        result = super().execute(stage)
        try:
            from usd_chunks import chunk_registry
            scene = chunk_registry.ChunkRegistry().scene
            prims = scene.get_selected_prims() if scene else []
            result["prim"] = prims[0] if prims else None
        except Exception:
            pass
        return result


class OnFrameChangedNode(_TriggerNodeBase):
    """Triggers when the timeline frame changes."""
    NODE_NAME = "On Frame Changed"
    _min_interval_ms = 33  # ~30 fps cap — frame events fire on every scrub tick

    def __init__(self):
        super().__init__()
        self.add_output("frame", color=PORT_COLORS["float"])

    def _subscribe(self, stage):
        from usd_chunks import chunk_registry
        try:
            scene = chunk_registry.ChunkRegistry().scene
            ctrl = scene.usdviewAPI._UsdviewApi__appController
            model = ctrl._dataModel

            def _on_frame(*args):
                try:
                    frame = model.currentFrame.GetValue()
                    self._output_cache = {
                        "trigger": TRIGGER, "prim": None, "event": "frame_changed", "frame": frame,
                    }
                    QtCore.QTimer.singleShot(0, self._trigger_downstream)
                except Exception:
                    pass

            model.signalCurrentFrameChanged.connect(_on_frame)
            return lambda: model.signalCurrentFrameChanged.disconnect(_on_frame)
        except Exception:
            return None

    def execute(self, stage):
        result = super().execute(stage)
        result["frame"] = 0
        try:
            from usd_chunks import chunk_registry
            scene = chunk_registry.ChunkRegistry().scene
            ctrl = scene.usdviewAPI._UsdviewApi__appController
            result["frame"] = ctrl._dataModel.currentFrame.GetValue()
        except Exception:
            pass
        return result


class OnTimerNode(_TriggerNodeBase):
    """Triggers on a repeating timer interval."""
    NODE_NAME = "On Timer"

    def __init__(self):
        super().__init__()
        self.add_text_input("interval", "Interval (seconds)", text="1.0")
        self.add_output("tick", color=PORT_COLORS["float"])
        self._tick_count = 0

    def _subscribe(self, stage):
        interval = int(float(self.get_property("interval") or 1) * 1000)
        timer = QtCore.QTimer()
        timer.setInterval(max(100, interval))

        def _on_tick():
            self._tick_count += 1
            self._output_cache = {
                "trigger": TRIGGER, "prim": None, "event": "timer", "tick": self._tick_count,
            }
            self._trigger_downstream()

        timer.timeout.connect(_on_tick)
        timer.start()
        return lambda: timer.stop()

    def execute(self, stage):
        result = super().execute(stage)
        result["tick"] = self._tick_count
        return result


class OnStageChangedNode(_TriggerNodeBase):
    """Triggers when the USD stage is edited (any prim/attribute change)."""
    NODE_NAME = "On Stage Changed"

    def __init__(self):
        super().__init__()

    def _subscribe(self, stage):
        from pxr import Tf, Usd

        def _on_notice(notice, sender):
            self._output_cache = {"trigger": TRIGGER, "prim": None, "event": "stage_changed"}
            QtCore.QTimer.singleShot(50, self._trigger_downstream)

        self._notice_cb = _on_notice
        self._notice_key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, _on_notice, stage)
        return lambda: self._notice_key.Revoke()

    def execute(self, stage):
        return super().execute(stage)


class OnPrimChangedNode(_TriggerNodeBase):
    """Triggers when a specific prim is modified (any property/child change)."""
    NODE_NAME = "On Prim Changed"

    def __init__(self):
        super().__init__()
        self.add_text_input("prim_path", "Prim Path", text="/")

    def _subscribe(self, stage):
        from pxr import Tf, Usd
        watch_path = self.get_property("prim_path") or "/"

        def _on_notice(notice, sender):
            for p in notice.GetChangedInfoOnlyPaths():
                if str(p).startswith(watch_path):
                    prim = stage.GetPrimAtPath(watch_path)
                    self._output_cache = {
                        "trigger": TRIGGER,
                        "prim": prim if prim.IsValid() else None,
                        "event": str(p),
                    }
                    QtCore.QTimer.singleShot(50, self._trigger_downstream)
                    return
            for p in notice.GetResyncedPaths():
                if str(p).startswith(watch_path):
                    prim = stage.GetPrimAtPath(watch_path)
                    self._output_cache = {
                        "trigger": TRIGGER,
                        "prim": prim if prim.IsValid() else None,
                        "event": f"resynced:{p}",
                    }
                    QtCore.QTimer.singleShot(50, self._trigger_downstream)
                    return

        self._notice_cb = _on_notice
        self._notice_key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, _on_notice, stage)
        return lambda: self._notice_key.Revoke()

    def execute(self, stage):
        result = super().execute(stage)
        path = self.get_property("prim_path") or "/"
        prim = stage.GetPrimAtPath(path)
        result["prim"] = prim if prim and prim.IsValid() else None
        return result


class OnAttributeChangedNode(_TriggerNodeBase):
    """Triggers when a specific attribute value changes."""
    NODE_NAME = "On Attribute Changed"

    def __init__(self):
        super().__init__()
        self.add_text_input("prim_path", "Prim Path", text="/")
        self.add_text_input("attr_name", "Attribute Name", text="")
        self.add_output("value", color=PORT_COLORS["any"])

    def _subscribe(self, stage):
        from pxr import Tf, Usd
        watch_path = self.get_property("prim_path") or "/"
        attr_name = (self.get_property("attr_name") or "").strip()
        if not attr_name:
            return None
        full_path = f"{watch_path}.{attr_name}"

        self._last_value = None
        prim = stage.GetPrimAtPath(watch_path)
        if prim and prim.IsValid():
            attr = prim.GetAttribute(attr_name)
            if attr and attr.IsValid():
                self._last_value = attr.Get()

        def _on_notice(notice, sender):
            for p in notice.GetChangedInfoOnlyPaths():
                if str(p) == full_path or str(p).startswith(watch_path):
                    prim = stage.GetPrimAtPath(watch_path)
                    if not prim or not prim.IsValid():
                        return
                    attr = prim.GetAttribute(attr_name)
                    if not attr or not attr.IsValid():
                        return
                    new_val = attr.Get()
                    if new_val != self._last_value:
                        self._last_value = new_val
                        self._output_cache = {
                            "trigger": TRIGGER,
                            "prim": prim, "event": f"{attr_name}_changed",
                            "value": new_val,
                        }
                        QtCore.QTimer.singleShot(50, self._trigger_downstream)
                    return

        self._notice_cb = _on_notice
        self._notice_key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, _on_notice, stage)
        return lambda: self._notice_key.Revoke()

    def execute(self, stage):
        result = super().execute(stage)
        result["value"] = None
        path = self.get_property("prim_path") or "/"
        attr_name = (self.get_property("attr_name") or "").strip()
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            result["prim"] = prim
            if attr_name:
                attr = prim.GetAttribute(attr_name)
                if attr and attr.IsValid():
                    result["value"] = attr.Get()
        return result
