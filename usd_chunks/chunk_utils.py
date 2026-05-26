import re
from contextlib import contextmanager, ExitStack
from pxr import Usd


def get_unique_path(stage, prim_path):
    if not stage.GetPrimAtPath(prim_path).IsValid():
        return prim_path

    # Strip any trailing digits to get the base
    base = re.sub(r'\d+$', '', prim_path)

    count = 1
    while stage.GetPrimAtPath(f"{base}{count}").IsValid():
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

    with ExitStack() as stack:
        for anchor_prim_path, vs_name, variant in var_selections:
            anchor_prim = stage.GetPrimAtPath(anchor_prim_path)
            if not anchor_prim.IsValid():
                anchor_prim = stage.OverridePrim(anchor_prim_path)
            vset = anchor_prim.GetVariantSets().GetVariantSet(vs_name)

            with Usd.EditContext(stage, session_target):
                prev = vset.GetVariantSelection()
                vset.SetVariantSelection(variant)

            # Clean up the session layer selection after the variant context
            # exits (LIFO: callback registered before enter_context).
            def _restore(v=vset, p=prev):
                with Usd.EditContext(stage, session_target):
                    if p:
                        v.SetVariantSelection(p)
                    else:
                        v.ClearVariantSelection()

            stack.callback(_restore)
            stack.enter_context(vset.GetVariantEditContext())
        yield target_path
