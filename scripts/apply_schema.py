from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_notice import ChunkNoticeAlias

from pxr import Usd, Tf


def apply(owner: chunk.Chunk, notice, sender):
    menu = owner.get_param("schemas", chunk.is_param_t(str))

    prims = chunk_registry.ChunkRegistry().scene.get_selected_prims()
    if not prims:
        return

    schema_reg = Usd.SchemaRegistry()
    schema = schema_reg.GetTypeFromName(menu.value)
    if not schema:
        return

    for prim in prims:
        prim.ApplyAPI(schema)


def update(owner: chunk.Chunk, notice, sender: chunk.LineEdit):
    prims = sender.get_prims()

    prim: Usd.Prim = prims[0]
    stage = owner.get_stage()
    if not stage:
        return

    param = owner.get_param("schemas", chunk.is_inst(chunk.Menu))

    if not prim or prim == stage.GetPseudoRoot():
        param.menu_items = []
        return

    schema_reg = Usd.SchemaRegistry()
    schema_list = []
    for t in Tf.Type.FindByName("UsdAPISchemaBase").GetAllDerivedTypes():
        if not (schema_reg.IsAppliedAPISchema(t) or schema_reg.IsMultipleApplyAPISchema(t)):
            continue

        if schema_reg.IsAppliedAPISchema(t) and not schema_reg.IsMultipleApplyAPISchema(t):
            if prim.CanApplyAPI(t):
                schema_list.append(t.typeName)

    param.menu_items = schema_list


def main():
    menu = chunk.Menu("schemas", "Schemas")
    primSel = chunk.LineEdit(
        "prim", flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION
    )

    btn = chunk.Button("apply", "Apply")

    myChunk = chunk.Chunk("Apply Schema")
    myChunk.add_params([primSel, menu, btn])

    myChunk.add_callback(update, sender=primSel, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)
    myChunk.add_callback(apply, sender=btn)

    return myChunk


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.UTILS)
