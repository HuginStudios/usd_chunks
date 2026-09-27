from pxr import Gf, Sdf, Usd, UsdShade
from usd_chunks import chunk, chunk_utils


_WRAP_MODES = ["useMetadata", "black", "clamp", "repeat", "mirror"]
_COLOR_SPACES = ["auto", "raw", "sRGB"]

_OUTPUTS = [
    ("rgb", Sdf.ValueTypeNames.Color3f),
    ("r",   Sdf.ValueTypeNames.Float),
    ("g",   Sdf.ValueTypeNames.Float),
    ("b",   Sdf.ValueTypeNames.Float),
    ("a",   Sdf.ValueTypeNames.Float),
]


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    shader_path = node.get_param("primpath_location").value

    if not shader_path:
        return

    # Clear and recreate only when the shader location changes — same pattern as camera.py.
    if shader_path != getattr(node, '_last_shader_path', None):
        node._last_shader_path = shader_path
        stage.GetEditTarget().GetLayer().Clear()

    tex = UsdShade.Shader.Define(stage, shader_path)
    tex.CreateIdAttr("UsdUVTexture")

    node.get_param("file").connect_usd_attr(
        tex.CreateInput("file", Sdf.ValueTypeNames.Asset).GetAttr()
    )
    node.get_param("wrap_s").connect_usd_attr(
        tex.CreateInput("wrapS", Sdf.ValueTypeNames.Token).GetAttr()
    )
    node.get_param("wrap_t").connect_usd_attr(
        tex.CreateInput("wrapT", Sdf.ValueTypeNames.Token).GetAttr()
    )
    node.get_param("source_color_space").connect_usd_attr(
        tex.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).GetAttr()
    )

    fallback_val = node.get_param("fallback").value
    fb_in = tex.CreateInput("fallback", Sdf.ValueTypeNames.Float4)
    fb_in.Set(Gf.Vec4f(*fallback_val))
    node.get_param("fallback").connect_usd_attr(fb_in.GetAttr())

    scale_val = node.get_param("scale").value
    sc_in = tex.CreateInput("scale", Sdf.ValueTypeNames.Float4)
    sc_in.Set(Gf.Vec4f(*scale_val))
    node.get_param("scale").connect_usd_attr(sc_in.GetAttr())

    bias_val = node.get_param("bias").value
    bi_in = tex.CreateInput("bias", Sdf.ValueTypeNames.Float4)
    bi_in.Set(Gf.Vec4f(*bias_val))
    node.get_param("bias").connect_usd_attr(bi_in.GetAttr())

    _prim_path = getattr(node, '_exec_prim_path', None) or getattr(node, 'prim_path', None)
    chunk_prim = stage.GetPrimAtPath(_prim_path) if _prim_path else None

    st_input = tex.CreateInput("st", Sdf.ValueTypeNames.Float2)
    chunk_utils.wire_shader_input(stage, chunk_prim, "st", st_input.GetAttr(), clear_on_miss=True)

    for out_name, out_type in _OUTPUTS:
        tex.CreateOutput(out_name, out_type)

    if chunk_prim and chunk_prim.IsValid():
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            for out_name, _ in _OUTPUTS:
                attr = chunk_prim.GetAttribute(f"outputs:{out_name}")
                if attr and attr.IsValid():
                    attr.SetConnections([Sdf.Path(f"{shader_path}.outputs:{out_name}")])


def main():
    chunk_api = chunk.Chunk("UVTexture")

    primpath_location = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/materials/surface/UvTexture",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    texture_file = chunk.FileBrowser(
        "file",
        extensions=[".png", ".jpg", ".jpeg", ".exr", ".tx", ".tif", ".tiff", ".hdr"],
        # The texture has to exist already. Without FILE_EXISTING the widget computes
        # create_new=True and file_dial puts up getSaveFileName -- a Save prompt for a
        # file the shader is only ever going to read.
        flags=chunk.ChunkFlags.ParamFlags.FILE_EXISTING,
    )

    wrap_s = chunk.Menu("wrap_s", "Wrap S", menu_items=_WRAP_MODES, default_value="useMetadata")
    wrap_t = chunk.Menu("wrap_t", "Wrap T", menu_items=_WRAP_MODES, default_value="useMetadata")
    source_color_space = chunk.Menu(
        "source_color_space", "Color Space",
        menu_items=_COLOR_SPACES,
        default_value="auto",
    )
    st = chunk.Vector(
        "st", "st",
        param=chunk.Slider(min_value=0.0, max_value=1.0),
        length=2,
        suffixes="st",
        default_value=(0.0, 0.0),
    )
    fallback = chunk.Vector(
        "fallback", "Fallback",
        param=chunk.Slider(min_value=0.0, max_value=1.0),
        length=4,
        suffixes="rgba",
        default_value=(0.0, 0.0, 0.0, 1.0),
    )
    scale = chunk.Vector(
        "scale", "Scale",
        param=chunk.Slider(min_value=0.0, max_value=4.0),
        length=4,
        suffixes="rgba",
        default_value=(1.0, 1.0, 1.0, 1.0),
    )
    bias = chunk.Vector(
        "bias", "Bias",
        param=chunk.Slider(min_value=-1.0, max_value=1.0),
        length=4,
        suffixes="rgba",
        default_value=(0.0, 0.0, 0.0, 0.0),
    )

    chunk_api.add_params([
        primpath_location, texture_file,
        st, wrap_s, wrap_t, source_color_space,
        fallback, scale, bias,
    ])
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MATERIALS,
        outputs=[
            ("rgb", "Color3f"),
            ("r",   "Float"),
            ("g",   "Float"),
            ("b",   "Float"),
            ("a",   "Float"),
        ],
        description="""
        Creates a UsdUVTexture shader inside the material at primpath_location.

        Outputs rgb/r/g/b/a are exposed as nodegraph ports. Connect them
        to a Preview Surface input to wire the texture into the material.

        Color Space: set to 'raw' for normal maps and data textures.
        Scale / Bias are applied after sampling: out = sample * scale + bias.
        """,
    )
