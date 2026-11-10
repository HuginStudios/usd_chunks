# Common chunks

The chunks most networks use, one line each: what it is for, then the params that matter most.
Read this before `usdchunks show`; show a chunk only when it isn't here or you need a param that
isn't listed. Recipes that combine them are in SKILL.md ("Common jobs").
`tests/test_skill_common_chunks.py` checks every chunk and param named here exists.

- shape: USD Cube, Sphere, Cylinder, Cone, Capsule or Plane prims (menu: usd_cube, usd_sphere, usd_cylinder ...), centred on their origin. Params: prim_path, shape_type, cube_size, sphere_radius, round_radius, round_height, axis.
- mesh: polygon meshes with UVs (menu: ground, grid, plane, cube, sphere ...); a grid is width1 (X) by height2 (Z) and binds a checker unless grid_checker=False; mesh_type values are lowercase. Params: prim_path, mesh_type, width1, height2, grid_checker, width, height, depth, radius1.
- transform: moves, turns and scales existing prims; aim_at_point with aim_point turns the prim's -Z to a point. Params: primpath_location, tx, ty, tz, rx, ry, rz, sx, sy, sz, aim_at, aim_at_point, aim_point.
- duplicate_prim: N copies in a row, ring or spiral; copy i is the source moved i times by the per-copy offset, about copy_pivot; copies are named after the source. Params: source_prim, dest_path, copies, copy_translate, copy_rotate, copy_pivot, as_instances.
- prune_prim: removes prims; exports leave them out. Params: prim_path.
- preview_surface: a UsdPreviewSurface material bound to primpath_location (comma-separate several prims). Params: mat_path, primpath_location, diffuse_color, roughness, metallic, opacity.
- set_display_color: a flat colour, no material. Params: prim_path, color_r, color_g, color_b.
- property_editor: overrides attributes on existing prims; edits is one `name = <JSON value>` per line, by USD name (`inputs:` on lights); `float name = 1` creates one. Params: prim, edits.
- light: menu distant_light, dome_light, rect_light, sphere_light, disk_light; intensity, width, height and color are built when it cooks but can be passed at create; place or aim it with a Transform. Params: primpath_location, light_type.
- camera: a camera with its own placement; aim_at_point with aim_point looks at a point. Params: primpath_location, focalLength, tx, ty, tz, aim_at, aim_at_point, aim_point.
- scatter_points: a PointInstancer of prototypes over a mesh; prototypes are rows of proto_path and proto_weight; an exact count with auto_spacing=False, spacing and max_count. Params: surface_prim, inst_path, prototypes, auto_spacing, spacing, max_count, density, seed, scale_min, scale_max, orient_mode, rand_axis, rand_amount.
- rbd_solver: rigid bodies fall and collide (MuJoCo), baked as animation; collision_path holds the still colliders, one per line; leave gravity at 0 (Earth gravity along the up axis). Params: prim_path, collision_path, shape_type, start_frame, frame_count, restitution, friction, initial_velocity, gravity.
- reference: brings a file in at primpath_location; a relative file_path is found from the network file's folder. Params: file_path, primpath_location, import_primpath.
- sublayer: files under the scene, one row per file. Params: imports.
- stage_settings: units, up axis, frame range, default prim (new scenes are already metres, Y up). Params: meters_per_unit, up_axis, start_frame, end_frame, default_prim.
- layer: a new layer for a department (layout, lighting). Params: layer_name.
- edit_target: routes the chunks below it into a layer by name; "Session Layer" routes back out. Params: edit_target.
- export_usd: writes files when the network is committed: one per layer into output_dir, or one flattened file. Params: output_dir, session_file_name, bake_all, flatten, output_file, keep_session_layer, export_layers.
- urdf_import: a URDF robot as UsdPhysics links, meshes, colliders and joints; mesh_root is where `package://` paths are found. Params: primpath_location, file_path, mesh_root.
- pose_articulation: one slider per moving joint of an imported robot, named after the link it moves. Params: prim_path.
