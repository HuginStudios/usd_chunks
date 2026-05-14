from usd_chunks import chunk, chunk_notice, chunk_core


def build(myChunk: chunk.Chunk, notice: chunk_notice.ParamChangeNotice, sender):
    dynGrp = myChunk.get_param("dyn_grp", chunk.is_group)
    ppParm = myChunk.get_param("prim", chunk.is_inst(chunk.LineEdit))

    prims = ppParm.get_prims()

    if not prims:
        return

    for grp in dynGrp.get_my_params():
        grp.request_delete()

    for prim in prims:
        prim_grp = chunk.Group(prim.GetName())
        dynGrp.add_param(prim_grp)

        attributes = prim.GetAttributes()
        groups = {}
        for attr in attributes:
            if not (param := chunk.param_from_attr(attr)):
                continue

            if not isinstance(param, chunk.ChunkGroupBase):
                param.set_flag(chunk_core.ChunkFlags.ParamFlags.LABELED)

            ns = attr.GetNamespace()
            tgt_grp = prim_grp if not ns else groups.get(ns)

            if not tgt_grp:
                tgt_grp = chunk.Group(
                    ns,
                    flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
                )
                prim_grp.add_param(tgt_grp)
                groups[ns] = tgt_grp

            if param:
                tgt_grp.add_param(param)

        relationships = prim.GetRelationships()
        # Do the same for relationships
        for rel in relationships:
            if not (param := chunk.param_from_rel(rel)):
                continue

            if not isinstance(param, chunk.ChunkGroupBase):
                param.set_flag(chunk_core.ChunkFlags.ParamFlags.LABELED)

            ns = rel.GetNamespace()
            tgt_grp = prim_grp if not ns else groups.get(ns)

            if not tgt_grp:
                tgt_grp = chunk.Group(
                    ns,
                    flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
                )
                prim_grp.add_param(tgt_grp)
                groups[ns] = tgt_grp

            if param and tgt_grp:
                tgt_grp.add_param(param)


def main():
    """
    Build a dynamic chunk that updates based on the currently selected prims. All suported prim
    attributes will be created as chunk parameters, and connected to their UsdAttributes respectively
    """

    myChunk = chunk.Chunk("Dynamic")

    dynGroup = chunk.Group("dyn_grp", flags=chunk.ChunkFlags.GroupFlags.FRAMED)
    primSel = chunk.LineEdit(
        "prim", flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION
    )
    myChunk.add_param(primSel)
    myChunk.add_param(dynGroup)
    myChunk.add_callback(build, sender=primSel, send_on_create=True)

    return myChunk


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.PROPERTIES)
