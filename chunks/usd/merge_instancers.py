"""Merge Instancers chunk -- PointInstancers that share assets become one, so a renderer builds
each asset once.

A renderer builds a PointInstancer's prototypes once per instancer: two scatter layers that
both place the same bush build its meshes twice, however few instances either has (Hydra's
prototype propagation re-roots a copy of every prototype under each instancer; measured in
Storm and V-Ray, a set of nine scanned bushes is ~2-3 GB a copy). USD instancing does not
help, nor do two instancers targeting the same prototype prims. One instancer does.

This chunk finds the instancers whose prototypes are the same assets, writes one instancer per
such group with each asset once, and deactivates the sources. What can be merged without
changing what renders is merged; anything else is left alone and named in the readout:

  * Same asset: a prototype is the asset its deepest reference reaches (file and prim), plus
    the opinions it holds of its own in the shot or in the file holding its instancer -- a
    scatter writes a few (an xformOpOrder, instanceable, its viewport proxy switched off).
    Prototypes merge when both match, and the merged one carries those opinions over. An
    instancer with a pruned prototype is left alone (merge first, then Frustum Prune).
  * Same instancer: purpose (For Final Render: default and render count as one),
    visibility and material binding must match; an instancer with
    primvars, velocities, ids or time samples of its own is left alone.
  * Each source's transform is baked into its instances (a rigid transform with a uniform
    scale; anything else is left alone). Instances switched off are dropped.

The sources are deactivated, not deleted: put this at the end of the chain, so a chunk that
reads an instancer by path (Grow Near, Proximity Field) still reads it -- only what renders
sees the merged one. All edits go to the chunk's own layer.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np
from pxr import Gf, Pcp, Sdf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import chunk, sdf_authoring

_MERGED_KEY = "usd_chunks:merged_into"
_TIME = Usd.TimeCode.Default()
# What a scatter's instancer carries; anything else authored on one is not merged.
_CARRIED = {"positions", "orientations", "scales", "protoIndices", "purpose", "visibility",
            "extent", "xformOpOrder", "inactiveIds", "invisibleIds"}


# Prim metadata a prototype's own opinions may set and the merged prototype carries over. On
# the prototype root, active is the instancer's business (a scatter switches its library entry
# back on) and the arcs are what is being matched, so neither counts there.
_ROOT_INFO = ("instanceable", "kind", "variantSelection")
_CHILD_INFO = ("active",) + _ROOT_INFO


def _opinions(proto: Usd.Prim, local: set) -> list[tuple]:
    """The opinions a prototype holds of its own, in *local* (the shot's layers and the file
    holding its instancer), strongest first per key: (relative path, "info" | "attr" | "rel",
    name, value[, type name]). A scatter writes a few -- an xformOpOrder, instanceable, its
    viewport proxy switched off inside a render-purpose prototype."""
    seen, out = set(), []
    root = proto.GetPath()
    for prim in Usd.PrimRange(proto, Usd.PrimAllPrimsPredicate):
        rel = prim.GetPath().MakeRelativePath(root)
        keys = _ROOT_INFO if prim == proto else _CHILD_INFO
        for spec in prim.GetPrimStack():
            if spec.layer not in local:
                continue
            for key in keys:
                if spec.HasInfo(key) and (rel, "info", key) not in seen:
                    seen.add((rel, "info", key))
                    out.append((rel, "info", key, spec.GetInfo(key)))
            for prop in spec.properties:
                if (rel, prop.name) in seen:
                    continue
                seen.add((rel, prop.name))
                # Its variability and custom flag after the value, to author it the same
                # (_replay); not part of what makes two prototypes one asset (_asset).
                if isinstance(prop, Sdf.AttributeSpec):
                    if prop.HasInfo("timeSamples"):
                        out.append((rel, "animated", prop.name, None))
                    else:
                        out.append((rel, "attr", prop.name, prop.default, str(prop.typeName),
                                    prop.variability, prop.custom))
                else:
                    targets = tuple(t.MakeRelativePath(root) if t.HasPrefix(root) else t
                                    for t in prop.targetPathList.GetAddedOrExplicitItems())
                    out.append((rel, "rel", prop.name, targets, prop.custom))
    return out


def _asset(proto: Usd.Prim, local: set) -> tuple:
    """What a prototype is: the file and prim its deepest reference (or payload) reaches, and
    the opinions it holds of its own -- two prototypes are one asset only when both match.
    ("own", path) when it reaches no file, or animates something of its own."""
    direct = [arc for arc in Usd.PrimCompositionQuery(proto).GetCompositionArcs()
              if arc.GetArcType() in (Pcp.ArcTypeReference, Pcp.ArcTypePayload) and not arc.IsAncestral()]
    opinions = _opinions(proto, local)
    if not direct or any(o[1] == "animated" for o in opinions):
        return ("own", str(proto.GetPath()))
    last = direct[-1]
    layer = last.GetTargetLayer()
    # The value and type name: what an opinion says, not how it was authored.
    signature = tuple(sorted((str(o[0]), o[1], o[2], repr(o[3:5] if o[1] == "attr" else o[3:4])) for o in opinions))
    return ("asset", layer.realPath or layer.identifier, str(last.GetTargetPrimPath()), signature, tuple(opinions))


def _replay(writer: sdf_authoring.Writer, proto_path: Sdf.Path, opinions) -> None:
    """Author a prototype's own opinions again, on the merged prototype that references its
    asset directly -- each property as its source authored it."""
    for rel, kind, name, *value in opinions:
        spec = writer.over(proto_path.AppendPath(rel) if rel != Sdf.Path(".") else proto_path)
        if kind == "info":
            spec.SetInfo(name, value[0])
        elif kind == "attr":
            default, type_name, variability, custom = value
            sdf_authoring.attribute(spec, name, Sdf.ValueTypeNames.Find(type_name), default,
                                    uniform=variability == Sdf.VariabilityUniform, custom=custom)
        elif kind == "rel":
            targets, custom = value
            _relationship(writer, spec, name, [proto_path.AppendPath(t) if not t.IsAbsolutePath() else t
                                               for t in targets], custom=custom)


def _relationship(writer: sdf_authoring.Writer, spec: Sdf.PrimSpec, name: str, targets,
                  custom: bool = False) -> None:
    """The relationship name on spec, targeting targets -- Relationship.SetTargets."""
    prop = spec.relationships[name] if name in spec.relationships else Sdf.RelationshipSpec(spec, name, custom)
    prop.targetPathList.explicitItems = [writer.target_path(t) for t in targets]


def _undefined_parents(stage: Usd.Stage, path: Sdf.Path) -> list[Sdf.Path]:
    """path's ancestors not defined on stage, outermost first -- what Stage.DefinePrim of path
    defines along with it."""
    return [parent for parent in reversed(list(path.GetParentPath().GetAncestorsRange()))
            if parent != Sdf.Path.absoluteRootPath
            and not ((prim := stage.GetPrimAtPath(parent)) and prim.IsDefined())]


def _reason_not_mergeable(pi: UsdGeom.PointInstancer) -> str | None:
    prim = pi.GetPrim()
    for attr in prim.GetAuthoredAttributes():
        name = attr.GetName()
        if attr.GetNumTimeSamples() > 0:
            return f"{name} is animated"
        if name.startswith("primvars:"):
            return "has primvars"
        if name.startswith("xformOp:"):
            continue
        if name not in _CARRIED:
            return f"has {name}"
    if prim.GetMetadata("invisibleIds") and pi.GetInvisibleIdsAttr().GetNumTimeSamples():
        return "invisibleIds are animated"
    m = np.asarray(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(_TIME), dtype=np.float64)[:3, :3]
    s = np.linalg.norm(m, axis=1)
    if s.min() <= 0 or not np.allclose(s, s[0], rtol=1e-6) or not np.allclose(m @ m.T, np.eye(3) * s[0] ** 2, atol=1e-6 * s[0] ** 2):
        return "its transform is not rigid with a uniform scale"
    return None


def _instancer_key(prim: Usd.Prim, final_render: bool) -> tuple:
    """What two instancers must share to merge. *final_render*: default and render purpose
    are one -- both render, though only one draws in a viewport showing proxies."""
    material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
    purpose = UsdGeom.Imageable(prim).ComputePurpose()
    if final_render and purpose == UsdGeom.Tokens.default_:
        purpose = UsdGeom.Tokens.render
    return (purpose, UsdGeom.Imageable(prim).ComputeVisibility(_TIME), str(material.GetPath()) if material else "")


def _groups(members: dict) -> list[list[Sdf.Path]]:
    """Instancers that share an asset, transitively: one group each, two or more members."""
    parent = {p: p for p in members}

    def find(p):
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    by_asset = defaultdict(list)
    for path, (key, assets, _, _) in members.items():
        for a in set(assets):
            by_asset[(key, a)].append(path)
    for paths in by_asset.values():
        for other in paths[1:]:
            parent[find(other)] = find(paths[0])
    groups = defaultdict(list)
    for p in members:
        groups[find(p)].append(p)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def _world_instances(pi: UsdGeom.PointInstancer):
    """Positions, orientations (i, j, k, real) and scales in world space, instances that are
    on only, and the prototype index of each."""
    pos = np.asarray(pi.GetPositionsAttr().Get(_TIME) or [], dtype=np.float64).reshape(-1, 3)
    idx = np.asarray(pi.GetProtoIndicesAttr().Get(_TIME) or [], dtype=np.int64)
    n = len(pos)
    orient = pi.GetOrientationsAttr().Get(_TIME)
    # A quaternion array converts as (i, j, k, real) rows. Read one by one, it was most of a
    # merge: a third of a second for the bathroom's instances.
    q = (np.asarray(orient, dtype=np.float64).reshape(-1, 4)
         if orient else np.tile([0.0, 0.0, 0.0, 1.0], (n, 1)))
    sc = np.asarray(pi.GetScalesAttr().Get(_TIME) or [(1.0, 1.0, 1.0)] * n, dtype=np.float64).reshape(-1, 3)
    mask = pi.ComputeMaskAtTime(_TIME)
    keep = np.asarray(mask, dtype=bool) if mask and len(mask) == n else np.ones(n, dtype=bool)
    m = np.asarray(UsdGeom.Xformable(pi.GetPrim()).ComputeLocalToWorldTransform(_TIME), dtype=np.float64)
    lin, t = m[:3, :3], m[3, :3]
    s = float(np.linalg.norm(lin[0]))
    world = pos @ lin + t
    rot = Gf.Matrix3d(*(lin / s).ravel().tolist()).ExtractRotation().GetQuat()
    r = np.array([*rot.GetImaginary(), rot.GetReal()])
    # Instance rotation first, then the instancer's: q' = r * q (row vectors: x * q * r).
    qx, qy, qz, qw = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    rx, ry, rz, rw = r
    out_q = np.stack([rw * qx + rx * qw + ry * qz - rz * qy,
                      rw * qy - rx * qz + ry * qw + rz * qx,
                      rw * qz + rx * qy - ry * qx + rz * qw,
                      rw * qw - rx * qx - ry * qy - rz * qz], axis=1)
    return world[keep], out_q[keep], (sc * s)[keep], idx[keep]


def execute(node):
    stage = node.get_stage()
    readout = node.get_param("readout")
    if not stage:
        return
    roots = node.get_param("prim_path").get_prims() or [stage.GetPseudoRoot()]
    target_root = Sdf.Path(node.get_param("merged_path").value or "/MergedInstancers")
    suspicious = set(stage.GetLayerStack(includeSessionLayers=True))
    final_render = bool(node.get_param("final_render").value)

    members, skipped = {}, []
    for root in roots:
        for prim in Usd.PrimRange(root):
            if not prim.IsA(UsdGeom.PointInstancer) or prim.GetPath().HasPrefix(target_root):
                continue
            pi = UsdGeom.PointInstancer(prim)
            if reason := _reason_not_mergeable(pi):
                skipped.append(f"{prim.GetName()} ({reason})")
                continue
            local = suspicious | {spec.layer for spec in prim.GetPrimStack()}
            targets = pi.GetPrototypesRel().GetTargets()
            protos = [stage.GetPrimAtPath(t) for t in targets]
            if not all(p and p.IsActive() for p in protos):
                # Re-referencing the asset would bring a pruned prototype back.
                skipped.append(f"{prim.GetName()} (a prototype is pruned: merge before Frustum Prune)")
                continue
            assets = [_asset(p, local) for p in protos]
            if any(a[0] == "own" for a in assets):
                skipped.append(f"{prim.GetName()} (a prototype reaches no asset file, or animates its own)")
                continue
            # (matching key, full identity with the opinions to replay) per prototype
            members[prim.GetPath()] = (_instancer_key(prim, final_render), [a[:4] for a in assets], targets, assets)

    groups = _groups(members)
    made, merges = [], []
    # Everything is read first and written after, in one change block: through the Usd API,
    # each define, reference and opinion replayed recomposed the stage on its own.
    for n, group in enumerate(groups):
        key = members[group[0]][0]
        # One prototype per asset, in first-seen order; each source index remapped onto it.
        protos, slot = [], {}
        positions, orientations, scales, indices = [], [], [], []
        for path in group:
            _, keys, targets, assets = members[path]
            remap = []
            for k, a, t in zip(keys, assets, targets):
                if k not in slot:
                    slot[k] = len(protos)
                    protos.append((a, t))
                remap.append(slot[k])
            p, q, s, i = _world_instances(UsdGeom.PointInstancer(stage.GetPrimAtPath(path)))
            positions.append(p)
            orientations.append(q)
            scales.append(s)
            indices.append(np.asarray(remap, dtype=np.int64)[i] if len(i) else i)
        name = f"{group[0].name}_merged" if n == 0 or group[0].name not in {g[0].name for g in groups[:n]} \
            else f"{group[0].name}_merged_{n}"
        out_path = target_root.AppendChild(name)
        sources = []
        for path in group:
            data = dict(stage.GetPrimAtPath(path).GetMetadata("customData") or {})
            data[_MERGED_KEY] = str(out_path)
            sources.append((path, data))
        merges.append((out_path, protos, positions, orientations, scales, indices, key, sources))
        made.append((len(group), len(protos), sum(len(m[2]) for p, m in members.items() if p in group)))

    writer = sdf_authoring.Writer(stage)
    root_parents = _undefined_parents(stage, target_root) if merges and not stage.GetPrimAtPath(target_root) else None
    with Sdf.ChangeBlock():
        if root_parents is not None:
            for parent in root_parents:
                writer.define(parent)
            writer.define(target_root, "Scope")
        for out_path, protos, positions, orientations, scales, indices, key, sources in merges:
            out = writer.define(out_path, "PointInstancer")
            rel_targets = []
            for k, (asset, _) in enumerate(protos):
                proto_path = out_path.AppendChild("Prototypes").AppendChild(f"proto_{k}")
                writer.define(proto_path.GetParentPath())
                writer.define(proto_path, "Xform").referenceList.Prepend(Sdf.Reference(asset[1], asset[2]))
                _replay(writer, proto_path, asset[4])
                rel_targets.append(proto_path)
            _relationship(writer, out, "prototypes", rel_targets)
            names = Sdf.ValueTypeNames
            sdf_authoring.attribute(out, "positions", names.Point3fArray,
                                    Vt.Vec3fArray.FromNumpy(np.concatenate(positions).astype(np.float32)))
            quats = np.concatenate(orientations).astype(np.float16)
            sdf_authoring.attribute(out, "orientations", names.QuathArray, Vt.QuathArray.FromNumpy(quats))
            sdf_authoring.attribute(out, "scales", names.Float3Array,
                                    Vt.Vec3fArray.FromNumpy(np.concatenate(scales).astype(np.float32)))
            sdf_authoring.attribute(out, "protoIndices", names.IntArray,
                                    Vt.IntArray.FromNumpy(np.concatenate(indices).astype(np.int32)))
            purpose, visibility, material = key
            sdf_authoring.attribute(out, "purpose", names.Token, purpose, uniform=True)
            if visibility == UsdGeom.Tokens.invisible:
                sdf_authoring.attribute(out, "visibility", names.Token, visibility)
            if material:
                schemas = Sdf.TokenListOp()
                schemas.prependedItems = ["MaterialBindingAPI"]
                out.SetInfo("apiSchemas", schemas)
                _relationship(writer, out, "material:binding", [Sdf.Path(material)])
            for path, data in sources:
                src = writer.over(path)
                src.active = False
                # Not .customData: its edit proxy refuses a namespaced key like _MERGED_KEY.
                src.SetInfo("customData", data)

    if readout:
        merged = sum(g for g, _, _ in made)
        text = f"{merged} instancers → {len(made)}" if made else "nothing to merge"
        if made:
            text += f" · prototypes {sum(c for _, _, c in made)} → {sum(p for _, p, _ in made)}"
        if skipped:
            text += "\nleft alone: " + ", ".join(skipped[:8]) + (" …" if len(skipped) > 8 else "")
        readout.value = text


def _on_change(owner, notice, sender):
    """Callback for the chunk panel: author again from scratch, the same way a cook does."""
    _chunk = owner.get_chunk()
    if not owner.get_stage() or not _chunk:
        return
    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    execute(_chunk)


def main():
    chunk_api = chunk.Chunk("Merge Instancers")
    _PF = chunk.ChunkFlags.ParamFlags
    prims = chunk.LineEdit(
        "prim_path", "Prims", flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="Where to look for PointInstancers to merge (globs, comma separated). Empty: the whole stage.",
    )
    merged = chunk.LineEdit(
        "merged_path", "Merged Into", default_value="/World/merged_instancers", flags=_PF.LABELED,
        tooltip="Where the merged instancers are written, one per group of instancers sharing assets.",
    )
    final_render = chunk.Checkbox(
        "final_render", "For Final Render", default_value=False,
        tooltip=(
            "Merge default- and render-purpose instancers as one, written as render purpose: the\n"
            "same final render, but a viewport showing proxies no longer draws the default ones."
        ),
    )
    for param in (prims, merged, final_render):
        param.add_callback(_on_change)
        chunk_api.add_param(param)
    chunk_api.add_param(chunk.Label("readout", default_value="nothing merged yet",
                                    tooltip="What the last cook merged, and what it left alone and why."))
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="scatter",
        requirements=["numpy>=2,<3"],
        description="""
        Merges PointInstancers that share assets into one, so a renderer builds each asset once.

        Merge Instancers — PointInstancers that share assets become one, so a renderer
        builds each asset once instead of once per instancer.

        Parameters:
          - Prims: where to look (globs, comma separated); empty is the whole stage.
          - Merged Into: where the merged instancers are written.
          - For Final Render: merge default- and render-purpose instancers as one
            (render purpose) -- the same render, a different viewport.

        Instancers are merged when their prototypes reach the same asset files and their
        purpose, visibility and material binding match. A prototype with an override of
        its own, or an instancer with primvars, velocities, ids, animation or a transform
        that is not rigid, is left alone and named in the readout. Each source's transform
        is baked into its instances; instances switched off are dropped. The sources are
        deactivated: put this last, after anything that reads them by path.
        """,
    )
