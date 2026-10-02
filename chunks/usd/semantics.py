from __future__ import annotations
import colorsys
import logging

from pxr import Gf, Sdf, Usd, UsdGeom, UsdSemantics, Vt

from usd_chunks import chunk, chunk_utils, sdf_authoring

logger = logging.getLogger(f"usd_chunks.{__name__}")

#: Class colours for the rendered semantic pass: saturated primaries and secondaries first,
#: so a handful of classes read at a glance, then evenly spread hues.
_CLASS_COLOURS = [
    (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 1.0, 0.0),
    (1.0, 0.0, 1.0), (0.0, 1.0, 1.0), (1.0, 0.5, 0.0), (0.5, 0.0, 1.0),
]


def class_colour(class_id):
    """The semantic pass colour of a 1-based class id."""
    if class_id <= len(_CLASS_COLOURS):
        return Gf.Vec3f(*_CLASS_COLOURS[class_id - 1])
    hue = ((class_id - len(_CLASS_COLOURS)) * 0.618033988749895) % 1.0
    return Gf.Vec3f(*colorsys.hsv_to_rgb(hue, 0.7, 0.8))


def _lattice(levels, brightest_at_least):
    return [
        (r, g, b)
        for r in levels for g in levels for b in levels
        if max(r, g, b) >= brightest_at_least
    ]


#: Instance colours: points of an RGB lattice, so every pair is a known distance apart --
#: which is what lets a reader tell two instances apart at an anti-aliased edge. Hue-based
#: palettes drift together as ids grow. The dark corner is left out: black is background,
#: and an instance's edge must stay distinguishable from it.
#:   - 6 levels a channel, 0.2 apart: 152 colours, every pair at least 0.2 apart.
#:   - Then a second lattice offset 0.1 from the first: 117 more, at least 0.1 from all.
_INSTANCE_LATTICES = [
    _lattice([0.0, 0.2, 0.4, 0.6, 0.8, 1.0], 0.6),
    _lattice([0.1, 0.3, 0.5, 0.7, 0.9], 0.5),
]

#: Walked with a stride coprime to each lattice's size, so consecutive ids land far apart
#: rather than on neighbouring lattice points.
_INSTANCE_STRIDE = 67

#: How many instances get a colour of their own; beyond it, colours repeat.
DISTINCT_INSTANCES = sum(len(lattice) for lattice in _INSTANCE_LATTICES)


def instance_colour(instance_id):
    """The instance pass colour of a 1-based instance id.

    Distinct for the first DISTINCT_INSTANCES ids (269), each at least 0.1 from every other
    on some channel; past that, colours repeat and two instances could share one.
    """
    index = (instance_id - 1) % DISTINCT_INSTANCES
    for lattice in _INSTANCE_LATTICES:
        if index < len(lattice):
            return Gf.Vec3f(*lattice[(index * _INSTANCE_STRIDE) % len(lattice)])
        index -= len(lattice)
    raise AssertionError("unreachable")


def _split(text):
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def labelled_instances(stage, root, taxonomy, classes):
    """[(instance prim, class name)] for every prim under `root` labelled with one of
    `classes`, in path order. With no `classes`, every label found counts, sorted by name.

    Returns:
        (instances, classes): the instances, and the class list ids are counted in.
    """
    root_prim = stage.GetPrimAtPath(root)
    if not root_prim or not root_prim.IsValid():
        return [], list(classes)

    found = []
    for prim in Usd.PrimRange(root_prim):
        if not prim.HasAPI(UsdSemantics.LabelsAPI, taxonomy):
            continue
        labels = UsdSemantics.LabelsAPI(prim, taxonomy).GetLabelsAttr().Get() or []
        found.append((prim, [str(label) for label in labels]))

    if not classes:
        classes = sorted({label for _prim, labels in found for label in labels})

    instances = []
    for prim, labels in found:
        name = next((label for label in labels if label in classes), None)
        if name is not None:
            instances.append((prim, name))
    # Sorted by path, not left in traversal order: traversal follows the order prims were
    # authored in, so rewiring the network that builds them renumbered every instance.
    instances.sort(key=lambda pair: pair[0].GetPath())
    return instances, list(classes)


def _covered_gprims(instance, labelled_paths):
    """The gprims an instance's label covers: itself and its descendants, stopping at any
    descendant that carries a label of its own."""
    it = iter(Usd.PrimRange(instance))
    for prim in it:
        if prim != instance and prim.GetPath() in labelled_paths:
            it.PruneChildren()
            continue
        if prim.IsA(UsdGeom.Gprim):
            yield prim


def author_render_primvars(stage, root, taxonomy, classes):
    """Constant primvars a renderer can draw, for every prim labelled under `root`.

    No render delegate reads UsdSemantics, but every one can output a primvar as an AOV
    (a Render Var with sourceType "primvar"). So each gprim a label covers gets

        primvars:semantic_id     int      the class, 1-based in `classes` order; 0 is none
        primvars:semantic_color  color3f  the class's colour in the semantic pass
        primvars:instance_id     int      the labelled prim, 1-based in path order
        primvars:instance_color  color3f  a colour unique to that prim

    and rendering semantic_color and instance_color gives class and instance masks.

    Every labelled prim is numbered, not only this chunk's own targets: ids must be unique
    across the whole scene, and a network usually labels with one Semantic Labels chunk
    per class. Each chunk with this on writes the full numbering from what it can see, and
    the most downstream one -- whose layer is strongest, and which sees every label above
    it -- is the one that holds.

    Returns:
        (instance count, class list)
    """
    instances, classes = labelled_instances(stage, root, taxonomy, classes)
    labelled_paths = {prim.GetPath() for prim, _name in instances}

    # Found first, then written as specs in one change block (sdf_authoring): a dataset
    # scene has thousands of gprims, four primvars each, and the walk must not run over a
    # stage that recomposes under it.
    covered = [
        (gprim.GetPath(), classes.index(name) + 1, instance_id)
        for instance_id, (instance, name) in enumerate(instances, start=1)
        for gprim in _covered_gprims(instance, labelled_paths)
    ]
    writer = sdf_authoring.Writer(stage)
    with Sdf.ChangeBlock():
        for path, class_id, instance_id in covered:
            spec = writer.over(path)
            for pv_name, sdf_type, value in (
                ("semantic_id", Sdf.ValueTypeNames.Int, class_id),
                ("semantic_color", Sdf.ValueTypeNames.Color3f, class_colour(class_id)),
                ("instance_id", Sdf.ValueTypeNames.Int, instance_id),
                ("instance_color", Sdf.ValueTypeNames.Color3f, instance_colour(instance_id)),
            ):
                sdf_authoring.primvar(spec, pv_name, sdf_type, value, UsdGeom.Tokens.constant)
    return len(instances), classes


def execute(node):
    stage = node.get_stage()

    primpath_param = node.get_param("primpath_location")
    if not primpath_param.value:
        return

    label_list = node.get_param("labels", chunk.is_inst(chunk.ListGroup))
    taxonomy_t = label_list.get_param("taxonomy")
    values_t = label_list.get_param("label_values")

    taxonomies: dict[str, list[str]] = {}
    for i in range(label_list.size):
        taxonomy = (label_list.get_indexed_param(taxonomy_t, i).value or "").strip()
        raw_values = label_list.get_indexed_param(values_t, i).value or ""
        values = [v.strip() for v in raw_values.split(",") if v.strip()]
        if not taxonomy or not values:
            continue
        # Two rows naming the same taxonomy merge rather than the last one winning --
        # they both author the same semantics:labels:<taxonomy> attribute.
        existing = taxonomies.setdefault(taxonomy, [])
        existing.extend(v for v in values if v not in existing)

    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param):
        target_prim = stage.OverridePrim(target_path)
        for taxonomy, values in taxonomies.items():
            # CanApply rejects instance names that aren't valid identifiers ("2bad",
            # "my taxo"); skip those rows rather than raising, matching how variant.py
            # skips rows with an empty variant set name.
            if not UsdSemantics.LabelsAPI.CanApply(target_prim, taxonomy):
                continue
            api = UsdSemantics.LabelsAPI.Apply(target_prim, taxonomy)
            api.CreateLabelsAttr().Set(Vt.TokenArray(values))

    if not node.get_param("render_primvars").value:
        return
    taxonomy = (node.get_param("primvar_taxonomy").value or "").strip()
    root = (node.get_param("primvar_root").value or "").strip() or "/"
    if not taxonomy or not Sdf.Path.IsValidPathString(root):
        return
    count, classes = author_render_primvars(stage, Sdf.Path(root), taxonomy, _split(node.get_param("classes").value))
    logger.info("Semantic Labels: render primvars on %d instances, classes %s", count, classes)


def main():
    chunk_api = chunk.Chunk("Semantic Labels")

    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Prims to label — a path, a glob (/chair*), or a %collection reference.",
    )
    chunk_api.add_param(primpath)

    label_list = chunk.ListGroup("labels")
    taxonomy = chunk.LineEdit(
        "taxonomy",
        "Taxonomy",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Classification domain, e.g. category, style, state. Must be a valid identifier.",
    )
    values = chunk.LineEdit(
        "label_values",
        "Labels",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Comma-separated labels for this taxonomy, e.g. chair, furniture.",
    )
    label_list.add_params([taxonomy, values])
    label_list.size = 1
    chunk_api.add_param(label_list)

    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    render_grp = chunk.Group(
        "render_grp", "Render Primvars",
        flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED
        | chunk.ChunkFlags.GroupFlags.FRAMED,
    )
    render_grp.set_state(chunk.ChunkFlags.State.COLLAPSED)
    render_grp.add_params([
        chunk.Checkbox(
            "render_primvars", "Render Primvars", default_value=False,
            tooltip="Also author semantic_color / instance_color primvars on every labelled "
            "prim, for rendering class and instance masks as AOVs. Turn it on in the last "
            "Semantic Labels chunk of a network; it numbers every label it can see.",
        ),
        chunk.LineEdit(
            "primvar_taxonomy", "Taxonomy", default_value="class", flags=labeled,
            tooltip="The taxonomy whose labels are the classes.",
        ),
        chunk.LineEdit(
            "classes", "Classes", flags=labeled,
            tooltip="Comma-separated class names. Their order is the class id: the first "
            "is 1, and 0 is background. Labels not listed get no primvars. Empty takes "
            "every label found, sorted.",
        ),
        # Not PRIM_SELECTION: that pre-fills from the host's selection whenever the chunk
        # is built, loading included, which would empty the root of a saved network.
        chunk.LineEdit(
            "primvar_root", "Root", default_value="/", flags=labeled,
            tooltip="Only labels on this prim and below are numbered.",
        ),
    ])
    chunk_api.add_param(render_grp)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.PROPERTIES,
        description=(
            "Authors UsdSemantics labels — applies SemanticsLabelsAPI:<taxonomy> and sets\n"
            "token[] semantics:labels:<taxonomy> on every matched prim.\n"
            "Prim Path takes comma-separated paths, Houdini-style globs (*, **, ?), a\n"
            "%name collection reference, or embedded variant selections; prims are\n"
            "overridden rather than defined, so they need not exist yet.\n"
            "Each Labels row is one taxonomy: an arbitrary classification domain you\n"
            "invent (category, style, state...) plus a comma-separated list of label\n"
            "tokens. A prim can carry any number of taxonomies at once, and labels\n"
            "accumulate down the hierarchy — descendants inherit whatever their\n"
            "ancestors declare, queryable via UsdSemantics.LabelsQuery.\n"
            "Rows with an empty taxonomy, no labels, or a taxonomy name that isn't a\n"
            "valid identifier are skipped. Each execution clears the edit-target layer\n"
            "and re-authors from scratch, so re-cooking replaces labels rather than\n"
            "accumulating them.\n"
            "\n"
            "Render Primvars (off by default) makes the labels renderable. No render\n"
            "delegate reads UsdSemantics, but every one can output a primvar as an AOV\n"
            "(a Render Var with Source Type \"primvar\"). With it on, every gprim a label\n"
            "under Root covers gets constant primvars semantic_id / semantic_color (its\n"
            "class, numbered in Classes order from 1) and instance_id / instance_color\n"
            "(the labelled prim, numbered in path order from 1), so rendering the two\n"
            "colours gives class and instance masks lined up with the beauty — what the\n"
            "KITTI Writer reads. An instance is the prim the label sits on; its label\n"
            "covers every gprim below it without a label of its own.\n"
            "Every labelled prim is numbered, not just this chunk's own, since ids must\n"
            "be unique across the scene: turn it on in the last Semantic Labels chunk\n"
            "of a network, which sees every label above it."
        ),
    )
