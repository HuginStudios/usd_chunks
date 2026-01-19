from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_notice import ChunkNoticeAlias

from pxr import Usd, Tf

def apply(owner: chunk.Chunk, notice, sender):
    menu = owner.get_param("schemas")
    if not menu:
        return
    
    schema_str = menu.value
    if not schema_str:
        return
    
    prims = chunk_registry.ChunkRegistry().scene.get_selected_prims()
    if not prims:
        return
    
    schema_reg = Usd.SchemaRegistry()
    schema = schema_reg.GetTypeFromName(schema_str)

    if stage := owner.get_stage():
        with Usd.EditContext(stage, owner.get_edit_context()):
            for prim in prims:
                prim.ApplyAPI(schema)

def update(owner: chunk.Chunk, notice, sender):
    prims = notice.data
    if not prims:
        return

    prim: Usd.Prim = prims[0]
    stage = owner.get_stage()
    if not stage:
        return

    param = owner.get_param("schemas")
    if not param:
        return

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
    btn = chunk.Button("apply", "Apply")

    myChunk = chunk.Chunk("Apply Schema", category="utility")
    myChunk.add_params([menu, btn])

    myChunk.add_callback(update, listen_globally=True, notice_alias=ChunkNoticeAlias.PRIM_SELECTION_CHANGED)
    myChunk.add_callback(apply, sender=btn)

    return myChunk
