from usd_chunks import chunk
from pxr import Sdf, Tf


def text_changed(owner: chunk.TextEdit, notice, sender):
    print("HELOOOO")
    layer = owner.get_edit_context()
    if not layer:
        return
    layer.Clear()
    try:
        layer.ImportFromString(str(owner.value))
    except Tf.ErrorException:
        pass


def main():
    myChunk = chunk.Chunk("inline_chunk")

    default = (
        "#usda 1.0\n\ndef Sphere 'sphere' {\n\tdouble radius = 3\n\tcolor3f[] primvars:displayColor=[(0,0.5,0)]\n}"
    )

    text = chunk.TextEdit("code", default_value=default)
    text.set_flag(chunk.ChunkFlags.ParamFlags.USD_SYNTAX_HIGHLIGHT)
    text.add_callback(text_changed, send_on_create=True)

    myChunk.add_param(text)

    return myChunk


def config():
    return chunk.DefaulConfig(chunk.StandardCategory.UTILS, description="Write .usda code directly in to the text param. The resulting layer overrides the chunk layer")
