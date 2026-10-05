"""Blender objects into the stage -- what a SOP Import is to Houdini's LOPs.

A collection (or the whole scene) goes through Blender's own USD exporter, and what it writes
is copied into this chunk's layer under `primpath_location`. Copied, not referenced: the
chunk's layer is its whole result, cleared before every cook, so there is no export file
beside the network to go stale or be shipped. The cook cache keeps the result as a
`<key>.usdc` entry, as it does a House's, and always (`cache_always`): reopening the .blend
reads an unchanged import back instead of exporting it again, and there is no checkbox or
environment switch to lose that. Blender's exporter does the conversion because it is the
one that knows Blender: meshes with modifiers applied, UVs, normals, instancing, armatures,
shape keys, and materials as UsdPreviewSurface or MaterialX.

**Live.** Blender tells the add-on when its scene changes; the add-on calls this module's
`host_changed(node)`, which cooks again only when what this chunk exports actually moved
(`_fingerprint`). Editing a mesh, a modifier or a material re-imports; selecting, orbiting
or touching an object outside the collection does not.

**Shown once.** The Hydra view draws the imported copy where Blender draws the original,
so the originals are hidden in Blender's viewport (the eye) while this chunk imports them --
`host_sources` names them and the add-on hides and unhides. A hidden object is still
evaluated and exported, so editing it (Alt+H, or through the outliner) still re-imports.

**Axes.** Blender is Z-up. On a Y-up stage the exporter converts, so the imported prims
stand the way the stage expects; the stage's metersPerUnit is applied the same way.

Textures keep the paths Blender has for them. An image packed into the .blend has no path
to keep -- unpack it (File > External Data) for it to reach the stage.

Requires: Blender 5.1+ (its own pxr; never install usd-core beside it), numpy.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile

import numpy as np
from pxr import Sdf, UsdGeom

from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags

try:
    import bpy
except ImportError:  # pragma: no cover - config() keeps this module out of other hosts
    bpy = None

logger = logging.getLogger(__name__)

#: The collection menu's entry for "everything in the scene".
WHOLE_SCENE = "Scene Collection"

#: Where Blender's exporter puts everything; copied from here to primpath_location.
_EXPORT_ROOT = "/root"

#: usd_chunks' own camera mirror (frontends/blender/addon/camera.py): a view of the stage,
#: not Blender content, so never imported back into it.
_MIRROR_NAME = "USD Camera"

#: The cook's record of what it exported, so host_changed can tell a real change.
_COOKED_FROM = "_blender_import_fingerprint"

#: Mesh attribute data types -> (foreach_get key, width, dtype) for the fingerprint.
_ATTRIBUTE_READS = {
    "FLOAT": ("value", 1, np.float32),
    "INT": ("value", 1, np.int32),
    "INT8": ("value", 1, np.int32),
    "BOOLEAN": ("value", 1, np.bool_),
    "FLOAT2": ("vector", 2, np.float32),
    "INT32_2D": ("value", 2, np.int32),
    "FLOAT_VECTOR": ("vector", 3, np.float32),
    "FLOAT_COLOR": ("color", 4, np.float32),
    "BYTE_COLOR": ("color", 4, np.float32),
    "QUATERNION": ("value", 4, np.float32),
    "FLOAT4X4": ("value", 16, np.float32),
}


# ---------------------------------------------------------------------------
# What is exported
# ---------------------------------------------------------------------------

def _collection(node):
    """The bpy collection the chunk exports, None for the whole scene, False if missing."""
    name = node.get_param("collection").value or WHOLE_SCENE
    if name == WHOLE_SCENE:
        return None
    return bpy.data.collections.get(name) or False


def _objects(node):
    """The objects the export takes, or None when the named collection is gone."""
    collection = _collection(node)
    if collection is False:
        return None
    scene = bpy.context.scene
    objects = (scene.objects if scene else []) if collection is None else collection.all_objects
    return [obj for obj in objects if obj.name != _MIRROR_NAME]


def _skipped_props() -> frozenset:
    """Properties that change without the content changing.

    Everything every ID has -- session_uid differs per session, users and tag change as
    Blender works -- and the show_* panel flags, so expanding a modifier is not an edit.
    """
    return frozenset({"rna_type"} | {p.identifier for p in bpy.types.ID.bl_rna.properties})


def _rna_values(struct) -> str:
    """Every plain (non-pointer, non-collection) property of an RNA struct, as text.

    Generic on purpose: a light's power, a camera's lens, a modifier's levels and a
    material's settings all count, with no list of names to keep up with Blender. Must be
    the same text in every session, or a reopened .blend never finds its cache entry.
    """
    skipped = _skipped_props()
    values = []
    for prop in struct.bl_rna.properties:
        if (prop.identifier in skipped or prop.identifier.startswith("show_")
                or prop.type in {"POINTER", "COLLECTION"}):
            continue
        try:
            value = getattr(struct, prop.identifier)
        except Exception:
            continue
        if getattr(prop, "is_array", False):
            try:
                value = tuple(value)
            except TypeError:
                pass
        values.append(f"{prop.identifier}={value!r}")
    return ";".join(values)


def _hash_mesh(digest, mesh) -> None:
    """Every attribute of an evaluated mesh: positions, topology, UVs, colours, creases."""
    for attribute in mesh.attributes:
        read = _ATTRIBUTE_READS.get(attribute.data_type)
        digest.update(f"{attribute.name}:{attribute.domain}:{attribute.data_type}:"
                      f"{len(attribute.data)}".encode())
        if read is None:
            continue
        key, width, dtype = read
        values = np.empty(len(attribute.data) * width, dtype=dtype)
        try:
            attribute.data.foreach_get(key, values)
        except Exception:
            continue
        digest.update(values.tobytes())


def _hash_material(digest, material) -> None:
    digest.update(_rna_values(material).encode())
    tree = material.node_tree
    if tree is None:
        return
    for shader in tree.nodes:
        digest.update(f"{shader.name}:{shader.bl_idname}".encode())
        image = getattr(shader, "image", None)
        if image is not None:
            digest.update(image.filepath.encode())
        for socket in shader.inputs:
            value = getattr(socket, "default_value", None)
            try:
                value = tuple(value)
            except TypeError:
                pass
            digest.update(f"{socket.identifier}={value!r}".encode())
    for link in tree.links:
        digest.update(f"{link.from_node.name}.{link.from_socket.identifier}->"
                      f"{link.to_node.name}.{link.to_socket.identifier}".encode())


def _fingerprint(node):
    """A digest of everything the export reads, or None when it cannot be taken.

    Evaluated data, so a modifier's result counts, not just the base mesh. A deliberate
    over-approximation: a property that does not reach the USD still re-imports, which
    costs a cook; missing one that does would leave the stage silently stale.
    """
    if bpy is None or bpy.context.scene is None:
        return None
    objects = _objects(node)
    if objects is None:
        return None
    digest = hashlib.blake2b(digest_size=16)
    scene = bpy.context.scene
    digest.update(f"{scene.frame_current}:{scene.frame_start}:{scene.frame_end}".encode())
    depsgraph = bpy.context.evaluated_depsgraph_get()
    materials = {}
    for obj in sorted(objects, key=lambda o: o.name):
        evaluated = obj.evaluated_get(depsgraph)
        digest.update(f"{obj.name}:{obj.type}:{obj.parent.name if obj.parent else ''}:"
                      f"{obj.hide_render}:{obj.hide_viewport}".encode())
        digest.update(np.array(evaluated.matrix_world, dtype=np.float64).tobytes())
        for modifier in obj.modifiers:
            digest.update(_rna_values(modifier).encode())
        data = evaluated.data
        if data is None:
            continue
        if obj.type == "MESH":
            _hash_mesh(digest, data)
        else:
            digest.update(_rna_values(data).encode())
        for slot in obj.material_slots:
            if slot.material is not None:
                materials[slot.material.name] = slot.material
    for name in sorted(materials):
        _hash_material(digest, materials[name])
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def cache_key(node):
    """The Blender data the export reads -- none of it is a param, so the cache cannot see it.

    Also recorded as what the chunk's result was made from: the cache asks this before
    every cook, and a result read back from it never reaches execute() to record it there,
    which would leave host_changed re-reading the same entry on every edit in Blender.
    """
    fingerprint = _fingerprint(node)
    setattr(node, _COOKED_FROM, fingerprint)
    return fingerprint


def menu_items(node, param_name):
    """The scene's collections, asked for as the menu opens (chunk.Menu.refresh_items)."""
    if param_name != "collection" or bpy is None:
        return None
    return [WHOLE_SCENE] + sorted(c.name for c in bpy.data.collections)


def host_sources(node):
    """The Blender objects this chunk stands in for; the add-on hides them in the viewport.

    None while the chunk's result is not on the stage -- disabled, or outside the active
    view -- since hiding the original then would show nothing at all.
    """
    if bpy is None or not node.get_param("hide_source").value:
        return []
    if node.is_blocked() or node.has_state(chunk.ChunkFlags.State.DISABLED):
        return []
    return [obj.name for obj in _objects(node) or []]


def host_changed(node):
    """Blender's scene changed (the add-on calls this, debounced). Re-import if it matters."""
    if not node.get_param("live").value:
        return
    fingerprint = _fingerprint(node)
    if fingerprint is not None and fingerprint != getattr(node, _COOKED_FROM, None):
        node.execute()


def _export_kwargs(node, stage, path):
    collection = _collection(node)
    kwargs = dict(
        filepath=path,
        check_existing=False,
        collection=collection.name if collection else "",
        root_prim_path=_EXPORT_ROOT,
        # Absolute and where they are: the export file is temporary, so neither a path
        # relative to it nor a texture copied beside it would outlive the cook.
        relative_paths=False,
        export_textures_mode="KEEP",
        # Not the World: it is not part of a collection, and Blender writes its colour as a
        # generated .exr beside the export -- in the scratch folder this deletes, leaving a
        # dome light pointing at nothing.
        convert_world_material=False,
        export_materials=bool(node.get_param("export_materials").value),
        generate_materialx_network=bool(node.get_param("materialx").value),
        export_animation=bool(node.get_param("export_animation").value),
        use_instancing=bool(node.get_param("use_instancing").value),
        evaluation_mode=(node.get_param("evaluation_mode").value or "Render").upper(),
        convert_scene_units="CUSTOM",
        meters_per_unit=UsdGeom.GetStageMetersPerUnit(stage),
    )
    if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y:
        kwargs.update(convert_orientation=True, export_global_up_selection="Y",
                      export_global_forward_selection="NEGATIVE_Z")
    return kwargs


def _run_export(kwargs) -> None:
    """wm.usd_export, with a window in context: a cook can run from a timer, which has none."""
    wm = bpy.context.window_manager
    window = wm.windows[0] if wm is not None and wm.windows else None
    if window is None:
        bpy.ops.wm.usd_export(**kwargs)
        return
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.usd_export(**kwargs)


def execute(node):
    if bpy is None:
        return
    root_path = (node.get_param("primpath_location").value or "").strip()
    if not root_path or not Sdf.Path.IsValidPathString(root_path):
        return
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    if _collection(node) is False:
        logger.warning("%s: no collection named %r", node.name, node.get_param("collection").value)
        return

    fingerprint = _fingerprint(node)
    scratch = tempfile.mkdtemp(prefix="usd_chunks_blender_import_")
    try:
        path = os.path.join(scratch, "export.usdc")
        _run_export(_export_kwargs(node, stage, path))
        exported = Sdf.Layer.FindOrOpen(path)
        if exported is None or not exported.GetPrimAtPath(_EXPORT_ROOT):
            logger.warning("%s: Blender exported nothing", node.name)
            return
        target = Sdf.Path(root_path)
        # CopySpec needs the parent; overs, so this chunk defines nothing above its target.
        if target.GetParentPath() != Sdf.Path.absoluteRootPath:
            Sdf.CreatePrimInLayer(layer, target.GetParentPath())
        # The whole root in one copy, so bindings and connections that point inside it
        # (a mesh's material:binding -> /root/_materials/...) are remapped to the target.
        Sdf.CopySpec(exported, _EXPORT_ROOT, layer, target)
    finally:
        exported = None  # the only reference: Sdf closes it, so Windows lets go of the file
        shutil.rmtree(scratch, ignore_errors=True)
    setattr(node, _COOKED_FROM, fingerprint)


def main():
    chunk_api = chunk.Chunk("Blender Import")
    chunk_api.add_params([
        chunk.Menu(
            "collection", "Collection", menu_items=[WHOLE_SCENE], default_value=WHOLE_SCENE,
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="What to import: one collection, children included, or the whole scene.",
        ),
        chunk.LineEdit(
            "primpath_location", "Insert to location", default_value="/blender",
            flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="The prim the imported objects are authored under.",
        ),
        chunk.Checkbox(
            "hide_source", "Hide Source", default_value=True,
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Hide the imported objects in Blender's viewport, so the stage's copy "
            "is not drawn on top of them. They still import when edited.",
        ),
        chunk.Checkbox(
            "live", "Live", default_value=True, flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Re-import whenever what this chunk exports changes in Blender.",
        ),
        chunk.Menu(
            "evaluation_mode", "Evaluation", menu_items=["Render", "Viewport"],
            default_value="Render", flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Which modifier and visibility settings the export uses.",
        ),
        chunk.Checkbox("export_materials", "Materials", default_value=True,
                       flags=ChunkFlags.ParamFlags.LABELED),
        chunk.Checkbox("materialx", "MaterialX", default_value=False,
                       flags=ChunkFlags.ParamFlags.LABELED,
                       tooltip="Also write a MaterialX network beside UsdPreviewSurface."),
        chunk.Checkbox("use_instancing", "Instancing", default_value=False,
                       flags=ChunkFlags.ParamFlags.LABELED,
                       tooltip="Linked duplicates and instanced collections as USD instances."),
        chunk.Checkbox("export_animation", "Animation", default_value=False,
                       flags=ChunkFlags.ParamFlags.LABELED,
                       tooltip="Time samples over the scene's frame range."),
    ])
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        hosts=["blender>=5.1"],
        requirements=["numpy"],  # Blender ships it
        # Always cached, never a checkbox: an unchanged import reads back on reopen
        # rather than running Blender's exporter again.
        cache_always=True,
        description="""
        Imports Blender objects into the stage -- one collection or the whole scene --
        the way Houdini's SOP Import brings SOPs into LOPs.

        Blender's own USD exporter does the conversion, so modifiers, UVs, normals,
        instancing, armatures, shape keys and materials (UsdPreviewSurface, optionally
        MaterialX) come through as Blender writes them. The result is copied into this
        chunk's layer under the target prim: nothing is left on disk beside the network,
        and an unchanged import is read back from the cook cache instead of exported again.

        Live by default: editing what the collection holds re-imports it; anything else
        Blender does is ignored.
        """,
    )
