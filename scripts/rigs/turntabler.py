from usd_chunks import chunk
from pxr import UsdGeom, Gf, UsdLux, Sdf
from scripts.modelling import add_mesh

import math

import usd_chunks.chunk_core


def apply(owner, notice, sender):
    layer = owner.get_edit_context()
    if not layer:
        return None

    # tube = add_mesh.build_tube(owner.get_stage(), "/turntable/model/pedetal", 20, 10, 1, 1)
    # chunk_registry.ChunkLayerManager().clear_layer(layer.identifier)


def num_lights_changed(owner: chunk.Group, notice, sender):
    nlights_param = owner.get_param("numLights")
    light_pos_grp = owner.get_param("modify_lights")
    if not nlights_param or not light_pos_grp:
        return

    layer = owner.get_edit_context()
    if not layer:
        return

    params = light_pos_grp.get_my_params()

    lights_spec = layer.GetPrimAtPath("/turntable/lights")
    nlights = nlights_param.value
    nparams = len(params)

    if nlights < nparams:
        for i in range(nlights, nparams):
            params[i].request_delete()
            prim_spec = layer.GetPrimAtPath(f"/turntable/lights/light{i}")
            del lights_spec.nameChildren[prim_spec.name]  # type: ignore

    else:
        for i in range(nparams, nlights):
            x = i / nlights
            xp = math.cos(x * math.pi * 2)
            zp = math.sin(-x * math.pi * 2)
            y = 0.4
            pos = Gf.Vec3f(xp, y, zp)

            light = UsdLux.SphereLight.Define(owner.get_stage(), f"/turntable/lights/light{i}")
            op = light.GetTranslateOp()
            if not op:
                op = light.AddTranslateOp()

            op.Set(pos)

            # Create a parameter for managing the position of the light
            pos_param = chunk.param_from_attr(op.GetAttr())
            if not pos_param:
                continue

            # Update min and max value of the nested vector parameters
            for slider in pos_param.get_params():
                slider.min_value = -10
                slider.max_value = 10

            # Create a parameter for modifying the intensity of the light
            intens_param = chunk.param_from_attr(light.CreateIntensityAttr())
            if not intens_param:
                continue

            # Create a parameter for managing the position of the light
            color_param = chunk.param_from_attr(light.CreateColorAttr())
            if not color_param:
                continue

            # Create the container group for the light parameters
            light_grp = chunk.Group(f"light{i}")
            light_grp.set_flag(usd_chunks.chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE)
            light_grp.set_state(usd_chunks.chunk_core.ChunkFlags.State.COLLAPSED)

            light_grp.add_params([intens_param, pos_param, color_param])
            light_pos_grp.add_param(light_grp)

            proxy = UsdGeom.Sphere.Define(owner.get_stage(), f"/turntable/lights/light{i}/proxy{i}")
            proxy.CreateRadiusAttr().Set(0.1)
            proxy.GetPurposeAttr().Set("proxy")


def build_light_group(turntabler):
    nlights = chunk.Spinbox("numLights", is_int=True)
    light_grp = chunk.Group("lights")
    light_grp.add_callback(num_lights_changed, nlights)

    light_pos_grp = chunk.Group("modify_lights", "lights")

    light_grp.add_params([nlights, light_pos_grp])
    return turntabler.add_param(light_grp)


def build_pedestal(owner, notice, sender):
    stage = owner.get_stage()
    prim = stage.GetPrimAtPath(owner.value)
    if not prim:
        return

    bbox_cache: UsdGeom.BBoxCache = owner.user_data["bbox_cache"]
    bounds = bbox_cache.ComputeWorldBound(prim)
    r = bounds.GetRange()
    b0 = r.GetCorner(2)
    b1 = r.GetCorner(3) + (r.GetCorner(4) - r.GetCorner(0))

    h = Gf.Vec3d(b0 - r.GetCorner(4)).GetLength()

    rad = Gf.Vec3d(b0 - b1).GetLength() / 2
    height = 0.1

    c = bounds.ComputeCentroid() - Gf.Vec3d(0, (h - height) / 2, 0)

    add_mesh.build_tube(stage, "/turntabler/geom/pedestal", 10, 50, rad, height, pos=c)


def refererence_geom(owner: chunk.LineEdit, notice, sender):
    stage = owner.get_stage()
    prim = stage.GetPrimAtPath(owner.value)
    if not prim:
        return

    img = UsdGeom.Imageable.Get(stage, prim.GetPath())
    img.GetVisibilityAttr().Set("invisible")

    model_ref = stage.DefinePrim(Sdf.Path("/turntabler/geom/model"), "Xform")
    model_ref.GetReferences().AddInternalReference(prim.GetPath())
    img = UsdGeom.Imageable.Get(stage, model_ref.GetPath())
    img.GetVisibilityAttr().Set("inherited")


def create_camera(owner: chunk.LineEdit, notice, sender):
    stage = owner.get_stage()
    prim = stage.GetPrimAtPath(owner.value)
    if not prim:
        return

    bbox_cache: UsdGeom.BBoxCache = owner.user_data["bbox_cache"]
    bounds = bbox_cache.ComputeWorldBound(prim)
    s = bounds.GetBox().GetSize().GetLength()

    camera = UsdGeom.Camera.Define(stage, "/turntabler/camera")
    camera_grp = owner.get_chunk().get_param("camera")

    gfCam = camera.GetCamera()
    camera.GetClippingRangeAttr().Set(Gf.Vec2d(0.001, 1000))
    a = gfCam.GetFieldOfView(gfCam.FOVVertical) * (math.pi / 180)

    aim = Gf.Vec3d(-20, 45, 0)
    op = camera.GetRotateXYZOp()
    if not op:
        op = camera.AddRotateXYZOp()
    op.Set(aim)

    rot_param = chunk.param_from_attr(op.GetAttr())
    for p in rot_param.get_params():
        p.min_value = 0
        p.max_value = 360
    camera_grp.add_param(rot_param)

    z = (s / 2) / math.tan(a / 2)
    pos = Gf.Vec3d(0, 0, z)
    op = camera.GetTranslateOp()
    if not op:
        op = camera.AddTranslateOp()
    op.Set(pos)
    pos_param = chunk.param_from_attr(op.GetAttr())
    for p in pos_param.get_params():
        p.min_value = -10
        p.max_value = 10

    camera_grp.add_param(pos_param)

    # tube = add_mesh.build_tube(stage, "/turntabler/geom/pedestal", 20, 10, rad, height, pos=c)


def build_model_group(turntabler: chunk.Chunk):
    model_grp = chunk.Group("model")
    path_param = chunk.LineEdit("prim")
    path_param.add_callback(build_pedestal, send_on_create=True)
    path_param.add_callback(refererence_geom, send_on_create=True)

    path_param.user_data["bbox_cache"] = UsdGeom.BBoxCache(0, ["render", "default"], True)

    model_grp.add_params([path_param])

    return turntabler.add_param(model_grp)


def build_camera_group(turntabler):
    camera_grp = chunk.Group("camera")
    turntabler.add_param(camera_grp)

    path_param = turntabler.get_param("prim")
    path_param.add_callback(create_camera, send_on_create=True)

    return camera_grp


def main():
    turntabler = chunk.Chunk("turtabler")
    light_grp = build_light_group(turntabler)
    model_grp = build_model_group(turntabler)
    camera_grp = build_camera_group(turntabler)

    light_grp.set_flag(usd_chunks.chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE)
    model_grp.set_flag(usd_chunks.chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE)
    camera_grp.set_flag(usd_chunks.chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE)

    light_grp.set_state(usd_chunks.chunk_core.ChunkFlags.State.COLLAPSED)
    model_grp.set_state(usd_chunks.chunk_core.ChunkFlags.State.COLLAPSED)
    camera_grp.set_state(usd_chunks.chunk_core.ChunkFlags.State.COLLAPSED)

    apply_btn = chunk.Button("apply")

    turntabler.add_callback(apply, apply_btn)
    turntabler.add_params([apply_btn])

    return turntabler


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.RIGS)
