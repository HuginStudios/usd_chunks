"""Render Pass chunk -- creates a UsdRender.Pass prim: one step of a render, pointing at
the settings that describe it.

A pass is the layer above settings. Where a RenderSettings prim says how to render, a
pass says which rendering this is and what it depends on, so a shot can describe a
beauty pass and a matte pass over the same scene and order them.

Worth having beyond that: a delegate may support passes without supporting render
settings prims directly, so driving a render through its pass can be the route that
reaches the renderer -- hdVRay, for instance, ships a render-pass scene index plugin
and no render-settings filtering one.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import logging

from pxr import Sdf, UsdRender

from usd_chunks import chunk, chunk_utils

# Parented under "usd_chunks" so this module's messages reach the package handler and
# its INFO level; a chunk module imported under its own file name otherwise logs from
# outside the package tree, where only WARNING and above escape, unformatted, through
# logging's lastResort. See the note in render.py.
logger = logging.getLogger(f"usd_chunks.{__name__}")


def main():
    chunk_api = chunk.Chunk("Render Pass")

    # A literal, not get_unique_path(stage, ...). A default computed from whatever the stage
    # holds at construction is a value wearing a default's clothes: the save path leaves
    # defaults out on the grounds that running main() again reproduces them, and running
    # main() again does not reproduce this one -- on reload no prim exists yet, so every
    # instance is handed the same path back. execute() does the uniquifying and records the
    # result as a value, which is what survives a save.
    prim_path = "/Render/Passes/beauty"

    chunk_api.add_params([
        chunk.LineEdit(
            "primpath_location", "Prim Path",
            default_value=prim_path,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.LineEdit("pass_type", "Pass Type", default_value="render"),
        chunk.LineEdit(
            "render_source", "Render Source",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
        ),
        chunk.LineEdit("input_passes", "Input Passes"),
        chunk.LineEdit("command", "Command"),
        chunk.LineEdit("file_name", "File Name"),
    ])
    return chunk_api


def execute(node):
    stage = node.get_stage()

    raw_path = node.get_param("primpath_location").value
    if not raw_path:
        return

    primpath_param = node.get_param("primpath_location")
    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param, unique=True):
        render_pass = UsdRender.Pass.Define(stage, Sdf.Path(target_path))
        if not render_pass:
            return

        if pass_type := node.get_param("pass_type").value.strip():
            render_pass.CreatePassTypeAttr().Set(pass_type)

        # One source: renderSource names the settings this pass renders with, and a
        # pass renders one thing. Resolved rather than taken verbatim, like every other
        # path param here, so a multi-selection or a relative path cannot raise.
        if source := chunk_utils.target_paths(node.get_param("render_source"))[:1]:
            render_pass.CreateRenderSourceRel().SetTargets(source)

        if inputs := chunk_utils.target_paths(node.get_param("input_passes")):
            render_pass.CreateInputPassesRel().SetTargets(inputs)

        if command := node.get_param("command").value.strip():
            render_pass.CreateCommandAttr().Set(command.split())

        if file_name := node.get_param("file_name").value.strip():
            render_pass.CreateFileNameAttr().Set(file_name)


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_RENDER,
        description="""
        Render Pass chunk — creates a UsdRender.Pass prim, one step of a render
        pointing at the settings that describe it.

        A pass sits above settings. A RenderSettings prim says how to render; a
        pass says which rendering this is, what it depends on, and optionally how
        it is invoked — so a shot can describe a beauty pass and a matte pass over
        one scene and order them.

        Parameters:
          - Prim Path: stage path for the RenderPass prim. Auto-initialised to a
            unique /Render/Passes/beauty path.
          - Pass Type: what kind of pass this is, e.g. "render" for one a renderer
            executes. Free text, since the set is open and host-specific.
          - Render Source: the RenderSettings prim this pass renders with, named
            by prim path. One source only — a pass renders one thing.
          - Input Passes: passes that must run before this one, as paths or glob
            patterns. This is what orders a multi-pass render.
          - Command: the argv a host would run for this pass, split on whitespace.
            Only meaningful to a host that executes passes itself.
          - File Name: a layer this pass renders instead of the current stage.

        Beyond describing the render, a pass can be the route that reaches the
        renderer at all: a delegate may implement render passes without
        implementing render settings prims. hdVRay ships a render-pass scene index
        plugin and no render-settings filtering one, so pointing the Render chunk
        at a pass is worth trying where the settings prim alone does not take.
        """,
    )
