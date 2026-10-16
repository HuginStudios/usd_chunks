"""A chunk wired through a placeholder must stay downstream of the chain behind it.

A chunk prim whose type no module registers (renamed, missing, off USD_CHUNK_DIRS) loads
as a placeholder: a graph node with no chunk behind it. The edge into it resolved to
nothing and was dropped, so whatever was wired after it came out with no dependency at all
and the load cooked it as a root -- first, before the chain it is wired to had authored
anything.

Live-reported: house_variation_wedge.usda still named `modelling.add_mesh` after the
rename to `modelling.mesh`. Its Scatter Points, wired after the missing Add Mesh, cooked
before the houses it instances existed and scattered default cubes. A second cook the
readout Label used to trigger hid it until Label became presentation_only.
"""
from __future__ import annotations

from pxr import Sdf, Usd

from chunk_stubs import register_stub_module
from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import ChunkStageSync
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

_SCENE = """#usda 1.0

def Scope "chunks"
{{
    def Chunk "ChunkGroup"
    {{
        uniform token info:chunkType = "chunkgroup"

        def Chunk "Up"
        {{
            uniform token info:chunkType = "{up}"
            uniform token outputs:chunk
        }}

        def Chunk "Missing"
        {{
            uniform token info:chunkType = "no_module_registers_this"
            token inputs:chunk.connect = </chunks/ChunkGroup/Up.outputs:chunk>
            uniform token outputs:chunk
        }}

        def Chunk "Down"
        {{
            uniform token info:chunkType = "{down}"
            token inputs:chunk.connect = </chunks/ChunkGroup/Missing.outputs:chunk>
            uniform token outputs:chunk
        }}
    }}
}}
"""


def test_chunk_after_a_placeholder_cooks_after_its_upstream(qapp, tmp_path) -> None:
    cooked: list[str] = []
    up = register_stub_module(execute=lambda node: cooked.append("Up"))
    down = register_stub_module(execute=lambda node: cooked.append("Down"))

    path = tmp_path / "scene.usda"
    path.write_text(_SCENE.format(up=up.module_name, down=down.module_name), encoding="utf-8")
    stage = Usd.Stage.Open(str(path))

    class _Scene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _Scene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    qapp.processEvents()

    assert stage.GetPrimAtPath(Sdf.Path("/chunks/ChunkGroup/Missing")), "setup: the placeholder prim"
    by_name = {c.name: c for c in scene.get_chunks()}
    assert "Missing" not in by_name, "setup: the missing type must load without a chunk"

    assert [d.name for _, d in by_name["Down"].get_dependencies()] == ["Up"], (
        "Down is wired through the placeholder to Up and must depend on it"
    )
    assert "Down" in cooked, "Down must still cook on load"
    assert cooked.index("Up") < cooked.index("Down"), (
        f"a wired chunk must never cook as a root ahead of its upstream: {cooked}"
    )


def test_opening_a_file_never_wires_around_a_placeholder(qapp, tmp_path) -> None:
    """The edge through the placeholder orders the cook; it is not a wire in the file.

    usdview hands its stage over after the plugin is up, so every File > Open loads
    through reload_for_stage. There the edge was authored as Up -> Down beside the saved
    Missing -> Down, and the next save kept a wire nobody made.
    """
    stages: list[Usd.Stage] = []

    class _Scene(SceneSender):
        def get_stage(self):
            return stages[-1]

        def get_selected_prims(self):
            return []

    scene = _Scene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    up = register_stub_module()
    down = register_stub_module()
    path = tmp_path / "scene.usda"
    path.write_text(_SCENE.format(up=up.module_name, down=down.module_name), encoding="utf-8")
    stages.append(Usd.Stage.Open(str(path)))
    chunk_registry.ChunkLayerManager().initialize(stages[0])
    _chunk_usd_init(stages[0], scene)
    stages.append(Usd.Stage.Open(str(path)))
    ChunkStageSync().reload_for_stage(stages[-1])
    qapp.processEvents()

    stage = stages[-1]
    wires = stage.GetPrimAtPath("/chunks/ChunkGroup/Down").GetAttribute("inputs:chunk").GetConnections()
    assert wires == [Sdf.Path("/chunks/ChunkGroup/Missing.outputs:chunk")], "opening must not rewire Down"
    assert not stage.GetRootLayer().dirty, "opening a file must not rewrite it"
    by_name = {c.name: c for c in scene.get_chunks()}
    assert [d.name for _, d in by_name["Down"].get_dependencies()] == ["Up"], "the cook order still holds"
