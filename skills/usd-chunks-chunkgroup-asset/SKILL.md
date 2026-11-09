---
name: usd-chunks-chunkgroup-asset
description: Package a usd_chunks network as a reusable ChunkGroup asset -- a group with a description and lofted (exposed) params, saved to its own .usda -- and bring it into other scenes by reference, where edits stay overrides. Use when asked to make a network reusable, publish or share a tool/setup as a file, expose params on a group, or reference one network into another.
---

# Make a ChunkGroup asset

A ChunkGroup is a subnet. Saved to its own file, it becomes an asset: another scene
references it, and it shows as one node you can dive into. The file is never written to
from there; changes made in the other scene are `over`s in that scene's own layer. The
background is in `docs/core-chunks.md#chunkgroup`.

An asset has three parts:

- the **network** inside the group, ending at the group's Output;
- the **interface**: the group's `doc` description plus the params lofted onto it;
- the **file** it's saved to.

## 1. Build the network inside a group

Follow the `usd-chunks-build-network` skill, but make the chunks inside the group:

```python
from __future__ import annotations

from usd_chunks import Scene, chunk_stage

with Scene.new() as scene:
    group = scene.create.chunkgroup(
        name="Post",
        doc="A fence post: a cube lifted by Transform_ty. Writes /post/geo.",
    )
    with group:                                   # chunks made here go inside the group
        box = scene.create.cube(prim_path="/post/geo")
        lift = scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=1.0)
        scene.set_output(lift)                    # what the group delivers
```

- **Name the group** for what it makes. Write `doc` for someone who will never open it:
  what it builds, which prims it writes, and what each exposed param does. People read it
  in the graph, and agents and `usdchunks info` read it from the file.
- **End at an Output** (`scene.set_output(last)` inside the group). A group delivers its
  Output.
- Keep the prims it writes under one root (`/post/...`), so a scene can tell its content
  apart from everything else.

## 2. Loft the params a user should change

Lofting publishes an inner param on the group. That gives the group a real param, and
saves it as an `inputs:` attribute on the group prim.

```python
    chunk_stage.loft_param_to_context(lift.get_param("ty"), group)   # -> group param "Transform_ty"
```

- The lofted name is `<InnerChunk>_<param>` (here `Transform_ty`). It's fixed once
  lofted, because files and command lines refer to it. Rename the inner chunk *before*
  lofting if you want a better name.
- Loft only what a user of the asset should change. Everything else stays inside.
- A vector lofts whole: loft the vector param, not its fields.
- In the graph: right-click a param and choose **Loft up**. **Loft up chunk** shows a whole
  chunk's panel on the group's Params page without adding params.

`loft_param_to_context` and `export_network` (below) are `usd_chunks.chunk_stage`
functions. The `Scene` API has no wrapper for them yet.

## 3. Save it to its own file

```python
    chunk_stage.export_network(scene, group, "assets/post_net.usda")   # returns "/chunks/Post"
```

- `export_network` writes just this group, at `/chunks/<name>` in the new file, with its
  wiring rebased and the link to the graph outside it dropped.
- `chunk_stage.publish_network(group, path)` writes the same file and also replaces the
  group in the current scene with a reference to it. Use it when the current scene should
  keep using the published asset.
- `scene.save(path)` and the **Save ChunkGroup** menu write the whole graph. That's fine
  when the group is all there is.

## 4. Check the interface

```
usdchunks info assets/post_net.usda                     # prints the doc and Transform_ty = 1.0
usdchunks run assets/post_net.usda Transform_ty=2.5 -p  # the lofted param drives the result
```

A lofted param is set on the command line by its bare name. If the `-p` output doesn't
change, the param isn't lofted or the network doesn't reach the Output.

## 5. Use it in another scene

Point a Reference chunk at the file (a Sublayer or Payload chunk works too). The node
becomes the group:

```python
with Scene.open("shot.usda") as scene:
    post = scene.create.reference(file_path="assets/post_net.usda")
    scene.set(post, Transform_ty=4.0)        # the lofted param, on the node
    scene.set_output(post)
    scene.save("shot.usda")
```

- The asset's chunks compose in under the node, **locked** in the graph (amber header).
  Their params can still be changed; each change is an `over` in this scene, and
  `assets/post_net.usda` is untouched.
- To change the asset itself, open its own file and edit it there (see the
  build-network skill). In the graph, **Allow Editing of Contents** turns the reference
  into a local copy of the group, cut off from the file.
- A warning that a `ui:gizmo:*:anchor` target "refers to a path outside the scope of the
  reference" is expected: the network's chunks point at scene prims like `/post/geo`, which
  live outside `/chunks`. The asset still cooks.

## 6. Hand it over

Tell the user the file path, the group's description, and each lofted param with its
default, and show the `usdchunks info` output. Suggest a test if the asset ships with the
repo or a pipeline depends on it: open a scene, reference the file, set a lofted param,
and assert on the prims it writes.
