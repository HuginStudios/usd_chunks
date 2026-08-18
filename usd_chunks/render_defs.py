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

Both are read here and returned as the same :class:`RenderSettingsDef`, so a chunk never
learns which renderer or which mechanism produced a setting. Nothing here imports Qt.

``` py
for d in iter_render_settings_defs():
    print(d.label, d.namespace, len(d.props))

d = get_render_settings_def("VRayRendererSettingsAPI")
for group, props in group_props(d.props).items():
    print(group, [p.name for p in props])
```
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterator, List, Optional

from pxr import Plug, Usd

from usd_chunks import shader_defs

logger = logging.getLogger(__name__)

#: Prim type these settings attach to. Applied API schemas advertise this in plugInfo.
RENDER_SETTINGS_TYPE = "RenderSettings"

#: Suffixes stripped from a schema type name to get a display label, longest first so
#: "RendererSettingsAPI" wins over "SettingsAPI".
_LABEL_SUFFIXES = ("RendererSettingsAPI", "RenderSettingsAPI", "SettingsAPI", "API")

#: Sdr identifiers that describe a renderer's options rather than a shader, by namespace.
#: RenderMan declares its settings this way instead of as a USD schema. Unverified --
#: this machine has no RenderMan install -- so treat it as the seam to extend rather
#: than a tested mapping.
_SDR_OPTION_NODES: Dict[str, tuple] = {
    "ri": ("PRManOptions", "PRManGlobalOptions"),
}

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
        options (tuple): Allowed token values, making the param a Menu
    """

    name: str
    attr_name: str
    label: str = ""
    sdf_type: str = ""
    default: Any = None
    help: str = ""
    options: tuple = ()


@dataclass(frozen=True)
class RenderSettingsDef:
    """One renderer's settings, however they were declared.

    Attributes:
        key (str): Stable identifier -- the schema type name, or "sdr:<identifier>"
        label (str): Display name, e.g. "VRay"
        namespace (str): Attribute namespace this renderer owns, e.g. "vray"
        source (str): "schema" or "sdr", for diagnostics only
        schema_name (str): Applied API schema to ApplyAPI, empty for Sdr-sourced defs
        props (List[RenderSettingProp]): The settings
    """

    key: str
    label: str = ""
    namespace: str = ""
    source: str = ""
    schema_name: str = ""
    props: List[RenderSettingProp] = field(default_factory=list)


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
    counts: Dict[str, int] = defaultdict(int)
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


def _prop_from_definition(prim_def, attr_name: str, namespace: str) -> Optional[RenderSettingProp]:
    """Normalise one attribute of an applied API schema's prim definition."""
    spec = prim_def.GetSchemaAttributeSpec(attr_name)
    if spec is None:
        return None

    remainder = attr_name[len(namespace) + 1:] if namespace and attr_name.startswith(namespace + ":") else attr_name
    tokens = prim_def.GetPropertyMetadata(attr_name, "allowedTokens")

    return RenderSettingProp(
        name=attr_name.replace(":", "_"),
        attr_name=attr_name,
        label=remainder.replace(":", " ").replace("_", " "),
        sdf_type=str(spec.typeName),
        default=_sanitize_default(prim_def.GetAttributeFallbackValue(attr_name)),
        help=prim_def.GetPropertyDocumentation(attr_name) or "",
        options=tuple(str(t) for t in (tokens or ())),
    )


def _schema_names_for_render_settings() -> List[str]:
    """Applied API schemas that may only be applied to a RenderSettings prim.

    Read from plugin metadata rather than Tf.Type: the schema's library is not loaded
    until something uses it, so Tf.Type.FindByName on the plugin's type name comes back
    Unknown. The USD-side type name is the UsdSchemaBase alias.
    """
    names = []
    for plugin in Plug.Registry().GetAllPlugins():
        for type_name, info in (plugin.metadata.get("Types") or {}).items():
            if RENDER_SETTINGS_TYPE not in (info.get("apiSchemaCanOnlyApplyTo") or []):
                continue
            alias = (info.get("alias") or {}).get("UsdSchemaBase") or type_name
            names.append(alias)
    return sorted(set(names))


def _def_from_schema(schema_name: str) -> Optional[RenderSettingsDef]:
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


def _def_from_sdr(namespace: str, identifiers: tuple) -> Optional[RenderSettingsDef]:
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


@lru_cache(maxsize=1)
def _discover() -> tuple:
    """Read every renderer's settings once per process.

    Cached because building a definition is not cheap -- V-Ray's is 258 attributes, each
    needing a spec, a fallback and its documentation out of the schema registry -- and it
    is asked for repeatedly: once to populate the delegate menu, again per chunk-menu
    variant, and again on every cook to decide what to author. The registries this reads
    are fixed for the life of the process, so re-reading them only ever returns the same
    answer more slowly.
    """
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

    return tuple(found)


def clear_cache() -> None:
    """Forget the discovered definitions, for a test that registers a renderer late."""
    _discover.cache_clear()


def iter_render_settings_defs() -> Iterator[RenderSettingsDef]:
    """Every renderer whose settings this USD can describe, schema-declared first."""
    yield from _discover()


def get_render_settings_def(key: str) -> Optional[RenderSettingsDef]:
    """The def with this key, or None when the renderer is not installed here."""
    for definition in iter_render_settings_defs():
        if definition.key == key:
            return definition
    return None


def available_delegates() -> List[str]:
    """Labels of every renderer with describable settings, for a menu."""
    return [d.label for d in iter_render_settings_defs()]


def _split_segment(remainder: str):
    """Leading name segment and what is left, or (None, remainder) when indivisible.

    ':' before '_': a renderer that namespaces a setting has already said where it
    belongs, so that split is meant, where an underscore is only a convention.
    """
    for separator in (":", "_"):
        if separator in remainder:
            head, _, rest = remainder.partition(separator)
            if head and rest:
                return head, rest
    return None, remainder


def _bucket(items):
    """One level of grouping over (prop, remaining name) pairs."""
    counts: Dict[str, int] = defaultdict(int)
    for _prop, remainder in items:
        head, _rest = _split_segment(remainder)
        if head:
            counts[head] += 1

    buckets: "OrderedDict[str, list]" = OrderedDict()
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


def _tree(items, depth: int):
    grouped: "OrderedDict[str, Any]" = OrderedDict()

    for name, members in _bucket(items).items():
        split_further = (
            name != _GENERAL_GROUP
            and depth < _MAX_DEPTH
            and len(members) > _MAX_FLAT_GROUP
        )
        if split_further:
            sub = _tree(members, depth + 1)
            if _divides_well(sub, len(members)):
                grouped[name] = sub
                continue

        grouped[name] = [prop for prop, _rest in members]

    # General last: it is the leftovers, not the headline.
    if _GENERAL_GROUP in grouped:
        grouped.move_to_end(_GENERAL_GROUP)
    return grouped


def group_props(props) -> "OrderedDict[str, Any]":
    """Bucket settings into nested collapsible groups, the way a renderer's own UI does.

    Setting names are structured -- ``vray:SettingsImageSampler_progressive_maxSubdivs``
    names a plugin, a section within it, and the setting -- so grouping just follows the
    segments. A segment earns a group once a couple of settings share it, and a group
    that is still too big to read is split again on the next segment, up to a few levels
    deep. Anything left over lands in General, which sorts last.

    Returns an ordered mapping of group name to either a list of settings or a nested
    mapping of the same shape, so a caller renders it by recursing rather than by
    knowing any renderer's layout.
    """
    return _tree([(p, p.attr_name.partition(":")[2] or p.attr_name) for p in props], 0)


def param_for_setting(prop: RenderSettingProp):
    """Build the chunk param that edits one setting, or None when unmapped.

    Delegates to shader_defs.param_for_prop rather than repeating its Sdf-type-to-param
    table: a render setting and a shader input are the same problem once normalised.
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
