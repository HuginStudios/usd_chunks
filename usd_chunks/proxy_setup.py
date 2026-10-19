"""Set a prim up as the proxy of others: purpose=proxy on it, purpose=render and proxyPrim on them.

That is USD's own proxy pairing. A viewport that draws proxies (usdview by default) shows
the proxy and hides the source; a final render does the opposite. `proxyPrim` tells a tool
looking at the source which prim stands in for it.

Authored on the stage's current edit target, so a chunk calls this inside its own
EditContext and the pairing goes with the chunk's layer.
"""

from __future__ import annotations

from pxr import Sdf, Usd, UsdGeom


def sibling_path(source_path) -> Sdf.Path:
    """Where a proxy of `source_path` goes by default: beside it, as `<name>_proxy`.

    A sibling and not a child, so the source turned `render` does not contain its own proxy.
    """
    source = Sdf.Path(str(source_path))
    if source == Sdf.Path.absoluteRootPath:
        return Sdf.Path("/Proxy")
    return source.GetParentPath().AppendChild(f"{source.name}_proxy")


def _defaults_under(prim) -> list:
    """Imageable prims below `prim` with an authored purpose of `default`.

    An authored purpose wins over the one a prim would inherit, so these would stay visible
    in a proxy view under a source turned `render`. Instances are not entered: their insides
    cannot be edited from here.
    """
    found = []
    for below in Usd.PrimRange(prim):
        if below == prim:
            continue
        attr = UsdGeom.Imageable(below).GetPurposeAttr() if below.IsA(UsdGeom.Imageable) else None
        if attr and attr.HasAuthoredValue() and attr.Get() == UsdGeom.Tokens.default_:
            found.append(below.GetPath())
    return found


def set_up_proxy(stage, sources, proxy_path, copy_of=None) -> None:
    """Make the prim at `proxy_path` the proxy of each prim in `sources`.

    Args:
        stage: The stage to author on, at its current edit target.
        sources: Paths of the prims the proxy stands in for.
        proxy_path: Path of the proxy prim. It must exist.
        copy_of: The source the proxy is a reference to, when it is one. The proxy then
            inherits what is authored here on that source's children too, so the children
            turned `render` are turned back to `proxy` on the copy.
    """
    proxy_path = Sdf.Path(str(proxy_path))
    proxy = stage.GetPrimAtPath(proxy_path)
    UsdGeom.Imageable(proxy).CreatePurposeAttr().Set(UsdGeom.Tokens.proxy)
    if copy_of is not None:
        # The copy would also take the source's proxyPrim, pointing at itself.
        proxy.CreateRelationship(UsdGeom.Tokens.proxyPrim).SetTargets([])
    for source_path in sources:
        source_path = Sdf.Path(str(source_path))
        source = stage.GetPrimAtPath(source_path)
        if not source or source_path == proxy_path:
            continue
        defaults = _defaults_under(source)
        imageable = UsdGeom.Imageable(source)
        imageable.CreatePurposeAttr().Set(UsdGeom.Tokens.render)
        imageable.SetProxyPrim(proxy)
        for path in defaults:
            UsdGeom.Imageable(stage.GetPrimAtPath(path)).CreatePurposeAttr().Set(UsdGeom.Tokens.render)
        if copy_of is not None and source_path == Sdf.Path(str(copy_of)):
            for path in defaults:
                mirrored = stage.GetPrimAtPath(path.ReplacePrefix(source_path, proxy_path))
                if mirrored:
                    UsdGeom.Imageable(mirrored).CreatePurposeAttr().Set(UsdGeom.Tokens.proxy)
