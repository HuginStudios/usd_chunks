# Chunks by task

Every chunk, grouped by the job it does, with when to pick it over its neighbours. Use it to
choose; `common-chunks.md` has the key params of the ones most networks use, and
`usdchunks show <name>` has every param. A chunk that builds the object you need beats
modelling it from boxes: look here before you model anything.
`tests/test_skill_chunks_by_task.py` checks that every chunk is listed and every name exists.

## Make objects

- `shape`: USD's own Cube, Sphere, Cylinder, Cone, Capsule, Plane prims, centred on the prim. Pick it for simple stand-ins and colliders.
- `mesh`: polygon meshes with UVs (cube, sphere, torus, cone, tube, grid, ground). Pick it when the object needs a textured material, a scatter surface or a deformer; the ground and grid entries make terrain bases.
- `fence`: a whole fence (posts, rails, pickets) along a line or a curve, following the terrain, from new-built to derelict (`picket_fence`, `ranch_fence`, `board_fence`, `derelict_fence` presets). Use it rather than posts made of boxes. A straight one runs `length` along +X from its origin.
- `house`: a cottage with real window openings, floors, a chimney and a tiled roof; width re-tiles the bays. Its footprint runs from its origin to `width` (+X) and `depth` (+Z). Use it for any house or cottage before you model one.
- `prim`: an empty prim of any type (Xform, Scope ...), for grouping or a typed prim no other chunk makes.
- `inline_usd`: any USD you can type as `.usda`: an `over`, a typed prim, time samples (keyframes). The fallback when no chunk makes it.
- `cad_import`: a STEP or IGES CAD assembly, keeping its hierarchy, instancing and colours.
- `urdf_import`: a URDF robot as UsdPhysics links, joints and colliders.

## Bring in files

- `reference`: a file at a prim path you choose; local edits stay overrides. The usual way to bring in an asset, or a ready-made chunk group (`examples/groups/*.usda`, its lofted params set by name).
- `payload`: like `reference`, but the user can unload it: heavy sets and crowds.
- `sublayer`: whole files under the scene (a set, another department's layer); no prim path of their own.
- `value_clips`: per-frame or per-range files on a prim (sim caches, cycles).

## Place, copy and arrange

- `transform`: move, turn, scale prims about a pivot; aim a light or camera at a prim or point.
- `reset_transform`: clear a prim's transform ops.
- `duplicate_prim`: N copies in a row, ring or spiral (stairs, columns, a fence of objects), full copies or instances.
- `scatter_points`: many instances of one or more prototypes over a mesh as a PointInstancer: rocks, trees, grass, clutter. Spacing or density, slope cut-off (`max_slope`), `avoid_prims` to keep clear of other prims (their points, curves, and the ground under a mesh, so a house's floor stays empty), exact counts.
- `noise_field`: a noise density for `scatter_points`, so instances clump in patches instead of an even spread.
- `proximity_field`: a density from nearness to other points: grass around rocks, undergrowth under trees.
- `height_field`: a density from height: trees below a tree line, nothing on the peaks. Its `subset` gives the band its own material (a sandy shore, rocky tops).
- `edge_field`: a density from sharp edges (`find` Outer Edges) or inner corners (Inner Corners): chips and rubble along a building's edges, dirt in its corners, worn paint read in a material. Needs a mesh with divisions.
- `around_field`: the ground round an object's base, for a `scatter_points` to pile rubble or plants round it (its `ground` param follows a terrain).
- `polyhaven_model` / `polyhaven_material`: free Poly Haven models (rocks, plants, props) and textures, downloaded once; a model's parts at the origin for a scatter row of `<loc>/<model>*`.
- Styles (`concrete_debris`, `bushy_nature`, `abandoned_cottage`, `forest_floor`, `worn_paint`, ...): a whole look on one object, made with `scene.create.<style>(primpath_location="/obj")`. Reach for one before building edge wear, dirt or vines by hand; see docs/styles.md.
- `scatter_brush`: paint where `scatter_points` (or `vine`) may place, by hand.
- `merge_instancers`: join PointInstancers that share assets, so a renderer builds each asset once.
- `points_to_instancer`: animated Points (dust, sparks, a particle bake) as instanced meshes, for renderers that draw no Points.

## Grow and draw curves

- `vine`: climbing vines over the painted part of a mesh, with points for leaves.
- `add_curves`: hair or fur strands on a mesh's faces.
- `trails`: a curve through every position a point has had over time (motion paths, streaks).
- `curves_to_mesh`: curves (vines, hair, trails) as tube meshes with UVs, so they take a real material.

## Modelling tools

- `poly_reduce`: fewer triangles on a heavy mesh, keeping its silhouette, UV seams and materials.
- `remesh_proxy`: one closed proxy mesh for what decimation can't reduce (leaf cards, CAD assemblies).
- `uv_unwrap`: automatic UVs (xatlas), e.g. on a proxy before baking.
- `bake_textures`: bake a detailed asset's colour, roughness, metal and normals onto a low-poly mesh.

## Materials and colour

- `preview_surface`: a simple UsdPreviewSurface (colour, roughness, metal, opacity) bound to prims. The default for a plain material.
- `set_display_color`: a flat viewport colour, no material.
- `mtlx_shader`: a MaterialX shader (Standard Surface and others), every input from its definition.
- `AMDMaterialXLibrary`: download a ready-made MaterialX material from AMD's GPUOpen library and bind it.
- `mdl_to_mtlx`: give MDL (Omniverse) materials a MaterialX surface so other renderers shade them.
- `bind_material`: bind a material that already exists to more prims.
- `uv_texture`: an image texture in a material; wire its outputs to the surface inputs.
- `uv_transform`: offset, scale, rotate UVs before a texture (tiling).
- `primvar_reader`: read a primvar (UVs, vertex colour) into a material input.
- `uv_checker`: a UV checker material, to inspect UV layout.

## Lights, cameras, rendering

- `light`: any UsdLux light; the `distant_light`, `dome_light`, `rect_light`, `sphere_light`, `disk_light`, `cylinder_light` entries preset the type.
- `camera`: a camera with real lens and sensor presets, aimed at a prim or point.
- `set_camera`: pick which of the stage's cameras to look through.
- `render_settings`: one render's resolution, camera, products and renderer settings.
- `render_product`: one output file: its camera and AOVs.
- `render_var`: one AOV (colour, depth, a primvar) for a render product.
- `render_pass`: a pass that uses a render settings prim, and the passes before it.
- `render`: render images through Hydra, by hand or on every `usdchunks run` (in `commit()`).
- `finish_frame`: finish a rendered image: sky behind it, distance haze, sun shafts, bloom, a grade.
- `frames_to_movie`: an mp4 or mov from rendered frames.
- `frustum_prune`: drop what a camera can't see, so the renderer never loads it.

## Simulation and effects

- `rbd_solver`: rigid bodies falling and colliding (MuJoCo), baked as animation: crates, rocks, debris; with Breakable on it shatters fractured models.
- `fracture`: pre-break a model, or make procedural rocks, for `rbd_solver` to shatter.
- `physics_drive`: make a prim a collider, a spinning part or a conveyor belt for `rbd_solver`.
- `emit`: turn a PointInstancer into a stream over time (when each instance appears, its velocity), for `rbd_solver`.
- `particle_solver`: water, sand, snow or jelly (MPM, Taichi), baked as points over time.
- `surface_flow`: points crawling over a surface under gravity: water runs, rain on glass. `trails` turns the paths into streaks.
- Not covered: cloth, soft bodies, smoke, fire and volumes (fog). Bring in a cache made elsewhere (`reference`, `sublayer`, `value_clips`), author a `UsdVol` prim with `inline_usd`, or use the renderer's own fog; `finish_frame` adds distance haze to a rendered image.

## Rigs and characters

- `pose_articulation`: pose a robot or any jointed asset (drawers, doors), one slider per joint.
- `skel_pose`: hold a few joints of an animated UsdSkel character in a pose, over its animation.

## Change what's already there

- `property_editor`: set attributes on existing prims (`name = value` per line). The general way to change a value no chunk has a param for.
- `apply_schema`: apply a USD API schema (UsdPhysics, UsdLux ...) to prims.
- `set_metadata` / `get_metadata`: write or read prim metadata (kind, custom data ...).
- `get_property`: read an attribute's value as an output to wire on.
- `semantics`: label prims with semantic classes, optionally rendered as masks (datasets).
- `collection`: a named set of prims (`%Name`) that other chunks target in a prim field.
- `mute_prim`: deactivate prims, reversibly (a bypass).
- `prune_prim`: remove prims from the result for good; exports leave them out. Use it to drop a source after copying it.
- `inherit` / `specialize`: make prims share a base prim's opinions; inherit overrides every instance, specialize gives fallbacks each can override.
- `variant` / `set_variant`: add variant sets and variants to prims / pick the active variant.
- `override_chunk_prim`: change another network's chunk params from a chunk of your own (department overrides).

## Layers, departments, stage

- `stage_settings`: units, up axis, frame range, frame rate, default prim.
- `layer`: a new layer for a department (layout, lighting, anim).
- `edit_target`: send the chunks below it into a layer.
- `mute_layer`: mute layers on the stage.
- `stage_expression`: stage variables (USD expression variables) for asset paths and variants.
- `stage_expression_switch`: take one of two branches by a stage variable (only on some shots).

## Network structure

- `merge`: join branches; where they overlap the last input wins.
- `switch`: pass one branch by slot and mute the rest (try alternatives).
- `dot`: a passthrough to fan wires out tidily.
- `view`: cook everything above it while you build (the V key).
- `output`: mark what the network delivers.
- `chunkgroup`: a sub-network you dive into, save as a file and reference elsewhere.
- `wedge`: one network as N seeded variants (`usdchunks sweep` cooks them all): datasets, look development.
- `random_value`: a seeded random number to wire into params.
- `math_ops` / `vector_math_ops`: arithmetic on numbers or vectors between params (`add`, `multiply` ... presets on `math_ops`).
- `script_runner`: run Python with the stage in scope. Only when the user agrees and no chunk does the job: it runs code stored in the file.

## Write files

- `export_usd`: write each layer to its own file, or flatten into one; with `bake_all` plain USD, no network.
- `export_chunkgroup`: the network as one self-contained USD file that rebuilds itself on load.
- `kitti_writer`: rendered frames as a KITTI (and YOLO) training dataset: boxes, masks, depth.

## Ready-made groups (`reference` them)

- `examples/groups/scatter_on_terrain.usda`: instances on a terrain by density and slope (`Scatter_*` params).
- `examples/groups/spiral_stairs.usda`: spiral or straight stairs (`Stairs_*`, `Place_*` params).
