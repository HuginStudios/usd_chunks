from __future__ import annotations
from pxr import UsdSemantics, Vt

from usd_chunks import chunk, chunk_utils


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

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
            "accumulating them."
        ),
    )
