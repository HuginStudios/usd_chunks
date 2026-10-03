"""Write a chunk object straight onto a stage as a chunk prim -- for tests only.

Moved here from chunk_stage, where nothing in the framework called it any more: a running
session authors chunk prims through ChunkStageSync as chunks are added and changed. Tests
still use it to get a chunk's prim onto a bare stage without a session, so it lives with
them, unchanged.
"""
from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdUI

from usd_chunks import chunk_core as core
from usd_chunks import chunk_utils
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_stage import (
    _CHUNK_PRIM_TYPENAME,
    _CHUNK_TYPE_ATTR,
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    _INPUTS_PREFIX,
    _NODE_STEP,
    _connected_source_paths,
    _define_chunk_parent,
    _iter_chunk_params,
    _iter_chunk_vectors,
    _iter_list_group_arrays,
    _resolve_chunk_root,
    _sdf_type_and_value,
    _sdf_vector_type,
    _store_row_expressions,
    exec_dependencies,
    exec_input_connections,
    exec_output_path,
    get_chunk_prim_path,
    invalidate_edit_layer_memo,
    is_transient_param,
)
from usd_chunks.gizmo import runtime as chunk_gizmo


def _param_wire_source_paths(prim: Usd.Prim) -> set[str]:
    """The prim paths this chunk reads through a *param* wire, exec slots excluded."""
    exec_sources = {str(conn.GetPrimPath()) for conn in exec_input_connections(prim)}
    return _connected_source_paths(prim) - exec_sources


def _author_prim(
    stage: Usd.Stage, chunk: core.ChunkBase, prev_chunk: core.ChunkBase | None = None, index: int = 0
) -> None:
    chunk_root = _resolve_chunk_root(chunk)
    _define_chunk_parent(stage, chunk_root)

    prim = stage.DefinePrim(Sdf.Path(f"{chunk_root}/{chunk.name}"), _CHUNK_PRIM_TYPENAME)

    ui_api = UsdUI.NodeGraphNodeAPI.Apply(prim)
    if not ui_api.GetPosAttr().HasAuthoredValue():
        ui_api.CreatePosAttr(Gf.Vec2f(index * _NODE_STEP, 0.0))

    config = chunk.get_config()
    if config and config.module:
        attr = prim.CreateAttribute(_CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token, custom=False)
        attr.SetVariability(Sdf.VariabilityUniform)
        attr.Set(config.module)

    if not (config and config.terminal):
        exec_out = prim.CreateAttribute(_EXEC_OUTPUT, Sdf.ValueTypeNames.Token, custom=False)
        exec_out.SetVariability(Sdf.VariabilityUniform)

    if config and getattr(config, "outputs", None):
        for out_name, out_type_name in config.outputs:
            out_type = getattr(Sdf.ValueTypeNames, out_type_name, None)
            if out_type is not None:
                prim.CreateAttribute(f"outputs:{out_name}", out_type, custom=False)

    chunk_gizmo.author_gizmos(stage, prim, chunk)

    # The stage's exec wiring comes first, and the Python DAG is only the fallback for a
    # chunk that has none yet. The DAG now also holds edges reconciled from *param* wires
    # (see _wired_dependencies), and authoring one of those as inputs:chunk would
    # invent an exec edge the user never drew -- so the fallback filters them back out.
    deps = exec_dependencies(chunk)
    if not deps:
        wired = _param_wire_source_paths(prim)
        deps = [
            dep for _, dep in chunk.get_dependencies()
            if isinstance(dep, core.ChunkBase) and get_chunk_prim_path(dep) not in wired
        ]
    if not deps and prev_chunk:
        deps = [prev_chunk]
    if deps:
        exec_in = prim.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
        exec_in.SetConnections([Sdf.Path(exec_output_path(dep)) for dep in deps])
        invalidate_edit_layer_memo()

    # Build a name → context_param map for lofted params (direct ChunkParamBase children of context)
    context = chunk.get_context()
    lofted: dict[str, core.ChunkParamBase] = {}
    if context:
        for p in context.get_my_params():
            if isinstance(p, core.ChunkParamBase) and getattr(p, "_param_attr_path", None):
                lofted[p.name] = p

    for path, param in _iter_chunk_params(chunk):
        store_value = param.raw_value
        if store_value is None:
            store_value = param.value  # proxy params delegate raw_value to the template (None); use value instead
        if store_value is None or is_transient_param(param):
            continue
        if store_value == getattr(param, "default_value", None):
            continue
        sdf_type, usd_value = _sdf_type_and_value(store_value)
        if sdf_type is None:
            continue

        attr_name = f"{_INPUTS_PREFIX}{path.replace('.', ':')}"
        ctx_param = lofted.get(param.name)

        if ctx_param:
            # Param is lofted: write a connection on the inner attr and author the context prim's attr
            ctx_attr_path_str = ctx_param._param_attr_path  # e.g. "/chunks.inputs:mySlider"
            ctx_prim_str, ctx_attr_name = ctx_attr_path_str.rsplit(".", 1)
            ctx_prim = stage.GetPrimAtPath(Sdf.Path(ctx_prim_str))
            if not (ctx_prim and ctx_prim.IsValid()):
                ctx_prim = stage.DefinePrim(Sdf.Path(ctx_prim_str), _CHUNK_PRIM_TYPENAME)
            ctx_attr = ctx_prim.CreateAttribute(ctx_attr_name, sdf_type, custom=True)
            ctx_attr.Set(ctx_param.value if ctx_param.value is not None else param.value)

            inner_attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
            inner_attr.Set(ctx_param.value if ctx_param.value is not None else usd_value)
            inner_attr.SetConnections([Sdf.Path(ctx_attr_path_str)])
        else:
            attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
            attr.Set(usd_value)
            if isinstance(store_value, str) and chunk_utils.has_expression(store_value):
                attr.SetConnections([
                    Sdf.Path(f"{chunk_root}/{node_name}.inputs:{param_name}")
                    for node_name, param_name in chunk_utils.parse_expression_attrs(store_value)
                ])

    for vec_path, vector in _iter_chunk_vectors(chunk):
        raw = vector.raw_value
        is_vec_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
        if not is_vec_expr:
            val = vector.value
            if not val or not any(v is not None for v in val):
                continue
            if val == vector.default_value:
                continue
        prefix = "outputs:" if vector.has_flag(ChunkFlags.ParamFlags.OUTPUT) else _INPUTS_PREFIX
        attr_name = f"{prefix}{vec_path.replace('.', ':')}"
        # If the vector is already wired to a real USD attr, preserve its exact type
        backing = getattr(vector, '_param_attr_path', None)
        if not is_vec_expr and backing:
            real_attr = stage.GetAttributeAtPath(Sdf.Path(backing))
            if real_attr and real_attr.IsValid() and real_attr.HasAuthoredValue():
                if (current_val := real_attr.Get()) is not None:
                    attr = prim.CreateAttribute(attr_name, real_attr.GetTypeName(), custom=False)
                    attr.Set(current_val)
                    continue
        if is_vec_expr:
            type_source = vector.default_value
            sdf_type, _ = _sdf_vector_type(type_source) if type_source else (None, None)
        else:
            sdf_type, usd_value = _sdf_vector_type(val)
        if sdf_type is None:
            continue
        attr = prim.CreateAttribute(attr_name, sdf_type, custom=False)
        if is_vec_expr:
            attr.SetConnections([
                Sdf.Path(f"{chunk_root}/{node_name}.inputs:{param_name}")
                for node_name, param_name in chunk_utils.parse_expression_attrs(raw)
            ])
            continue
        attr.Set(usd_value)

    # Serialize ListGroup template params as arrays: inputs:list_name:param_name = [v0, v1, ...]
    for lg_path, list_group in _iter_list_group_arrays(chunk):
        if not list_group.proxy_params:
            continue
        for proxy_param in list_group.proxy_params[0].get_my_params():
            if not isinstance(proxy_param, core.ChunkParamBase):
                continue
            values = [
                v for i in range(list_group.size)
                if (px := list_group.get_indexed_param(proxy_param.name, i)) is not None
                for v in [px._as_own_number(px.value)]  # see _sync_list_proxy_to_usd
                if v is not None
            ]
            if not values:
                continue
            sdf_type, usd_value = _sdf_type_and_value(values)
            if sdf_type is None:
                continue
            attr = prim.CreateAttribute(f"{_INPUTS_PREFIX}{lg_path.replace('.', ':')}:{proxy_param.name}", sdf_type, custom=False)
            attr.Set(usd_value)
            _store_row_expressions(attr, list_group, proxy_param.name)
