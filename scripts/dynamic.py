from usd_chunks import chunk, chunk_notice
from typing import List
from pxr import Usd

import usd_chunks.chunk_core

def build(myChunk: chunk.Chunk, signal, sender):
    for grp in myChunk.get_my_params():
        grp.request_delete()

    prims: List[Usd.Prim] = signal.data
    if not prims:
        return

    for prim in prims:
        prim_grp = chunk.Group(prim.GetName())
        myChunk.add_param(prim_grp)

        attributes = prim.GetAttributes()
        groups = {}
        for attr in attributes:
            if not (param := chunk.param_from_attr(attr)):
                continue

            if not isinstance(param, chunk.ChunkGroupBase):
                param.set_flag(usd_chunks.chunk_core.ChunkFlags.ParamFlags.LABELED)

            ns = attr.GetNamespace()
            tgt_grp = prim_grp if not ns else groups.get(ns)

            if not tgt_grp:
                tgt_grp = chunk.Group(
                    ns,
                    flags=usd_chunks.chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE
                    | usd_chunks.chunk_core.ChunkFlags.GroupFlags.LABELED,
                )
                prim_grp.add_param(tgt_grp)
                groups[ns] = tgt_grp

            if param:
                tgt_grp.add_param(param)

        relationships = prim.GetRelationships()
        # Do the same for relationships
        for rel in relationships:
            param = chunk.param_from_rel(rel)
            if not param:
                continue

            if not isinstance(param, chunk.ChunkGroupBase):
                param.set_flag(usd_chunks.chunk_core.ChunkFlags.ParamFlags.LABELED)

            ns = rel.GetNamespace()
            tgt_grp = prim_grp if not ns else groups.get(ns)

            if not tgt_grp:
                tgt_grp = chunk.Group(
                    ns,
                    flags=usd_chunks.chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE
                    | usd_chunks.chunk_core.ChunkFlags.GroupFlags.LABELED,
                )
                prim_grp.add_param(tgt_grp)
                groups[ns] = tgt_grp

            if param:
                tgt_grp.add_param(param)


def main():
    """
    Build a dynamic chunk that updates based on the currently selected prims. All suported prim
    attributes will be created as chunk parameters, and connected to their UsdAttributes respectively
    """
    myChunk = chunk.Chunk("Dynamic")
    myChunk.add_callback(build, listen_globally=True, notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED)

    return myChunk


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.PROPERTIES)
