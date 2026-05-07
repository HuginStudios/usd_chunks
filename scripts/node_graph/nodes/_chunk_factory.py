"""Chunk node factory — dynamically generates UsdNodeBase subclasses from chunk modules.

This module owns the single responsibility of turning a loaded chunk module into
a node class that the graph can register and execute.  It has no dependency on
graph_widget or the executor — callers wire those concerns up separately.
"""
from __future__ import annotations

from PySide6 import QtCore
from usd_chunks import chunk, chunk_core, chunk_registry
from usd_chunks.chunk import Button

from ._base import ChunkWrapperBase, PORT_COLORS
from ..chunk_adapter import ChunkAdapter


def make_chunk_node(module_name: str, chunk_name: str) -> type:
    """Return a UsdNodeBase subclass that wraps the named chunk module.

    The returned class is anonymous (name mangled from module_name) and can be
    registered with NodeGraphQt like any hand-written node class.
    """

    class _AutoChunkNode(ChunkWrapperBase):
        __identifier__ = "usd_chunks"
        NODE_NAME = chunk_name
        CATEGORY = "Chunks"
        _chunk_module = module_name
        _chunk_display_name = chunk_name

        def _ensure_chunk(self):
            my_chunk = getattr(self, "_my_chunk", None)
            if my_chunk is not None:
                return my_chunk
            try:
                created = chunk.create_chunks(self._chunk_module)
            except Exception as e:
                print(f"[NodeGraph] ensure_chunk err: {e}")
                created = None
            if not created:
                return None
            my_chunk = created[0]
            self._my_chunk = my_chunk
            self._chunk_display_name = my_chunk.name
            try:
                self.set_name(my_chunk.name)
            except Exception:
                pass
            return my_chunk

        def __init__(self):
            super().__init__()
            self.add_output("prim", color=PORT_COLORS["prim"])
            self.add_output("layer", color=PORT_COLORS["layer"])
            _needs_prim = False
            try:
                reg = chunk_registry.ChunkRegistry()
                mod = None
                for cm in reg.get_chunk_modules():
                    if cm.module_name == self._chunk_module:
                        mod = cm.module
                        break
                if mod and hasattr(mod, "main"):
                    tmp = mod.main()
                    if tmp:
                        _F = chunk_core.ChunkFlags.ParamType
                        _TYPE_COLORS = {
                            _F.FLOAT: PORT_COLORS["float"],
                            _F.INT: PORT_COLORS["float"],
                            _F.STRING: PORT_COLORS["string"],
                            _F.BOOL: PORT_COLORS["bool"],
                        }

                        def _walk_params(params):
                            for p in params:
                                if isinstance(p, chunk_core.ChunkGroupBase):
                                    _walk_params(p.get_params())
                                elif isinstance(p, chunk_core.ChunkVectorBase):
                                    for sub in p:
                                        self.add_text_input(sub.name, sub.name,
                                                            text=str(sub.value if sub.value else 0))
                                        ap = self.get_input(f"_{sub.name}")
                                        if ap:
                                            ap.color = PORT_COLORS["float"]
                                        out_name = f"_{sub.name}"
                                        existing_out = {op.name() for op in self.output_ports()}
                                        if out_name not in existing_out:
                                            self.add_output(out_name, color=PORT_COLORS["float"])
                                elif isinstance(p, Button):
                                    pass
                                elif isinstance(p, chunk_core.ChunkParamBase):
                                    self._add_chunk_port(p, _TYPE_COLORS)
                                    if hasattr(p, 'has_flag') and p.has_flag(
                                            chunk_core.ChunkFlags.ParamFlags.PRIM_SELECTION):
                                        nonlocal _needs_prim
                                        _needs_prim = True
                        _walk_params(tmp.get_params())
            except Exception:
                pass
            if _needs_prim:
                self.add_input("prim", color=PORT_COLORS["prim"])
            for w in self.view._widgets.values():
                w.setVisible(False)
            self._folded = True
            self.view._folded_state = True

            def _fold_ports():
                try:
                    for port in self.input_ports():
                        if port.name().startswith("_"):
                            port.set_visible(False)
                    for port in self.output_ports():
                        if port.name().startswith("_"):
                            port.set_visible(False)
                    self.view.draw_node()
                except Exception:
                    pass
            QtCore.QTimer.singleShot(0, _fold_ports)

        def _add_chunk_port(self, p, type_colors):
            if isinstance(p, (Button, chunk_core.ChunkGroupBase, chunk_core.ChunkVectorBase)):
                return
            _RESERVED = {"width", "height", "name", "color", "icon", "id",
                         "selected", "disabled", "visible", "pos"}
            if p.name in _RESERVED:
                return
            try:
                self.add_text_input(p.name, p.name, text=str(p.value) if p.value else "")
                port_color = PORT_COLORS.get("any", (140, 150, 170))
                pt = p.evaluate_type()
                for base_type, color in type_colors.items():
                    if pt & base_type:
                        port_color = color
                        break
                ap = self.get_input(f"_{p.name}")
                if ap:
                    ap.color = port_color
                out_name = f"_{p.name}"
                existing_out = {op.name() for op in self.output_ports()}
                if out_name not in existing_out:
                    self.add_output(out_name, color=port_color)
            except Exception:
                pass

        def execute(self, stage) -> dict:
            prim = self._get_input_value("prim")

            my_chunk = self._ensure_chunk()
            if not hasattr(self, '_cached_adapter') or not self._cached_adapter.chunk:
                self._cached_adapter = ChunkAdapter(
                    self._chunk_display_name,
                    module_name=self._chunk_module,
                    chunk=my_chunk,
                )
            adapter = self._cached_adapter
            if not adapter.chunk:
                return {"prim": prim}

            self._subscribe_to_chunk_notices(adapter.chunk)

            self._executing = True
            try:
                old_sel = None
                if prim:
                    scene = chunk_registry.ChunkRegistry().scene
                    ctrl = scene.usdviewAPI._UsdviewApi__appController
                    old_sel = scene.get_selected_prims()
                    ctrl._dataModel.selection.setPrim(prim)

                def _push_params(params):
                    for p in params:
                        if isinstance(p, chunk_core.ChunkGroupBase):
                            try:
                                _push_params(p.get_params())
                            except Exception:
                                pass
                        elif isinstance(p, chunk_core.ChunkVectorBase):
                            for sub in p:
                                v = self._get_input_value(f"_{sub.name}")
                                if v is not None:
                                    adapter.set_param(sub.name, float(v))
                        elif isinstance(p, Button):
                            pass
                        elif hasattr(p, "value"):
                            v = self._get_input_value(f"_{p.name}")
                            if v is not None:
                                adapter.set_param(p.name, v)
                            else:
                                existing_ref = getattr(p, "value_reference", None)
                                prop_val = self.get_property(p.name)
                                if isinstance(prop_val, str) and prop_val.startswith("@"):
                                    adapter.set_param(p.name, prop_val)
                                elif existing_ref and not (
                                        isinstance(prop_val, str) and prop_val.startswith("@")):
                                    try:
                                        self.set_property(p.name, existing_ref)
                                    except Exception:
                                        pass
                                elif prop_val is not None and prop_val != "":
                                    try:
                                        adapter.set_param(p.name, prop_val)
                                    except Exception:
                                        pass

                try:
                    _push_params(adapter.chunk.get_params())
                except Exception:
                    _push_params(adapter.get_params())

                try:
                    from usd_chunks import chunk_notice as _cn
                    _ic_notice = _cn.get_notice(_cn.ChunkNoticeAlias.INPUT_CHANGED)()

                    def _fire_ref_params(container):
                        try:
                            my_params = container.get_my_params()
                        except Exception:
                            return
                        group_had_ref = False
                        for p in my_params:
                            if isinstance(p, chunk_core.ChunkGroupBase):
                                _fire_ref_params(p)
                            elif isinstance(p, chunk_core.ChunkVectorBase):
                                for sub in p:
                                    if getattr(sub, "value_reference", None):
                                        try:
                                            sub.data_changed(_ic_notice)
                                            group_had_ref = True
                                        except Exception:
                                            pass
                            else:
                                if getattr(p, "value_reference", None):
                                    try:
                                        p.data_changed(_ic_notice)
                                        group_had_ref = True
                                    except Exception:
                                        pass
                        if group_had_ref and hasattr(container, "send_self"):
                            try:
                                container.send_self()
                            except Exception:
                                pass

                    _fire_ref_params(adapter.chunk)
                except Exception as e:
                    print(f"[NodeGraph] ref refresh err: {e}")

                # Press the chunk's execute button so execute-button-pattern chunks
                # re-run their _execute callback with the current param values.
                # Chunks with group/param callbacks already fired during _push_params;
                # for them press_button() is a no-op (no Button param found).
                try:
                    adapter.press_button()
                except Exception:
                    pass

                out_prim = prim
                try:
                    layer = adapter.chunk.get_edit_context()
                    if layer:
                        for prim_spec in layer.rootPrims:
                            resolved = stage.GetPrimAtPath(prim_spec.path)
                            if resolved and resolved.IsValid():
                                out_prim = resolved
                                break
                except Exception:
                    pass
                if out_prim is prim:
                    def _find_output_prim(params):
                        for p in params:
                            if isinstance(p, chunk_core.ChunkGroupBase):
                                r = _find_output_prim(p.get_params())
                                if r:
                                    return r
                                continue
                            if not hasattr(p, 'value') or not p.value:
                                continue
                            sval = str(p.value)
                            if sval.startswith("/") and not sval.startswith("//"):
                                resolved = stage.GetPrimAtPath(sval)
                                if resolved and resolved.IsValid():
                                    return resolved
                        return None
                    found = _find_output_prim(adapter.chunk.get_params())
                    if found:
                        out_prim = found

                if prim and old_sel:
                    ctrl._dataModel.selection.setPrim(old_sel[0])

                out_layer = None
                try:
                    out_layer = adapter.chunk.get_edit_context()
                except Exception:
                    pass

                result = {"prim": out_prim, "layer": out_layer}
                try:
                    def _export_params(params):
                        for p in params:
                            if isinstance(p, chunk_core.ChunkGroupBase):
                                _export_params(p.get_params())
                            elif isinstance(p, chunk_core.ChunkVectorBase):
                                for sub in p:
                                    if hasattr(sub, 'value'):
                                        result[f"_{sub.name}"] = sub.value
                            elif hasattr(p, 'name') and hasattr(p, 'value'):
                                result[f"_{p.name}"] = p.value
                    _export_params(adapter.chunk.get_params())
                except Exception:
                    pass
                return result
            finally:
                self._executing = False

    _AutoChunkNode.__name__ = f"Auto_{module_name.replace('.', '_')}"
    _AutoChunkNode.__qualname__ = _AutoChunkNode.__name__
    return _AutoChunkNode
