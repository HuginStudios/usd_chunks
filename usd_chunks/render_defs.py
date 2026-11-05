"""Renderer-specific render settings, normalised into a model chunks can turn into params.

Every renderer authors its settings the same way: namespaced attributes on the
UsdRenderSettings prim -- ``vray:*`` for V-Ray, ``ri:*`` for RenderMan,
``karma:global:*`` for Karma. Hydra gathers those into the render settings prim's
namespaced settings and each delegate picks out the namespace it owns, so the
*authoring* side needs nothing renderer-specific.

What differs is where the settings are *declared*, and that is exactly what a params UI
needs:

* **Applied API schema** -- V-Ray ships ``VRayRendererSettingsAPI``, an applied API
  schema whose ``apiSchemaCanOnlyApplyTo`` is ``RenderSettings``. USD's schema registry
  then knows every attribute, its type, its fallback value and its documentation.
* **Sdr** -- RenderMan ships ``.args`` files that register into the shader registry
  instead, with no USD schema involved.
* **The delegate itself** -- Cycles ships neither. hdCycles declares its settings in C++
  as Hydra render setting descriptors, so the only way to read them is to load the
  delegate and ask it, which is what ``UsdImagingGL.Engine.GetRendererSettingsList``
  does. Its keys are already namespaced (``cycles:integrator:max_bounce``), so what
  comes back needs no more translation than the other two.

All three are read here and returned as the same :class:`RenderSettingsDef`, so a chunk
never learns which renderer or which mechanism produced a setting. Nothing here imports
Qt.

A renderer's *camera* settings are declared the first way only -- V-Ray's
``VRayCameraAPI`` applies only to ``Camera`` -- and :func:`iter_camera_defs` returns them
in the same shape.

``` py
for d in iter_render_settings_defs():
    print(d.label, d.namespace, len(d.props))

d = get_render_settings_def("VRayRendererSettingsAPI")
for group, props in group_props(d.props).items():
    print(group, [p.name for p in props])
```
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from collections.abc import Iterator
from typing import Any

from pxr import Plug, Usd

from usd_chunks import shader_defs
from usd_chunks.chunk_cache import user_cache_dir

try:
    from pxr import UsdImagingGL
except ImportError:  # usd-core wheels ship no imaging, so no delegate to ask
    UsdImagingGL = None

logger = logging.getLogger(__name__)

#: Prim type these settings attach to. Applied API schemas advertise this in plugInfo.
RENDER_SETTINGS_TYPE = "RenderSettings"

#: Prim type a renderer's camera settings attach to -- V-Ray's VRayCameraAPI (physical
#: camera, dome camera, ...) is declared as applying only to it.
CAMERA_TYPE = "Camera"

#: Suffixes stripped from a schema type name to get a display label, longest first so
#: "RendererSettingsAPI" wins over "SettingsAPI".
_LABEL_SUFFIXES = ("RendererSettingsAPI", "RenderSettingsAPI", "SettingsAPI", "CameraAPI", "API")

#: Sdr identifiers that describe a renderer's options rather than a shader, by namespace.
#: RenderMan declares its settings this way instead of as a USD schema. Unverified --
#: this machine has no RenderMan install -- so treat it as the seam to extend rather
#: than a tested mapping.
_SDR_OPTION_NODES: dict[str, tuple] = {
    "ri": ("PRManOptions", "PRManGlobalOptions"),
}

#: Sdf type for each of the four types a Hydra render setting descriptor can reach Python
#: as. The binding drops any other type with a "doesn't have a UI implementation" warning
#: before it gets here -- that is how hdCycles' double Time Limit and its float[] pixel
#: jitter override go missing -- so this table is complete by construction, not by choice.
_DELEGATE_SDF_TYPES = {
    "FLAG": "bool",
    "INT": "int",
    "FLOAT": "float",
    "STRING": "string",
}

#: Enumerated settings a delegate can only describe as a bare integer, as
#: (attribute name -> ((label, value), ...)).
#:
#: A Hydra render setting descriptor carries a name, a key and a default and nothing
#: else -- there is no field for allowed values -- so an enum arrives as an int and its
#: names are lost in transit. That leaves a settings UI offering "4" on an unbounded
#: slider, where 1 and 3 are equally reachable and mean nothing at all.
#:
#: Transcribed from the renderer's own NodeEnum declarations rather than inferred, and
#: paired explicitly rather than by position: Cycles registers sampling_pattern's
#: blue_noise_round (4) before blue_noise_first (3), so anything index-based would
#: quietly write the wrong pattern.
#:
#: Renderer-specific by necessity, in the same spirit as _SDR_OPTION_NODES. A schema
#: renderer needs no entry here -- V-Ray's allowedTokens already reach options.
_DELEGATE_ENUMS: dict[str, tuple] = {
    "cycles:integrator:denoiser_type": (
        ("none", 0), ("optix", 2), ("openimagedenoise", 4),
    ),
    "cycles:integrator:denoiser_prefilter": (
        ("none", 1), ("fast", 2), ("accurate", 3),
    ),
    "cycles:integrator:denoiser_quality": (
        ("high", 1), ("balanced", 2), ("fast", 3),
    ),
    "cycles:integrator:sampling_pattern": (
        ("sobol_burley", 0), ("tabulated_sobol", 1), ("blue_noise_pure", 2),
        ("blue_noise_round", 4), ("blue_noise_first", 3),
    ),
    "cycles:integrator:guiding_distribution_type": (
        ("PARALLAX_AWARE_VMM", 0), ("DIRECTIONAL_QUAD_TREE", 1), ("VMM", 2),
    ),
    "cycles:integrator:guiding_directional_sampling_type": (
        ("MIS", 0), ("RIS", 1), ("ROUGHNESS", 2),
    ),
    "cycles:integrator:direct_light_sampling_type": (
        ("multiple_importance_sampling", 0), ("forward_path_tracing", 1),
        ("next_event_estimation", 2),
    ),
}

#: Leading name segments that qualify a setting rather than naming one. A renderer that
#: calls its switch use_denoise has said "denoise", not "use" -- so grouping on the first
#: segment files the switch away from the thing it switches, and Cycles ends up with the
#: denoiser's controls under "denoiser" and no way to turn it on anywhere near them. The
#: segment after the qualifier is the subject, and that is what earns the group.
_QUALIFIER_SEGMENTS = {"use", "enable", "enabled"}

#: A segment needs at least this many settings sharing it before it earns a group, so a
#: one-off name does not become a section of one.
_MIN_GROUP_SIZE = 2

#: A group larger than this is split again on the next name segment. Small enough that a
#: section fits on screen, large enough that tidy groups are left alone.
_MAX_FLAT_GROUP = 8

#: Names carry at most a few meaningful segments; beyond this the nesting costs more to
#: navigate than the grouping saves.
_MAX_DEPTH = 3

#: Fraction of a group that must land in real subgroups for splitting it to be worth it.
#: Below this the split is mostly leftovers -- BakeView divides into u, v and ten others,
#: which reads worse than the flat fourteen it started as.
_MIN_SPLIT_COVERAGE = 0.6

_GENERAL_GROUP = "General"

#: Host-application variables that survive into a schema's defaults. V-Ray's schema is
#: generated for Solaris, so paths arrive as ${HIP}/${HIPNAME}.vrscene and
#: ${HIP}/render/${HIPNAME}.<frame04>.exr -- Houdini expansions that nothing outside
#: Houdini resolves, leaving a path that would be written literally.
_HOST_VARIABLE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_]\w*|<[^>]+>")

#: Leading CamelCase word of a group name, when something follows it in the same case:
#: "SettingsCameraDof" -> "Settings". Renderers name plugin families this way, so a
#: shared leading word is a level of hierarchy spelled without a separator.
_CAMEL_PREFIX = re.compile(r"^([A-Z][a-z0-9]+)(?=[A-Z])")

#: How many sibling groups must share a leading word before it becomes a level of its
#: own. V-Ray has twenty-one Settings* groups, which is a section; two would be a pair.
_MIN_PREFIX_GROUPS = 3


@dataclass(frozen=True)
class RenderSettingProp:
    """One renderer setting.

    Attributes:
        name (str): Param name -- the attribute name with ':' replaced, since param
            paths are '.'-separated and a raw namespaced name reads badly as a label
        attr_name (str): The USD attribute name to author, e.g. "vray:cloud:upload"
        label (str): Display label
        sdf_type (str): Sdf type name, e.g. "string"
        default (Any): Fallback value from the schema, or None
        help (str): Documentation, used as the param tooltip
        options (tuple): Allowed values, making the param a Menu
        option_values (tuple): What each option authors, when that differs from the
            option itself -- an enum a renderer only describes as an int. Empty when
            the option is its own value, as an allowedTokens enum is.
    """

    name: str
    attr_name: str
    label: str = ""
    sdf_type: str = ""
    default: Any = None
    help: str = ""
    options: tuple = ()
    option_values: tuple = ()


@dataclass(frozen=True)
class RenderSettingsDef:
    """One renderer's settings, however they were declared.

    Attributes:
        key (str): Stable identifier -- the schema type name, "sdr:<identifier>", or
            "delegate:<plugin id>"
        label (str): Display name, e.g. "VRay"
        namespace (str): Attribute namespace this renderer owns, e.g. "vray"
        source (str): "schema", "sdr" or "delegate", for diagnostics only
        schema_name (str): Applied API schema to ApplyAPI, empty when the settings did
            not come from one
        props (List[RenderSettingProp]): The settings
    """

    key: str
    label: str = ""
    namespace: str = ""
    source: str = ""
    schema_name: str = ""
    props: list[RenderSettingProp] = field(default_factory=list)


def _label_from_schema_name(schema_name: str) -> str:
    for suffix in _LABEL_SUFFIXES:
        if schema_name.endswith(suffix):
            return schema_name[: -len(suffix)] or schema_name
    return schema_name


def _namespace_of(attr_names) -> str:
    """The namespace a set of attribute names share, by majority.

    A schema may carry the odd unnamespaced attribute; the namespace is whatever most
    of them agree on rather than whatever the first one happens to be.
    """
    counts: dict[str, int] = defaultdict(int)
    for name in attr_names:
        head, sep, _ = name.partition(":")
        if sep:
            counts[head] += 1
    return max(counts, key=counts.get) if counts else ""


def _sanitize_default(value):
    """Drop a default that only means something inside another application.

    A schema generated for Solaris carries Houdini expansions in its string defaults.
    Nothing here expands them, so keeping one would offer a path that renders to the
    literal text "${HIP}/render/${HIPNAME}.<frame04>.exr". Blank is the honest default:
    the setting is unset until someone fills it in, and because only values that differ
    from their default are authored, an untouched one stays out of the layer either way.
    """
    if isinstance(value, str) and _HOST_VARIABLE.search(value):
        return ""
    return value


def _prop_from_definition(prim_def, attr_name: str, namespace: str) -> RenderSettingProp | None:
    """Normalise one attribute of an applied API schema's prim definition."""
    spec = prim_def.GetSchemaAttributeSpec(attr_name)
    if spec is None:
        return None

    remainder = attr_name[len(namespace) + 1:] if namespace and attr_name.startswith(namespace + ":") else attr_name
    tokens = prim_def.GetPropertyMetadata(attr_name, "allowedTokens")

    return RenderSettingProp(
        name=attr_name.replace(":", "_"),
        attr_name=attr_name,
        label=_prettify(remainder),
        sdf_type=str(spec.typeName),
        default=_sanitize_default(prim_def.GetAttributeFallbackValue(attr_name)),
        help=prim_def.GetPropertyDocumentation(attr_name) or "",
        options=tuple(str(t) for t in (tokens or ())),
    )


def _schema_names_for_render_settings(prim_type: str = RENDER_SETTINGS_TYPE) -> list[str]:
    """Applied API schemas that may only be applied to a prim of prim_type.

    Read from plugin metadata rather than Tf.Type: the schema's library is not loaded
    until something uses it, so Tf.Type.FindByName on the plugin's type name comes back
    Unknown. The USD-side type name is the UsdSchemaBase alias.
    """
    names = []
    for plugin in Plug.Registry().GetAllPlugins():
        for type_name, info in (plugin.metadata.get("Types") or {}).items():
            if prim_type not in (info.get("apiSchemaCanOnlyApplyTo") or []):
                continue
            alias = (info.get("alias") or {}).get("UsdSchemaBase") or type_name
            names.append(alias)
    return sorted(set(names))


def _def_from_schema(schema_name: str) -> RenderSettingsDef | None:
    prim_def = Usd.SchemaRegistry().FindAppliedAPIPrimDefinition(schema_name)
    if prim_def is None:
        return None

    attr_names = [n for n in prim_def.GetPropertyNames()]
    if not attr_names:
        return None

    namespace = _namespace_of(attr_names)
    props = []
    for attr_name in attr_names:
        if prop := _prop_from_definition(prim_def, attr_name, namespace):
            props.append(prop)

    return RenderSettingsDef(
        key=schema_name,
        label=_label_from_schema_name(schema_name),
        namespace=namespace,
        source="schema",
        schema_name=schema_name,
        props=props,
    )


def _def_from_sdr(namespace: str, identifiers: tuple) -> RenderSettingsDef | None:
    """A settings def from an Sdr options node, for renderers that declare them there.

    RenderMan registers .args files describing its options into the shader registry
    rather than shipping a USD schema. The resulting attributes are still authored as
    ``<namespace>:<name>`` on the RenderSettings prim, so only discovery differs.
    """
    for identifier in identifiers:
        node = shader_defs.get_shader_def(identifier)
        if node is None:
            continue

        props = [
            RenderSettingProp(
                name=f"{namespace}_{p.name}".replace(":", "_"),
                attr_name=f"{namespace}:{p.name}",
                label=p.label or p.name,
                sdf_type=p.sdf_type,
                default=p.default,
                help=p.help,
                options=p.options,
            )
            for p in node.inputs
        ]
        if not props:
            continue

        return RenderSettingsDef(
            key=f"sdr:{identifier}",
            label=identifier,
            namespace=namespace,
            source="sdr",
            props=props,
        )
    return None


def authored_value(prop: RenderSettingProp, value):
    """What a param's current value should actually be written as.

    An option is normally its own value, so this is a no-op. It is not for an enum the
    renderer only described as an int: there the param holds a readable label and the
    attribute -- and the delegate -- want the number behind it.

    Args:
        prop: The render setting.
        value: The param's current value.

    Returns:
        The value to author, or None for a label that is not one of the options, which a
        caller skips rather than authoring something the renderer would reject.
    """
    if not prop.option_values:
        return value
    try:
        return prop.option_values[list(prop.options).index(value)]
    except ValueError:
        return None


def displayed_value(prop: RenderSettingProp, value):
    """What a param should show for a value the delegate holds: authored_value reversed.

    Args:
        prop: The render setting.
        value: The value as the delegate has it.

    Returns:
        The param value, or None for a number that is not one of the options.
    """
    if not prop.option_values:
        return value
    try:
        return prop.options[list(prop.option_values).index(value)]
    except ValueError:
        return None


def _labelled_default(pairs: tuple, default):
    """The label standing for a numeric default, or the first label if none matches."""
    for label, value in pairs:
        if value == default:
            return label
    return pairs[0][0]


def _normalised(label: str) -> str:
    """A renderer name reduced to what two spellings of it agree on.

    A delegate's display name and its schema's label are written by different people --
    "V-Ray" against VRayRendererSettingsAPI's "VRay" -- so they only match once the
    punctuation and case are gone.
    """
    return "".join(ch for ch in label.lower() if ch.isalnum())


def _def_from_delegate(plugin_id: str, label: str, settings) -> RenderSettingsDef | None:
    """A settings def from a loaded render delegate's setting descriptors.

    Cycles declares its settings in C++ and ships neither a schema nor .args, so the
    descriptors Hydra hands out at runtime are the only description of them that exists.
    They are already in the form this module wants: the key is the namespaced attribute
    name to author, and the name is the renderer's own label for it.

    The name is kept as the help text rather than as the label, because the label is
    rewritten by :func:`group_props` to drop whatever the enclosing groups already say.
    So the tooltip is where a renderer's own wording survives -- and for a delegate that
    is the only documentation there is, since a descriptor carries none.
    """
    namespace = _namespace_of([setting.key for setting in settings])

    # No namespace means these are not settings that belong on a RenderSettings prim.
    # hdOSPRay declares samplesPerFrame and maxDepth bare, which would sit in the same
    # namespace as UsdRender's own resolution and camera with nothing keeping the two
    # apart; hdGatling declares max-bounces, which Sdf refuses outright because a USD
    # property name cannot contain a hyphen. Neither delegate reads a RenderSettings
    # prim anyway -- an unnamespaced key is a sign the settings were only ever meant to
    # be set on the delegate directly.
    if not namespace:
        return None

    props = []
    for setting in settings:
        if not setting.key.startswith(namespace + ":"):
            continue
        sdf_type = _DELEGATE_SDF_TYPES.get(getattr(setting.type, "name", ""))
        if not sdf_type:
            continue
        default = _sanitize_default(setting.defValue)
        options: tuple = ()
        option_values: tuple = ()

        # An enum the descriptor flattened to an int, restored from the table above so
        # the param can be a menu of names rather than a slider over magic numbers.
        if pairs := _DELEGATE_ENUMS.get(setting.key):
            options = tuple(label for label, _value in pairs)
            option_values = tuple(value for _label, value in pairs)
            default = _labelled_default(pairs, default)

        props.append(
            RenderSettingProp(
                name=setting.key.replace(":", "_"),
                attr_name=setting.key,
                label=setting.name or _prettify(setting.key[len(namespace) + 1:]),
                sdf_type=sdf_type,
                default=default,
                help=setting.name or "",
                options=options,
                option_values=option_values,
            )
        )

    if not props:
        return None

    return RenderSettingsDef(
        key=f"delegate:{plugin_id}",
        label=label,
        namespace=namespace,
        source="delegate",
        props=props,
    )


def _host_owns_a_viewport() -> bool:
    """Whether this process is a GUI host with a render engine of its own.

    A running QApplication is the honest proxy: usdview, Kraken and every other host this
    plugs into have one, and a batch script or a test does not. Overridable with
    USD_CHUNKS_PROBE_DELEGATES=1 for anyone who needs the delegate-declared settings badly
    enough to risk it, or =0 to refuse the probe outright.
    """
    override = os.environ.get("USD_CHUNKS_PROBE_DELEGATES")
    if override is not None:
        return override.strip() not in ("1", "true", "yes", "on")
    # Looked up rather than imported: a QApplication can only exist if the host has already
    # imported Qt, and a batch script has no reason to load it just to learn it is absent.
    QtWidgets = sys.modules.get("PySide6.QtWidgets")
    if QtWidgets is None:
        return False
    return QtWidgets.QApplication.instance() is not None


def _probe_refused() -> bool:
    """Whether USD_CHUNKS_PROBE_DELEGATES says no delegate is to be asked at all."""
    override = os.environ.get("USD_CHUNKS_PROBE_DELEGATES")
    return override is not None and override.strip() not in ("1", "true", "yes", "on")


def _can_probe_out_of_process() -> bool:
    """Whether this process can run discovery in a child Python of its own.

    It needs an interpreter to run it with: sys.executable is one under usdview and a
    script, but the host application itself under Blender, Houdini and the like.
    """
    return os.path.basename(sys.executable or "").lower().startswith("python")


#: How long the child process may take. Cycles' delegate costs a minute the first time it is
#: loaded on a cold machine.
_PROBE_TIMEOUT_S = 300

#: Run by the child process: delegate discovery with loading allowed, pickled to argv[1].
_PROBE_SCRIPT = """\
import pickle, sys
from usd_chunks import render_defs
found = render_defs._defs_from_delegates(render_defs._defs_without_delegates())
with open(sys.argv[1], "wb") as f:
    pickle.dump(tuple(found), f, protocol=pickle.HIGHEST_PROTOCOL)
"""


def _defs_from_delegates_out_of_process(existing) -> list[RenderSettingsDef]:
    """Delegate-declared settings, read by a child process rather than this one.

    A host must not load a delegate itself (see _defs_from_delegates), yet it is where the
    settings are wanted most: a renderer that describes itself only through its delegate
    (hdCycles, hdAurora) otherwise has no group in usdview at all. The child loads them
    where a crash costs nothing and hands back what it found. Its output is captured, as
    V-Ray logs to stdout, and only reaches the debug log when the probe fails.

    The answer is cached on disk with the rest (_discover), so the cost -- a few seconds,
    a minute for a cold Cycles -- is paid once per USD, plugin set and code change.
    """
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, USD_CHUNKS_PROBE_DELEGATES="1")
    # The child imports this package from where this process did, whatever its PYTHONPATH.
    env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH")) if p)

    fd, out_path = tempfile.mkstemp(prefix="render_settings_probe-", suffix=".pickle")
    os.close(fd)
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PROBE_SCRIPT, out_path],
            env=env,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            logger.debug(
                "Render settings probe exited with %s:\n%s",
                result.returncode, (result.stderr or result.stdout)[-2000:],
            )
            return []
        found = _read_disk_cache(out_path)
    except Exception as exc:  # noqa: BLE001 - a timeout, no interpreter: only a miss
        logger.debug("Render settings probe failed: %s", exc)
        return []
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    claimed = {d.namespace for d in existing if d.namespace}
    return [d for d in found or () if d.source == "delegate" and d.namespace not in claimed]


def _defs_from_delegates(existing) -> list[RenderSettingsDef]:
    """Ask every render delegate not already described for its setting descriptors.

    Loading a delegate is the expensive part -- V-Ray's takes two seconds to bring up its
    AppSDK and start a session, and Cycles' costs a minute the first time it is asked on
    a cold machine, against a fraction of a second once its libraries are resident -- so
    a renderer whose settings a schema or Sdr already described is skipped by name before
    it is loaded. When the two spellings of a name do not match, the load happens anyway
    and the duplicate is dropped afterwards on its namespace, which costs time but not
    correctness.

    Every failure here is one renderer's, not the whole discovery's: a delegate that
    cannot be loaded at all is normal rather than exceptional. Storm refuses outright in
    a headless process because it needs a GL context that is not there, which is exactly
    right -- Storm has no settings to author on a RenderSettings prim either way.
    """
    if UsdImagingGL is None:
        return []
    if _host_owns_a_viewport():
        # A second engine in a process that already has one takes the host down with it:
        # usdview segfaults (0xC0000005) the moment this loads hdCycles, and every scene
        # holding a Render Settings chunk therefore fails to open at all. Nothing here is
        # worth that, so in a host the delegates are asked by a child process instead.
        if _probe_refused() or not _can_probe_out_of_process():
            logger.info(
                "Render settings: not loading render delegates inside a host that owns a "
                "viewport -- only schema- and Sdr-declared renderers are offered here."
            )
            return []
        return _defs_from_delegates_out_of_process(existing)

    described = {_normalised(d.label) for d in existing}
    claimed = {d.namespace for d in existing if d.namespace}

    engine = UsdImagingGL.Engine()
    found = []
    for plugin_id in UsdImagingGL.Engine.GetRendererPlugins():
        label = UsdImagingGL.Engine.GetRendererDisplayName(plugin_id) or plugin_id
        if _normalised(label) in described:
            continue

        try:
            if not engine.SetRendererPlugin(plugin_id):
                continue
            settings = engine.GetRendererSettingsList()
        except Exception as exc:  # noqa: BLE001 - a delegate that will not load here
            logger.debug("Could not read render settings from %s: %s", plugin_id, exc)
            continue

        definition = _def_from_delegate(plugin_id, label, settings)
        if definition and definition.namespace not in claimed:
            claimed.add(definition.namespace)
            found.append(definition)

    return found


#: Bumped when RenderSettingsDef or RenderSettingProp change shape, so a cache written by
#: an older layout is ignored rather than unpickled into the wrong fields.
_DISK_CACHE_FORMAT = 1

#: A cache file nobody has read for this long is removed when another is written.
_DISK_CACHE_MAX_IDLE = 30 * 24 * 3600


def _disk_cache_dir() -> str | None:
    """Where discovered definitions are kept between processes, or None for nowhere.

    USD_CHUNKS_CACHE_DIR overrides it, and set empty turns the cache off.
    """
    override = os.environ.get("USD_CHUNKS_CACHE_DIR")
    if override is not None:
        return override or None
    return user_cache_dir()


def _mtime(path: str) -> int:
    """A file's modification time, or 0 when it cannot be read."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def _disk_cache_key() -> str:
    """Everything the discovered definitions depend on, hashed.

    USD's version, and every plugin with the path it loads from -- a renderer installed,
    removed or upgraded changes that list, and a V-Ray build's path names its version --
    and that file's modification time, since a delegate rebuilt in place (hdAurora from
    source) can declare different settings under the same path.
    Then whether this process may load delegates at all, which decides whether they are
    in the answer, and the two modules' own files, so a change to how definitions are
    built is never answered from a cache the old code wrote.
    """
    plugins = sorted((p.name, p.path, _mtime(p.path)) for p in Plug.Registry().GetAllPlugins())
    sources = []
    for module_file in (__file__, shader_defs.__file__):
        try:
            stat = os.stat(module_file)
            sources.append((module_file, stat.st_mtime_ns, stat.st_size))
        except OSError:
            sources.append((module_file, 0, 0))
    parts = (
        _DISK_CACHE_FORMAT,
        Usd.GetVersion(),
        sys.version_info[:2],
        plugins,
        UsdImagingGL is not None
        and not _probe_refused()
        and (not _host_owns_a_viewport() or _can_probe_out_of_process()),
        sources,
    )
    return hashlib.sha1(repr(parts).encode()).hexdigest()


def _read_disk_cache(path: str) -> tuple | None:
    try:
        with open(path, "rb") as f:
            found = pickle.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - a torn or foreign file is only a miss
        logger.debug("Ignoring unreadable render settings cache %s: %s", path, exc)
        return None
    if not isinstance(found, tuple) or not all(isinstance(d, RenderSettingsDef) for d in found):
        return None
    try:
        os.utime(path)  # still in use: see _write_disk_cache's clean-up
    except OSError:
        pass
    return found


def _write_disk_cache(path: str, found: tuple) -> None:
    """Keep found for the next process, or quietly don't.

    Written beside its final name and moved into place, so a process reading it at the
    same moment finds the old file or the new one, never half of one.
    """
    try:
        data = pickle.dumps(found, protocol=pickle.HIGHEST_PROTOCOL)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except Exception as exc:  # noqa: BLE001 - a default that will not pickle, a read-only home
        logger.debug("Could not write render settings cache %s: %s", path, exc)
        return
    # Only files nobody has read for a month go. A different key is not proof the old file
    # is dead: two checkouts, two worktrees, or usdview beside a CLI from another clone each
    # have their own key, and removing every other file on write made them delete each
    # other's on every run -- each paying the whole discovery again, every time.
    cache_dir, name = os.path.split(path)
    cutoff = time.time() - _DISK_CACHE_MAX_IDLE
    for other in os.listdir(cache_dir):
        if other != name and other.startswith("render_settings-") and other.endswith(".pickle"):
            other_path = os.path.join(cache_dir, other)
            try:
                if os.path.getmtime(other_path) < cutoff:
                    os.unlink(other_path)
            except OSError:
                pass


@lru_cache(maxsize=1)
def _discover() -> tuple:
    """Read every renderer's settings once per process, and from disk when unchanged.

    The first read was two seconds of every process that opened a scene with a Render
    Settings chunk in it, and almost none of it is this module's work: asking Sdr for
    RenderMan's options node is Sdr's first touch, which discovers every shader every
    plugin publishes -- whether or not RenderMan is installed at all. The answer only
    changes when USD, a plugin or this code does, and those are what the file is keyed
    on (_disk_cache_key), so a later process reads it back instead.
    """
    cache_dir = _disk_cache_dir()
    path = None
    if cache_dir:
        try:
            path = os.path.join(cache_dir, f"render_settings-{_disk_cache_key()}.pickle")
        except Exception as exc:  # noqa: BLE001 - never let the cache stop discovery
            logger.debug("Could not key the render settings cache: %s", exc)
        if path and (found := _read_disk_cache(path)) is not None:
            return found

    found = _discover_uncached()
    if path:
        _write_disk_cache(path, found)
    return found


def _discover_uncached() -> tuple:
    """Read every renderer's settings.

    Cached because building a definition is not cheap -- V-Ray's is 258 attributes, each
    needing a spec, a fallback and its documentation out of the schema registry -- and it
    is asked for repeatedly: once to populate the delegate menu, again per chunk-menu
    variant, and again on every cook to decide what to author. The registries this reads
    are fixed for the life of the process, so re-reading them only ever returns the same
    answer more slowly.

    More so once delegates are in the mix, since asking one costs loading it. Caching is
    what keeps that to a single load per renderer for the life of the process rather than
    one per cook.
    """
    found = _defs_without_delegates()

    # Last, because it is the only source that has to load anything to answer, and
    # because what the other two already found is what lets it skip most of that work.
    try:
        found.extend(_defs_from_delegates(found))
    except Exception as exc:  # noqa: BLE001 - no imaging, no GL, no delegates at all
        logger.debug("Could not read render settings from any delegate: %s", exc)

    return tuple(found)


def _defs_without_delegates() -> list[RenderSettingsDef]:
    """The renderers described by a schema or by Sdr -- everything that loads nothing."""
    found = []

    for schema_name in _schema_names_for_render_settings():
        try:
            if definition := _def_from_schema(schema_name):
                found.append(definition)
        except Exception as exc:  # noqa: BLE001 - a schema this USD cannot read
            logger.debug("Could not read render settings schema %s: %s", schema_name, exc)

    for namespace, identifiers in _SDR_OPTION_NODES.items():
        try:
            if definition := _def_from_sdr(namespace, identifiers):
                found.append(definition)
        except Exception as exc:  # noqa: BLE001 - registry shapes vary by USD version
            logger.debug("Could not read Sdr render settings for %s: %s", namespace, exc)

    return found


@lru_cache(maxsize=1)
def _discover_camera() -> tuple:
    """Read every renderer's camera settings, once per process.

    Schemas only. Sdr and the delegates describe render settings, never a camera's, and
    the schema registry is already loaded, so this needs neither the disk cache nor the
    out-of-process probe that _discover has.
    """
    found = []
    for schema_name in _schema_names_for_render_settings(CAMERA_TYPE):
        try:
            if definition := _def_from_schema(schema_name):
                found.append(definition)
        except Exception as exc:  # noqa: BLE001 - a schema this USD cannot read
            logger.debug("Could not read camera settings schema %s: %s", schema_name, exc)
    return tuple(found)


def clear_cache() -> None:
    """Forget the discovered definitions, for a test that registers a renderer late."""
    _discover.cache_clear()
    _discover_camera.cache_clear()


def iter_camera_defs() -> Iterator[RenderSettingsDef]:
    """Every renderer whose camera settings this USD can describe."""
    yield from _discover_camera()


def iter_render_settings_defs() -> Iterator[RenderSettingsDef]:
    """Every renderer whose settings this USD can describe, schema-declared first."""
    yield from _discover()


def get_render_settings_def(key: str) -> RenderSettingsDef | None:
    """The def with this key, or None when the renderer is not installed here."""
    for definition in iter_render_settings_defs():
        if definition.key == key:
            return definition
    return None


def def_for_plugin(plugin_id: str) -> RenderSettingsDef | None:
    """The settings def belonging to a Hydra plugin id, or None when it has none.

    Matched on the key first, since a def read from a delegate is keyed by the very
    plugin id being asked about. A def that came from a schema instead has no plugin id
    in it, so that one falls back to comparing display names -- the same normalised
    comparison discovery uses to avoid loading a renderer twice.

    Used to send one renderer's settings without sending every renderer's: a delegate
    redraws for each setting it is handed and discards the namespaces it does not own.

    Args:
        plugin_id: The Hydra renderer plugin id.

    Returns:
        The matching def, or None.
    """
    for definition in iter_render_settings_defs():
        if definition.key == f"delegate:{plugin_id}":
            return definition

    label = ""
    if UsdImagingGL is not None:
        try:
            label = UsdImagingGL.Engine.GetRendererDisplayName(plugin_id) or ""
        except Exception as exc:  # noqa: BLE001 - an id this build does not know
            logger.debug("Could not resolve a display name for %s: %s", plugin_id, exc)

    if not label:
        return None

    wanted = _normalised(label)
    for definition in iter_render_settings_defs():
        if _normalised(definition.label) == wanted:
            return definition
    return None


def available_delegates() -> list[str]:
    """Labels of every renderer with describable settings, for a menu."""
    return [d.label for d in iter_render_settings_defs()]


def _prettify(name: str) -> str:
    """A settings name as a label: separators become spaces."""
    return name.replace(":", " ").replace("_", " ").strip()


def _split_segment(remainder: str):
    """Leading name segment and what is left, or (None, remainder) when indivisible.

    ':' before '_': a renderer that namespaces a setting has already said where it
    belongs, so that split is meant, where an underscore is only a convention.
    """
    for separator in (":", "_"):
        if separator not in remainder:
            continue

        head, _, rest = remainder.partition(separator)
        if not head or not rest:
            continue

        if head.lower() in _QUALIFIER_SEGMENTS:
            subject, _, _tail = rest.partition(separator)
            if subject:
                # The whole name stays as the label: inside a "denoise" group the switch
                # reads "use denoise", which is what distinguishes it from "use gpu"
                # sitting beside it.
                return subject, remainder

        return head, rest
    return None, remainder


def _bucket(items):
    """One level of grouping over (prop, remaining name) pairs."""
    counts: dict[str, int] = defaultdict(int)
    for _prop, remainder in items:
        head, _rest = _split_segment(remainder)
        if head:
            counts[head] += 1

    buckets: OrderedDict[str, list] = OrderedDict()
    for prop, remainder in items:
        head, rest = _split_segment(remainder)
        if head and counts[head] >= _MIN_GROUP_SIZE:
            buckets.setdefault(head, []).append((prop, rest))
        else:
            buckets.setdefault(_GENERAL_GROUP, []).append((prop, remainder))
    return buckets


def _divides_well(sub, total: int) -> bool:
    """Whether a split earned its nesting.

    Two ways it does not: everything lands in one bucket (Foo > Foo), or most of it
    falls through to leftovers and the nesting only buries a few settings one level
    deeper than the rest.
    """
    if len(sub) <= 1:
        return False

    real = sum(
        (len(v) if isinstance(v, list) else _count(v))
        for name, v in sub.items()
        if name != _GENERAL_GROUP
    )
    return real >= total * _MIN_SPLIT_COVERAGE


def _count(node) -> int:
    return sum(len(v) if isinstance(v, list) else _count(v) for v in node.values())


def _factor_camel_prefixes(grouped):
    """Fold groups sharing a leading CamelCase word under that word.

    V-Ray names its settings after the plugin they configure, so twenty-one of the
    thirty-three top-level groups begin with "Settings" -- SettingsCamera,
    SettingsCameraDof, SettingsImageSampler. That prefix is a level of hierarchy the
    names just do not put a separator in front of, and reading it as one gives
    Settings > Camera rather than a column of near-identical siblings.
    """
    counts: dict[str, int] = defaultdict(int)
    for name in grouped:
        if name == _GENERAL_GROUP:
            continue
        if match := _CAMEL_PREFIX.match(name):
            counts[match.group(1)] += 1

    factorable = {prefix for prefix, count in counts.items() if count >= _MIN_PREFIX_GROUPS}
    if not factorable:
        return grouped

    folded: OrderedDict[str, Any] = OrderedDict()
    for name, contents in grouped.items():
        match = _CAMEL_PREFIX.match(name) if name != _GENERAL_GROUP else None
        prefix = match.group(1) if match else None

        # A group already named exactly the prefix keeps it: folding siblings into it
        # would mix two different things under one heading.
        if prefix in factorable and not isinstance(folded.get(prefix), list):
            folded.setdefault(prefix, OrderedDict())[name[len(prefix):]] = contents
        else:
            folded[name] = contents

    if _GENERAL_GROUP in folded:
        folded.move_to_end(_GENERAL_GROUP)
    return folded


def _tree(items, depth: int):
    grouped: OrderedDict[str, Any] = OrderedDict()

    for name, members in _bucket(items).items():
        split_further = (
            name != _GENERAL_GROUP
            and depth < _MAX_DEPTH
            and len(members) > _MAX_FLAT_GROUP
        )
        if split_further:
            sub = _tree(members, depth + 1)
            # Everything shared the next segment too, so this level says nothing the
            # next one does not: V-Ray's IPR overrides are all vray:settings:viewport:*,
            # and a "settings" group holding only "viewport" sat flat beside the real
            # Settings with every label starting "viewport Settings...". The deeper name
            # is the specific one, so it takes this group's place -- unless a sibling
            # already has it.
            only = next(iter(sub)) if len(sub) == 1 else None
            if only not in (None, _GENERAL_GROUP) and only not in grouped and only != name:
                grouped[only] = sub[only]
                continue
            if _divides_well(sub, len(members)):
                grouped[name] = sub
                continue

        # Relabel to what the grouping did not already say. A setting sitting in a
        # MotionBlur group is "bias", not "SettingsMotionBlur bias" -- the path is the
        # groups it is nested in, and repeating it in every label just makes the labels
        # too wide to read.
        grouped[name] = [replace(prop, label=_prettify(rest)) for prop, rest in members]

    # General last: it is the leftovers, not the headline.
    if _GENERAL_GROUP in grouped:
        grouped.move_to_end(_GENERAL_GROUP)
    return _factor_camel_prefixes(grouped)


def group_props(props) -> OrderedDict[str, Any]:
    """Bucket settings into nested collapsible groups, the way a renderer's own UI does.

    Setting names are structured -- ``vray:SettingsImageSampler_progressive_maxSubdivs``
    names a plugin, a section within it, and the setting -- so grouping just follows the
    segments. A segment earns a group once a couple of settings share it, and a group
    that is still too big to read is split again on the next segment, up to a few levels
    deep. Anything left over lands in General, which sorts last.

    Args:
        props: The render settings.

    Returns:
        An ordered mapping of group name to either a list of settings or a nested mapping of
        the same shape, so a caller renders it by recursing rather than by knowing any
        renderer's layout.
    """
    return _tree([(p, p.attr_name.partition(":")[2] or p.attr_name) for p in props], 0)


def param_for_setting(prop: RenderSettingProp):
    """Build the chunk param that edits one setting, or None when unmapped.

    Delegates to shader_defs.param_for_prop rather than repeating its Sdf-type-to-param
    table: a render setting and a shader input are the same problem once normalised.

    Args:
        prop: The render setting.

    Returns:
        The param, or None.
    """
    return shader_defs.param_for_prop(
        shader_defs.ShaderProp(
            name=prop.name,
            label=prop.label,
            sdf_type=prop.sdf_type,
            default=prop.default,
            connectable=False,
            help=prop.help,
            options=prop.options,
        )
    )
