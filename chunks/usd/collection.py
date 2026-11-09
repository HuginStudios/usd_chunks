from __future__ import annotations
from pxr import Sdf, Usd
from usd_chunks import chunk

_EXPANSION_RULES = {
    "Explicit": Usd.Tokens.explicitOnly,
    "Expand Prims": Usd.Tokens.expandPrims,
    "Expand Prims and Properties": Usd.Tokens.expandPrimsAndProperties,
}


def execute(node):
    stage = node.get_stage()

    name = node.get_param("collection_name").value
    owner_path = node.get_param("owner_path").value
    if not name or not owner_path:
        return

    # DefinePrim (not OverridePrim) so this shows up as a "defined" prim -- a pure
    # override with no other definer anywhere is skipped by the default stage
    # traversal predicate, which would make the collection invisible to
    # chunk_utils.find_collection's %name lookup.
    prim = stage.DefinePrim(owner_path, "Scope")
    if not prim.IsValid():
        return

    collection = Usd.CollectionAPI.Apply(prim, name)

    expression = node.get_param("membership_expression").value
    if expression:
        # Expression mode replaces includes/excludes rather than stacking with them --
        # USD ORs expression membership with rel-target membership rather than ANDing,
        # so authoring both produces confusing results (an exclude rel target can be
        # pulled back in by an unrelated expression match).
        collection.CreateMembershipExpressionAttr().Set(Sdf.PathExpression(expression))
    else:
        includes = [Sdf.Path(p) for p in node.get_param("include_pattern").resolve_paths()]
        excludes = [Sdf.Path(p) for p in node.get_param("exclude_pattern").resolve_paths()]
        collection.CreateIncludesRel().SetTargets(includes)
        collection.CreateExcludesRel().SetTargets(excludes)

    rule = _EXPANSION_RULES.get(node.get_param("expansion_rule").value, Usd.Tokens.expandPrims)
    collection.CreateExpansionRuleAttr().Set(rule)

    node.get_param("reference").value = f"%{name}"


def main():
    chunk_api = chunk.Chunk("Collection")
    chunk_api.add_params([
        chunk.LineEdit(
            "collection_name", "Name",
            default_value="collection1", flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.LineEdit(
            "owner_path", "Owner Prim",
            default_value="/collections", flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.LineEdit(
            "include_pattern", "Include",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        ),
        chunk.LineEdit(
            "exclude_pattern", "Exclude",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        ),
        chunk.LineEdit(
            "membership_expression", "Membership Expression",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.Menu(
            "expansion_rule", "Expansion Rule",
            default_value="Expand Prims", menu_items=list(_EXPANSION_RULES.keys()),
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.LineEdit(
            "reference", "Reference",
            flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
        ),
    ])
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="grid",
        description=(
            """
        Defines a USD collection from include/exclude paths or globs, for other chunks to target.

        Authors a named UsdCollectionAPI on Owner Prim from an Include/Exclude prim
        pattern (comma-separated paths or Houdini-style globs: *, **, ?).
        Reference outputs '%Name' — paste that token into any other chunk's prim-pattern
        field (e.g. Transform's Prim Path, Bind Material's Prim Path) to target every
        member of this collection.
        Expansion Rule controls whether included prims' descendants are implicitly
        members (Expand Prims / Expand Prims and Properties) or membership is limited
        to exactly what's listed (Explicit).
        Membership Expression authors a native USD SdfPathExpression instead — set
        operations (+ union, - difference, & intersection), recursive wildcards (//),
        and references to other collections (%:otherName). When set, it replaces
        Include/Exclude entirely rather than combining with them, since USD unions
        expression membership with rel-target membership rather than ANDing them.
        """
        ),
    )
