from usd_chunks import chunk
import os

def save_stage(owner: chunk.Chunk, notice, sender):

    dest_param = owner.get_param("destination")
    if not dest_param:
        return
    
    dest = dest_param.value
    
    _, ext = os.path.splitext(dest)
    if ext not in [".usd", ".usda", ".usdc"]:
        return

    if stage := owner.get_stage():
        print("Save file:", dest)
        stage.Export(dest)

def main():

    myChunk = chunk.Chunk("Save")
    
    dest = chunk.FileBrowser("destination")
    myChunk.add_param(dest)

    btn = chunk.Button("execute")
    myChunk.add_param(btn)

    myChunk.add_callback(save_stage, notice_alias=chunk.ChunkNoticeAlias.CHUNK_EXECUTED)
    return myChunk

def config():
    return chunk.DefaulConfig(chunk.StandardCategory.UTILS, "Save the stage to a file")