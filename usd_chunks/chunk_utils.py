import re
from contextlib import contextmanager, ExitStack
from pxr import Usd

_EXPR_RE = re.compile(r'@(\w+)\.(\w+)@')


def parse_expression(text: str) -> list[tuple[str, str]]:
    """Return (node_name, param_name) pairs for every @node.param@ token in text."""
    return _EXPR_RE.findall(text)


def has_expression(text: str) -> bool:
    return bool(_EXPR_RE.search(text))


def get_unique_path(stage, prim_path):
    """Return prim_path if unused on stage, else a unique variant with a numeric suffix.

    Also accepts a ChunkParamBase in place of a string.  If the param's raw value is an
    expression the resolved path is returned as-is — expressions define an explicit derived
    path, so uniqueness bumping would be wrong.

    When a param is passed, the chunk's procedural layer is cleared of the old prim spec
    before checking uniqueness, so previous executions don't inflate the suffix counter.
    """
    raw_value = getattr(prim_path, 'raw_value', None)
    if raw_value is not None:
        # Clear the chunk's procedural layer before resolving the path so stale prims
        # from a previous execution are removed regardless of whether the new path
        # comes from a literal value or an expression.
        chunk = getattr(prim_path, 'get_chunk', lambda: None)()
        if chunk is not None:
            layer = chunk.get_edit_context()
            if layer and layer.anonymous:
                layer.Clear()

        if isinstance(raw_value, str) and has_expression(raw_value):
            resolved = getattr(prim_path, 'value', None)
            return str(resolved) if resolved is not None else ""

        prim_path = getattr(prim_path, 'value', prim_path)

    if isinstance(prim_path, str) and has_expression(prim_path):
        return prim_path

    if not stage:
        return prim_path

    def _is_taken(path):
        prim = stage.GetPrimAtPath(path)
        return prim.IsValid() and bool(prim.GetTypeName())

    if not _is_taken(prim_path):
        return prim_path

    m = re.search(r'\d+$', prim_path)
    base = prim_path[: m.start()] if m else prim_path
    count = int(m.group()) + 1 if m else 1
    while _is_taken(f"{base}{count}"):
        count += 1

    return f"{base}{count}"


def split_variant_path(path_str: str) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Return (anchor_path, target_path, [(anchor_prim, variantSet, variant), ...]).

    anchor_path: first prim before any {} (the outermost anchor)
    target_path: full prim path with all variant selections stripped
    selections:  per-selection tuple including the prim that owns each variant set

    /myprim{LOD=high}/geom
        → ('/myprim', '/myprim/geom', [('/myprim', 'LOD', 'high')])

    /myprim{LOD=high}/geom/asset{season=summer}
        → ('/myprim', '/myprim/geom/asset',
           [('/myprim', 'LOD', 'high'), ('/myprim/geom/asset', 'season', 'summer')])

    /myprim
        → ('/myprim', '/myprim', [])
    """
    selections = []
    accumulated = ''
    remaining = path_str

    while remaining:
        path_match = re.match(r'^([^{]*)', remaining)
        path_part = path_match.group(1) if path_match else ''
        accumulated += path_part
        remaining = remaining[len(path_part):]

        var_match = re.match(r'^\{(\w+)=(\w+)\}', remaining)
        if var_match:
            anchor = accumulated.rstrip('/') or '/'
            selections.append((anchor, var_match.group(1), var_match.group(2)))
            remaining = remaining[var_match.end():]

    target = accumulated.rstrip('/') or '/'
    anchor = selections[0][0] if selections else target
    return anchor, target, selections


@contextmanager
def author_in_variant_context(stage, path_str):
    """Context manager that enters any variant edit contexts encoded in path_str.

    Yields target_path (variant specs stripped) inside the appropriate edit contexts.
    Supports variant selections at multiple levels of the path hierarchy.

    with author_in_variant_context(stage, '/prim{LOD=high}/geom/asset{season=summer}') as t:
        stage.DefinePrim(t, 'Xform')  # authored inside both variant contexts
    """
    _, target_path, var_selections = split_variant_path(path_str)

    session_target = Usd.EditTarget(stage.GetSessionLayer())

    session_layer = stage.GetSessionLayer()

    with ExitStack() as stack:
        for anchor_prim_path, vs_name, variant in var_selections:
            anchor_prim = stage.GetPrimAtPath(anchor_prim_path)
            if not anchor_prim.IsValid():
                anchor_prim = stage.OverridePrim(anchor_prim_path)
            vset = anchor_prim.GetVariantSets().GetVariantSet(vs_name)

            # Record only the session-layer opinion — not the composed selection.
            # Restoring the composed prev would bleed upstream chunk-layer defaults
            # back into the session layer, overriding downstream overrides.
            spec = session_layer.GetPrimAtPath(anchor_prim_path)
            prev_session = dict(spec.variantSelections).get(vs_name, "") if spec else ""

            with Usd.EditContext(stage, session_target):
                vset.SetVariantSelection(variant)

            # On exit, restore only what was already in the session layer.
            # If the session layer had no prior opinion, clear it so the
            # session layer stays free of composed (chunk-layer) opinions.
            def _restore(v=vset, p=prev_session):
                with Usd.EditContext(stage, session_target):
                    if p:
                        v.SetVariantSelection(p)
                    else:
                        v.ClearVariantSelection()

            stack.callback(_restore)
            stack.enter_context(vset.GetVariantEditContext())
        yield target_path
