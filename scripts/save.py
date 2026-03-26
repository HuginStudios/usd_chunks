from usd_chunks import chunk
import os


def save_stage(owner: chunk.Chunk, notice, sender):
    try:
        dst_param = owner.get_param("destination", chunk.is_param_t(str))
    except ValueError:
        return

    _, ext = os.path.splitext(dst_param.value)
    if ext not in [".usd", ".usda", ".usdc"]:
        return

    if stage := owner.get_stage():
        print("Save file:", dst_param.value)
        stage.Export(dst_param.value)


def main():

    myChunk = chunk.Chunk("Save")

    dest = chunk.FileBrowser("destination")
    myChunk.add_param(dest)

    btn = chunk.Button("execute")
    myChunk.add_param(btn)
    myChunk.add_callback(save_stage)

    return myChunk


def config():
    return chunk.DefaulConfig(chunk.StandardCategory.UTILS, "Save the stage to a file")
