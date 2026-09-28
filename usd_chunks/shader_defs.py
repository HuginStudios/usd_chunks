"""Shader definitions read from USD's shader registry (Sdr), normalised for chunks.

The model is small and frontend-agnostic, and chunks turn it into params. One provider
serves every renderer that publishes definitions to Sdr -- MaterialX (``mtlx``), V-Ray
(``vray``), RenderMan (``args``), OSL (``OSL``) -- because the only thing that differs
between them is the source type string and how the resulting ``UsdShade.Shader`` is
identified in USD. Nothing here imports Qt.

``` py
for d in iter_shader_defs("mtlx"):
    print(d.identifier, len(d.inputs))

d = get_shader_def("ND_standard_surface_surfaceshader")
params = [param_for_prop(p) for p in d.inputs]
```
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from collections.abc import Iterator
from typing import Any

from pxr import Sdr

from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

# Sdf type name -> how to build the param, overriding chunk.usd_type_map where a shader
# input wants something better than the generic USD-attribute choice.
#
# color3f gets a swatch rather than usd_type_map's three sliders. This relies on chunk_stage
# resolving a ColorPicker to Color3f: its value is a tuple, and _SDF_TYPE_MAP is keyed by
# Python type, so without that a colour param gets no chunk-prim attr and its value cannot
# round-trip. color4f is left as a Vector -- a ColorPicker is rgb only.
# Filter for a shader's asset inputs. An asset on a shader is a texture in practice, and
# without a filter the browser lists every file in the directory. Same set as
# chunks/materials/uv_texture.py, so both texture browsers behave alike.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".exr", ".tx", ".tif", ".tiff", ".hdr")

_TYPE_OVERRIDES: dict = {
    "int": (chunk.Slider, {"is_int": True}),
    "color3f": (chunk.ColorPicker, {}),
    # Geometric roles usd_type_map does not carry -- common on shader inputs
    # (UsdPreviewSurface's normal is normal3f), unremarkable on a USD attribute.
    "normal3f": (chunk.Vector, {"param": chunk.Slider(), "suffixes": "xyz", "length": 3}),
    "point3f": (chunk.Vector, {"param": chunk.Slider(), "suffixes": "xyz", "length": 3}),
}

# Sdr renamed its whole accessor surface when Ndr was folded in: USD <= 0.25.x has
# GetNodeNames/GetNodeByName/GetInputNames, USD >= 26.x has the GetShaderNode*
# spellings. usd_chunks runs against both (usdbundle 0.25.5 in usdview, usd-core 26.3
# under pytest), so every registry call goes through _first_attr.
_REGISTRY_METHODS = {
    "source_types": ("GetAllSourceTypes", "GetAllNodeSourceTypes", "GetAllShaderNodeSourceTypes"),
    "names": ("GetNodeNames", "GetShaderNodeNames"),
    "by_name": ("GetNodeByName", "GetShaderNodeByName"),
    "by_name_type": ("GetNodeByNameAndType", "GetShaderNodeByNameAndType"),
}

_NODE_METHODS = {
    "input_names": ("GetInputNames", "GetShaderInputNames"),
    "output_names": ("GetOutputNames", "GetShaderOutputNames"),
}


def _first_attr(obj, names: tuple, *args):
    """Call the first method in names that obj actually has.

    Returns None when obj has none of them, so a registry from an unexpected USD
    version degrades to "no shaders" rather than raising during chunk discovery.
    """
    for n in names:
        if fn := getattr(obj, n, None):
            return fn(*args)
    return None


@dataclass(frozen=True)
class ShaderProp:
    """One input or output of a shader definition.

    Attributes:
        name (str): Property name, used verbatim as the ``inputs:``/``outputs:`` suffix
        label (str): Display label, falling back to name
        sdf_type (str): Sdf type name, e.g. "color3f"
        default (Any): Default value, or None
        connectable (bool): Whether this may take a node-graph wire
        is_output (bool): True for outputs
        page (str): Sdr page, used to group params into collapsible Groups
        help (str): Documentation, used as the param tooltip
        options (tuple): Allowed string values, making the param a Menu
    """

    name: str
    label: str = ""
    sdf_type: str = ""
    default: Any = None
    connectable: bool = True
    is_output: bool = False
    page: str = ""
    help: str = ""
    options: tuple = ()


@dataclass(frozen=True)
class ShaderDef:
    """A shader the registry knows how to describe.

    Attributes:
        identifier (str): What lands in ``info:id`` -- e.g. "ND_standard_surface_surfaceshader"
        name (str): Registry name
        source_type (str): "mtlx", "vray", "OSL", ...
        context (str): Sdr context, e.g. "surface" for a terminal shader
        family (str): Sdr family -- for MaterialX this is per-node, so it makes a poor
            menu grouping: 784 shaders spread over 213 of them
        role (str): Broad classification (MaterialX's nodegroup: math, pbr, adjustment,
            ...), the grouping worth putting in a menu
        category (str): What distinguishes this definition from its siblings in the same
            family -- for MaterialX the operand type ("float", "color3"), so family plus
            category is Houdini's node-plus-signature split
        help (str): Documentation for the chunk tooltip
        inputs (List[ShaderProp]): Input properties
        outputs (List[ShaderProp]): Output properties
    """

    identifier: str
    name: str = ""
    source_type: str = ""
    context: str = ""
    family: str = ""
    role: str = ""
    category: str = ""
    help: str = ""
    inputs: list[ShaderProp] = field(default_factory=list)
    outputs: list[ShaderProp] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        """True when this shader can terminate a material (a surface/displacement/volume)."""
        return self.context in ("surface", "displacement", "volume")


def _prop_from_sdr(prop, is_output: bool) -> ShaderProp | None:
    """Normalise one SdrShaderProperty; None when its type has no Sdf equivalent."""
    try:
        sdf_type = prop.GetTypeAsSdfType().GetSdfType()
    except Exception:  # noqa: BLE001 - a property type this USD cannot express
        return None

    if not sdf_type:
        return None

    return ShaderProp(
        name=prop.GetName(),
        label=prop.GetLabel() or prop.GetName(),
        sdf_type=str(sdf_type),
        default=prop.GetDefaultValue(),
        connectable=bool(prop.IsConnectable()),
        is_output=is_output,
        page=prop.GetPage() or "",
        help=prop.GetHelp() or "",
        options=tuple(str(o[0]) for o in (prop.GetOptions() or ()) if o),
    )


def _def_from_sdr(node) -> ShaderDef:
    """Normalise one SdrShaderNode into a ShaderDef."""
    inputs: list[ShaderProp] = []
    outputs: list[ShaderProp] = []

    for names, is_output, into in (
        (_first_attr(node, _NODE_METHODS["input_names"]) or (), False, inputs),
        (_first_attr(node, _NODE_METHODS["output_names"]) or (), True, outputs),
    ):
        for n in names:
            prop = node.GetShaderOutput(n) if is_output else node.GetShaderInput(n)
            if prop and (p := _prop_from_sdr(prop, is_output)):
                into.append(p)

    return ShaderDef(
        identifier=node.GetIdentifier() or node.GetName(),
        name=node.GetName(),
        source_type=node.GetSourceType() or "",
        context=node.GetContext() or "",
        family=node.GetFamily() or "",
        role=(getattr(node, "GetRole", lambda: "")() or ""),
        category=node.GetCategory() or "",
        help=node.GetHelp() or "",
        inputs=inputs,
        outputs=outputs,
    )


def available_source_types() -> list[str]:
    """Every shader source type this USD's registry can parse (e.g. ['mtlx', 'vray'])."""
    return [str(s) for s in (_first_attr(Sdr.Registry(), _REGISTRY_METHODS["source_types"]) or ())]


#: Whether the "registry unavailable" warning has been said once this process.
_registry_warned = False


def iter_shader_defs(source_type: str = "") -> Iterator[ShaderDef]:
    """Yield every shader definition the registry can parse.

    Names are *discovered* far more freely than they can be *parsed* -- a renderer may
    advertise a shader whose parser needs that renderer initialised (V-Ray does this
    outside a live hdVRay). Unparseable names are skipped rather than raised on, so a
    chunk's variant list simply reflects what this runtime can actually describe.

    Args:
        source_type (str): Restrict to one source type; empty yields all.

    Yields:
        ShaderDef: One per parseable shader.
    """
    try:
        reg = Sdr.Registry()
        names = _first_attr(reg, _REGISTRY_METHODS["names"]) or ()
    except Exception as e:  # noqa: BLE001 - discovery is best-effort, like the parse below
        # Building the registry at all can fail, not just parsing one definition: it walks
        # every installed plugin's advertised shaders, and one bad entry takes the lot.
        # Seen on a USD 26.05 build -- "Invalid version 0.0: both components must be
        # non-negative and at least one non-zero" -- where nothing usd_chunks owns is
        # involved and there is nothing for a caller to do but carry on with no shaders.
        # Once per process, not once per menu: the failure is retried every time a menu is
        # drawn (see ChunkConfig.get_variants -- a registry that needs its renderer
        # initialised can start working later), and saying so on every right-click buries
        # everything else in the log. The retry still happens; only the telling is deduped.
        global _registry_warned
        if not _registry_warned:
            _registry_warned = True
            logger.warning("Shader registry unavailable, no shader definitions: %s", e)
        else:
            logger.debug("Shader registry still unavailable: %s", e)
        return

    for name in names:
        node = None
        if source_type:
            node = _first_attr(reg, _REGISTRY_METHODS["by_name_type"], str(name), source_type)
        else:
            node = _first_attr(reg, _REGISTRY_METHODS["by_name"], str(name))

        if not node:
            continue
        if source_type and (node.GetSourceType() or "") != source_type:
            continue

        try:
            yield _def_from_sdr(node)
        except Exception as e:  # noqa: BLE001 - one bad definition must not kill discovery
            logger.debug("Skipping shader definition %s: %s", name, e)


#: Started once per process; see warm_registry_in_background.
_warm_thread: threading.Thread | None = None


def warm_registry_in_background(source_type: str = "mtlx") -> threading.Thread | None:
    """Build the shader registry on a worker thread, so the first menu does not wait for it.

    The first touch of Sdr.Registry() discovers every installed shader plugin -- ~2.7s in
    a usdview with V-Ray loaded -- and the first chunk menu drawn is what touched it, so the
    first right-click took seconds. Registry construction and parsing are USD's own
    thread-safe C++ and release the GIL, so the UI keeps running meanwhile (measured: no
    main-thread stall over 50ms), and a menu opened before the thread finishes simply waits
    on the same singleton rather than paying twice.

    Only the registry is warmed, not ChunkConfig.get_variants: that memo is plain Python
    state the main thread reads, and computing it from here would race it. And only
    source_type is parsed -- what the menu asks for. Parsing everything at once is not safe:
    GetShaderNodesByFamily() took usdview down with V-Ray loaded, whose parser wants a live
    renderer.

    Args:
        source_type (str): The shader source type to parse ahead of time.

    Returns:
        Optional[threading.Thread]: The warming thread, or None if one was already started.
    """
    global _warm_thread
    if _warm_thread is not None:
        return None

    def warm():
        for _ in range(2):
            try:
                reg = Sdr.Registry()
                break
            except Exception as e:  # noqa: BLE001 - see iter_shader_defs; warming is best-effort
                # The first construction can raise on a bad advertised version and succeed
                # when asked again, which is what the menu would do anyway.
                logger.debug("Shader registry not ready while warming: %s", e)
        else:
            return
        try:
            for name in _first_attr(reg, _REGISTRY_METHODS["names"]) or ():
                _first_attr(reg, _REGISTRY_METHODS["by_name_type"], str(name), source_type)
        except Exception as e:  # noqa: BLE001
            logger.debug("Warming shader definitions stopped early: %s", e)

    _warm_thread = threading.Thread(target=warm, name="usd_chunks-sdr-warm", daemon=True)
    _warm_thread.start()
    return _warm_thread


def get_shader_def(name: str, source_type: str = "") -> ShaderDef | None:
    """Look up a single shader definition by registry name.

    Args:
        name (str): Registry name, e.g. "ND_standard_surface_surfaceshader"
        source_type (str): Optional source type to disambiguate

    Returns:
        Optional[ShaderDef]: The definition, or None when absent or unparseable.
    """
    reg = Sdr.Registry()

    if source_type:
        node = _first_attr(reg, _REGISTRY_METHODS["by_name_type"], name, source_type)
    else:
        node = _first_attr(reg, _REGISTRY_METHODS["by_name"], name)

    return _def_from_sdr(node) if node else None


def _coerce_default(value: Any) -> Any:
    """Turn an Sdr default into something a chunk param can hold.

    Gf vector/colour types are sequence-like but are not tuples, and the param classes
    store plain Python values.
    """
    if value is None:
        return None
    if isinstance(value, (str, bool, int, float, tuple, list)):
        return value
    try:
        return tuple(value)
    except TypeError:
        return value


def param_for_prop(prop: ShaderProp):
    """Build the chunk param that edits one shader property.

    Returns None for a type with no param mapping -- callers skip those rather than
    presenting an input they cannot edit.

    The default is passed to the constructor rather than assigned afterwards: assigning
    .value on a freshly built param fires value_changed, which reaches for the registry's
    scene and blows up on a param that is not attached to one yet.

    Args:
        prop (ShaderProp): The property to build a param for

    Returns:
        Optional[ChunkCommon]: A param (or Vector group), or None if unmapped.
    """
    default = _coerce_default(prop.default)

    if prop.options:
        return chunk.Menu(
            prop.name,
            prop.label,
            menu_items=list(prop.options),
            default_value=str(default) if default is not None else prop.options[0],
            flags=ChunkFlags.ParamFlags.LABELED,
        )

    spec = _TYPE_OVERRIDES.get(prop.sdf_type) or chunk.usd_type_map.get(prop.sdf_type)
    if not spec:
        return None

    cls, extra = spec if isinstance(spec, tuple) else (spec, {})
    kwargs = dict(extra)
    if default is not None:
        kwargs["default_value"] = default

    try:
        param = cls(prop.name, prop.label, **kwargs)
    except Exception as e:  # noqa: BLE001 - a default this param type cannot hold
        logger.debug("Could not build param for %s (%s): %s", prop.name, prop.sdf_type, e)
        kwargs.pop("default_value", None)
        try:
            param = cls(prop.name, prop.label, **kwargs)
        except Exception:  # noqa: BLE001 - type is mapped but unconstructable here
            return None

    # A shader reads its textures, so the browser must offer an Open dialog. Without
    # FILE_EXISTING the widget computes create_new=True and file_dial puts up
    # getSaveFileName -- a Save prompt for a file the shader needs to already exist.
    if isinstance(param, chunk.FileBrowser):
        param.set_flag(ChunkFlags.ParamFlags.FILE_EXISTING)
        param.extensions = list(_IMAGE_EXTENSIONS)

    return param
