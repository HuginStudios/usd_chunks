"""Every chunk's description opens with a summary line: what `usdchunks list` shows.

`usdchunks list` and the create menu's search show a description's first line only, cut at
90 characters, and that line is what a person or an agent picks a chunk by. Given only that
list, an agent asked for a spiral staircase answered "no chunk does this": Duplicate Prim's
line said "deep copies a prim to a new path". With "Copies a prim N times: row, ring, grid,
spiral stairs; ..." it picked it every time.

The rule: a description opens with one line of at most 90 characters, then a blank line (or
nothing more). LEGACY lists the chunks written before the rule; it only shrinks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from usd_chunks import Scene, chunk_registry

CHUNKS = Path(__file__).resolve().parents[1] / "chunks"
LIMIT = 90

#: Chunks from before the rule. Fix one, then take it off this list (the test says when).
LEGACY = {
    "core/dot.py",
    "core/export_chunkgroup.py",
    "core/export_usd.py",
    "core/merge.py",
    "core/output.py",
    "core/switch.py",
    "core/wedge.py",
    "datasets/kitti_writer.py",
    "dynamics/emit.py",
    "dynamics/fracture.py",
    "dynamics/particle_solver.py",
    "dynamics/physics_drive.py",
    "dynamics/rbd_solver.py",
    "dynamics/surface_flow.py",
    "examples/add_cube.py",
    "examples/closure.py",
    "examples/custom_class.py",
    "examples/custom_widget.py",
    "examples/example.py",
    "examples/subclassed.py",
    "examples/toggle_vis.py",
    "materials/AMDMaterialXLibrary.py",
    "materials/mdl_to_mtlx.py",
    "materials/mtlx_shader.py",
    "materials/preview_surface.py",
    "materials/primvar_reader.py",
    "materials/uv_transform.py",
    "materials/uvchecker/uv_checker.py",
    "modelling/add_curves.py",
    "modelling/bake_textures.py",
    "modelling/camera.py",
    "modelling/curves_to_mesh.py",
    "modelling/fence.py",
    "modelling/light.py",
    "modelling/mesh.py",
    "modelling/poly_reduce.py",
    "modelling/proximity_field.py",
    "modelling/remesh_proxy.py",
    "modelling/scatter_brush.py",
    "modelling/uv_unwrap.py",
    "modelling/vine.py",
    "rigs/pose_articulation.py",
    "rigs/skel_pose.py",
    "rigs/urdf_import.py",
    "transforms/reset_transform.py",
    "transforms/transform.py",
    "usd/apply_schema.py",
    "usd/collection.py",
    "usd/edit_target.py",
    "usd/finish_frame.py",
    "usd/get_property.py",
    "usd/inherit.py",
    "usd/inline_usd.py",
    "usd/layer.py",
    "usd/merge_instancers.py",
    "usd/payload.py",
    "usd/reference.py",
    "usd/render.py",
    "usd/render_pass.py",
    "usd/render_product.py",
    "usd/render_settings.py",
    "usd/render_var.py",
    "usd/semantics.py",
    "usd/set_display_color.py",
    "usd/specialize.py",
    "usd/stage_settings.py",
    "usd/sublayer.py",
    "usd/variant.py",
    "utils/math_ops.py",
    "utils/random_value.py",
    "utils/stage_expression.py",
    "utils/stage_expression_switch.py",
    "utils/vector_math_ops.py",
}


def _summary_problem(description: str) -> str:
    lines = chunk_registry.clean_description(description or "").strip().splitlines()
    if not lines:
        return "no description"
    if len(lines[0]) > LIMIT:
        return f"first line is {len(lines[0])} characters, more than {LIMIT}"
    if len(lines) > 1 and lines[1].strip():
        return "the first line runs on: a summary line needs a blank line after it"
    return ""


@pytest.fixture(scope="module")
def modules():
    with Scene() as scene:
        found = {}
        for m in scene.registry.get_chunk_modules():
            path = Path(m.module_file).resolve()
            if CHUNKS in path.parents:
                found[path.relative_to(CHUNKS).as_posix()] = m.config.description
        yield found


def test_every_chunk_opens_with_a_summary_line(modules) -> None:
    broken = {p: why for p, d in modules.items() if (why := _summary_problem(d)) and p not in LEGACY}
    assert not broken, "\n".join(f"{p}: {why}" for p, why in sorted(broken.items()))


def test_the_legacy_list_only_shrinks(modules) -> None:
    fixed = sorted(p for p in LEGACY if p in modules and not _summary_problem(modules[p]))
    gone = sorted(p for p in LEGACY if p not in modules)
    assert not fixed, f"these follow the rule now; take them off LEGACY: {fixed}"
    assert not gone, f"these are no longer chunks; take them off LEGACY: {gone}"
