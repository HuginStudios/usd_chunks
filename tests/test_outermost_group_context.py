"""A saved network's outermost chunkgroup is a context the frontends can look up.

A loaded file's outermost chunkgroup is the scene's root, so it is not a node in any graph
and get_chunks() leaves it out. Blender (and Flet, ImGui) found the shown context's chunk
in a dict built from get_chunks(), got None for it, and None means "the top level, every
chunk": opening such a file wired the default view to a chunk inside a nested group, and
only that group cooked.
"""
from __future__ import annotations

import textwrap

import pytest
from pxr import Usd

from usd_chunks import chunk_stage
from usd_chunks.chunk_script import Scene
from usd_chunks.graph import engine as chunk_graph

#: As saved with no View: a mesh, a nested group holding a transform, then a chunk after it.
NETWORK = textwrap.dedent("""\
    #usda 1.0

    def Scope "chunks"
    {
        def Chunk "Outer"
        {
            uniform token info:chunkType = "chunkgroup"
            uniform token outputs:chunk

            def Chunk "Ball"
            {
                uniform token info:chunkType = "mesh"
                string inputs:mesh_type = "sphere"
                string inputs:prim_path = "/ball"
                uniform token outputs:chunk
            }

            def Chunk "Inner"
            {
                uniform token info:chunkType = "chunkgroup"
                token inputs:chunk.connect = </chunks/Outer/Ball.outputs:chunk>
                uniform token outputs:chunk

                def Chunk "Move"
                {
                    uniform token info:chunkType = "transform"
                    string inputs:primpath_location = "/ball"
                    uniform token outputs:chunk
                }
            }

            def Chunk "Last"
            {
                uniform token info:chunkType = "transform"
                token inputs:chunk.connect = </chunks/Outer/Inner.outputs:chunk>
                string inputs:primpath_location = "/ball"
                uniform token outputs:chunk
            }
        }
    }
""")


@pytest.fixture
def opened(tmp_path):
    path = tmp_path / "network.usda"
    path.write_text(NETWORK)
    stage = Usd.Stage.Open(str(path))
    scene = Scene(stage, "pytest")
    yield stage, scene
    scene.close()


def test_the_outermost_group_is_found_by_its_path(opened):
    stage, scene = opened
    context = chunk_graph.default_context(stage)

    found = scene.get_context_for_prim_path(context)

    assert found is not None
    assert chunk_stage.get_chunk_prim_path(found) == "/chunks/Outer"


def test_opening_wires_the_default_output_to_the_end_of_the_outer_chain(opened):
    stage, scene = opened
    context = chunk_graph.default_context(stage)

    chunk_graph.default_view_slot(stage, scene, context, scene.get_context_for_prim_path(context))

    outputs = [c for c in scene.get_chunks() if chunk_stage.is_output_chunk(c)]
    wired = [str(t.GetPrimPath()) for o in outputs
             for t in chunk_stage.exec_input_connections(stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(o)))]
    assert wired == ["/chunks/Outer/Last"], "not Move, inside the nested group"
