from __future__ import annotations
import logging
from usd_chunks import chunk
from pxr import Sdf, Tf

logger = logging.getLogger(__name__)


def execute(node):
    """Import the text into the chunk's layer, on every cook.

    A cook clears the layer first, and a host may hand the chunk a new layer for each one
    (Houdini does, per node), so the text is imported here rather than only when it changes.
    """
    layer = node.get_edit_context()
    if not layer:
        return
    # Parsed as .usda, then copied in: ImportFromString keeps the layer's own format, and an
    # anonymous layer is not .usda everywhere (Houdini's are "sdf", which refuses "#usda").
    text = Sdf.Layer.CreateAnonymous(".usda")
    try:
        text.ImportFromString(str(node.get_param("code").value))
    except Tf.ErrorException as error:
        # Said, not swallowed: a typo in the text otherwise cooks to nothing at all, and
        # whatever relied on its prims quietly falls back (a Scatter to its default cube).
        logger.warning("%s: the .usda text does not parse, so nothing was imported: %s", node.name, error)
        return
    layer.TransferContent(text)


def main():
    myChunk = chunk.Chunk("inline_chunk")

    default = (
        "#usda 1.0\n\ndef Sphere 'sphere' {\n\tdouble radius = 3\n\tcolor3f[] primvars:displayColor=[(0,0.5,0)]\n}"
    )

    text = chunk.TextEdit("code", default_value=default)
    text.set_flag(chunk.ChunkFlags.ParamFlags.USD_SYNTAX_HIGHLIGHT)
    myChunk.add_param(text)

    return myChunk


def config():
    return chunk.DefaultConfig(
        chunk.StandardCategory.USD,
        icon="code",
        description="Write .usda code directly in to the text param. The resulting layer overrides the chunk layer",
    )
