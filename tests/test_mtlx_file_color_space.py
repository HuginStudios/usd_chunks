"""A texture node says which colour space its file is read in: File Colour Space.

Nothing set it from a chunk, so a data map (normal, roughness) was read as colour, or a
library material rebuilt from MaterialX node chunks needed a conversion node in place of
the colour space its file had.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdShade

from usd_chunks import Scene, shader_defs

pytestmark = pytest.mark.skipif(
    "mtlx" not in shader_defs.available_source_types(),
    reason="this USD has no MaterialX shader definitions (no usdMtlx plugin)",
)


def _file_attr(stage, path):
    return UsdShade.Shader(stage.GetPrimAtPath(path)).GetInput("file").GetAttr()


def test_an_image_nodes_file_is_read_in_the_colour_space_given(tmp_path):
    texture = tmp_path / "wood.png"
    texture.write_bytes(b"")                     # the file param wants a file that exists; its content isn't read
    with Scene(Usd.Stage.CreateInMemory(), "pytest") as scene:
        node = scene.create.mtlx_shader(nodedef="ND_image_color3", prim_path="/M/ng/tex", file=str(texture),
                                        file_color_space="srgb_texture")
        attr = _file_attr(scene.get_stage(), "/M/ng/tex")
        assert attr.GetColorSpace() == "srgb_texture"
        scene.set(node, file_color_space="raw")
        assert _file_attr(scene.get_stage(), "/M/ng/tex").GetColorSpace() == "raw"
        scene.set(node, file_color_space="")
        assert not _file_attr(scene.get_stage(), "/M/ng/tex").HasColorSpace()   # back to the file's default


def test_only_a_node_that_reads_a_file_shows_the_setting():
    from usd_chunks.chunk_flags import ChunkFlags
    with Scene(Usd.Stage.CreateInMemory(), "pytest") as scene:
        image = scene.create.mtlx_shader(nodedef="ND_image_color3", prim_path="/M/ng/tex")
        cc = scene.create.mtlx_shader(image, nodedef="ND_colorcorrect_color3", prim_path="/M/ng/cc")
        hidden = ChunkFlags.State.HIDDEN
        assert not image.get_param("file_color_space").has_state(hidden)
        assert cc.get_param("file_color_space").has_state(hidden)
