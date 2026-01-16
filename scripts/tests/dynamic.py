from usd_chunks import chunk, chunk_notice
from typing import List
from pxr import Usd


def build(myChunk: chunk.Chunk, signal, sender):
    for grp in myChunk.get_my_params():
        grp.request_delete()

    prims: List[Usd.Prim] = signal.data
    if not prims:
        return

    for prim in prims:
        prim_grp = chunk.Group(prim.GetName())

        attributes = prim.GetAttributes()

        groups = {}

        for attr in attributes:
            param = chunk.param_from_attr(attr)
            if not param:
                continue

            if not isinstance(param, chunk.ChunkGroupBase):
                param.set_flag(chunk.ChunkFlags.ParamFlags.LABELED)

            ns = attr.GetNamespace()
            tgt_grp = prim_grp if not ns else groups.get(ns)

            if not tgt_grp:
                tgt_grp = chunk.Group(
                    ns, flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED
                )
                prim_grp.add_param(tgt_grp)
                groups[ns] = tgt_grp

            if param:
                tgt_grp.add_param(param)

        myChunk.add_param(prim_grp)


def main():
    myChunk = chunk.Chunk("Dynamic", category="Attributes")
    myChunk.add_callback(build, listen_globally=True, notice_alias=chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED)

    return myChunk
