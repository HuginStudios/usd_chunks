"""The files a chunk's params name: resolved the way USD resolves them, and kept portable.

A relative path in a param is a search path, as in USD's default resolver: written without
`./` or `../`, it is looked for in the folder of the entry USD -- the network file -- since
a stage opened from a file searches its folder (ArDefaultResolver's default context). That
holds whatever the working directory is, so a saved network runs from anywhere.

A `./` or `../` path is different. USD anchors it to the layer that authors it, and a chunk
authors into its own anonymous layer, which has no folder: it falls back to the working
directory, and the network then works only when run from there.

So after each cook a chunk's input files are checked, and each problem is logged once per
chunk and path: an absolute path (the network is not portable), a `./` or `../` path (it
follows the working directory), and a path that does not resolve (named with where it was
looked for, rather than left to USD's own warning).

On save, absolute paths under the network's folder are written relative to it, as search
paths, unless the network turns that off (Stage Settings' Relative File Paths).
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

from pxr import Ar, Sdf, Vt

from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

#: A URI scheme ("omniverse://", "https:"). One letter is a Windows drive, not a scheme.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")

#: (chunk name, path, problem) already logged, so a recook does not repeat it.
_warned: set[tuple[str, str, str]] = set()

#: The Stage Settings param that turns rewriting on save off.
RELATIVE_PATHS_PARAM = "relative_paths"


def is_input(param) -> bool:
    """True for a file param whose file the chunk reads (FILE_EXISTING, or `reads`)."""
    if not hasattr(param, "get_type") or param.get_type() != ChunkFlags.ParamType.FILE:
        return False
    return bool(getattr(param, "reads", False)) or param.has_flag(ChunkFlags.ParamFlags.FILE_EXISTING)


def kind(value) -> str | None:
    """How a param value names a file: "absolute", "dot" (./ or ../) or "search".

    None for what is not one literal path: empty, an expression (`@Node.param@`, USD's
    `${VAR}` or a backticked expression), a URI, a `#` frame template, a glob or a
    `<UDIM>` tile pattern. Those are left to whatever reads them.
    """
    if isinstance(value, Sdf.AssetPath):
        value = value.path
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(c in value for c in "@`$#*?[<"):
        return None
    if _SCHEME.match(value):
        return None
    if os.path.isabs(value):
        return "absolute"
    if value.startswith(("./", "../", ".\\", "..\\")) or value in (".", ".."):
        return "dot"
    return "search"


def anchor(value: str, stage=None) -> str:
    """value as a chunk authors it: a ./ or ../ path anchored to the network's folder.

    In a USD file a ./ or ../ path is relative to that file. A param's value is saved in
    the network file, so it is relative to the network; but a chunk authors it into its own
    anonymous layer, which would anchor it to the working directory instead. Anchored here
    it names the file it does in the network: as a search path where that sits under the
    network's folder, else absolute. Anything else, or an unsaved network, is left as is.
    """
    if isinstance(value, Sdf.AssetPath):
        value = value.path
    if kind(value) != "dot":
        return value
    folder = network_folder(stage)
    if not folder:
        return value
    return relative_to(folder, os.path.normpath(os.path.join(folder, value.strip())))


def resolve(value: str, stage=None) -> str:
    """Where USD finds value, under stage's resolver context; "" when it finds nothing.

    A search path is looked for in the context's folders -- the entry USD's, for a stage
    opened from a file -- and then the working directory. A ./ or ../ path is anchored to
    the network's folder first (anchor).
    """
    if isinstance(value, Sdf.AssetPath):
        value = value.path
    value = anchor(value, stage)
    context = stage.GetPathResolverContext() if stage is not None else Ar.ResolverContext()
    with Ar.ResolverContextBinder(context):
        resolver = Ar.GetResolver()
        found = str(resolver.Resolve(resolver.CreateIdentifier(value)))
    return found or (os.path.abspath(value) if os.path.exists(value) else "")


def search_folders(stage=None) -> list[str]:
    """The folders a search path is looked for in: the context's, then the working directory."""
    folders = []
    if stage is not None:
        for context in stage.GetPathResolverContext().Get():
            if isinstance(context, Ar.DefaultResolverContext):
                folders.extend(context.GetSearchPath())
    folders.append(os.getcwd())
    return folders


def network_folder(stage) -> str:
    """The folder of the stage's entry USD: its root layer's file, or for a network not saved
    yet, the folder its resolver context searches (Scene.new(path=...)); "" with neither."""
    if stage is None:
        return ""
    root = stage.GetRootLayer()
    if root is not None and not root.anonymous and root.realPath:
        return os.path.dirname(root.realPath)
    for context in stage.GetPathResolverContext().Get():
        if isinstance(context, Ar.DefaultResolverContext) and context.GetSearchPath():
            return context.GetSearchPath()[0]
    return ""


def input_params(chunk) -> Iterator:
    """Every input file param of chunk, one per row in a list."""
    stack = list(chunk.get_my_params())
    while stack:
        param = stack.pop()
        if hasattr(param, "proxy_params"):  # a list: each row's params
            for row in param.proxy_params:
                stack.extend(row.get_my_params())
        elif is_input(param):
            yield param
        elif hasattr(param, "get_my_params"):  # a group
            stack.extend(param.get_my_params())


def check(chunk) -> None:
    """Log what is wrong with the files chunk reads, once per chunk and path. See the module."""
    stage = chunk.get_stage() if hasattr(chunk, "get_stage") else None
    for param in input_params(chunk):
        value = param.value
        if isinstance(value, Sdf.AssetPath):
            value = value.path
        how = kind(value)
        if how is None:
            continue
        if how == "absolute":
            if os.path.exists(value):
                folder = network_folder(stage)
                scene = chunk.get_scene() if hasattr(chunk, "get_scene") else None
                if folder and relative_to(folder, value) != value and scene and relative_paths_on(scene):
                    continue  # saved relative to the network anyway
                _warn_once(
                    chunk, value, "absolute",
                    "%s: %s is an absolute path, so the network works only where that file is. "
                    "Give it relative to the network's folder (%s), without ./ or ../, to keep "
                    "the network portable.",
                    chunk.name, value, folder or "where the network is saved",
                )
            else:
                _warn_once(chunk, value, "missing", "%s: %s does not exist.", chunk.name, value)
        elif how == "dot" and not network_folder(stage):
            _warn_once(
                chunk, value, "dot",
                "%s: %s is relative to the network's file, which is not saved yet, so for now "
                "it is found from the working directory (%s). Scene.new(path=...) gives it the "
                "network's folder from the start.",
                chunk.name, value, os.getcwd(),
            )
        elif not resolve(value, stage):
            _warn_once(
                chunk, value, "missing",
                "%s: %s was not found. Looked in %s.",
                chunk.name, value, ", ".join(search_folders(stage)),
            )


def _warn_once(chunk, path: str, problem: str, message: str, *args) -> None:
    key = (chunk.name, path, problem)
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def relative_paths_on(scene) -> bool:
    """Whether saving writes input paths relative to the network: Stage Settings' switch.

    On unless a chunk in the network has the switch and has it off.
    """
    for chunk in scene.get_chunks():
        param = chunk.get_param(RELATIVE_PATHS_PARAM) if hasattr(chunk, "get_param") else None
        if param is not None and param.value is False:
            return False
    return True


def relative_to(folder: str, value: str) -> str:
    """value as a search path from folder, if it is an absolute path under it; else value."""
    if not folder or kind(value) != "absolute":
        return value
    try:
        rel = Path(value).resolve().relative_to(Path(folder).resolve())
    except ValueError:  # outside the folder, or on another drive: no search path reaches it
        return value
    return rel.as_posix()


def refit(value: str, stage, folder: str, make_relative: bool) -> str:
    """value as a file in folder writes it, to name the file it names in the network.

    A search path found from the network's folder is written as found from folder: a search
    path where the file sits under folder, absolute where no search path reaches it. A ./ or
    ../ path is anchored to the network's folder the same way. With make_relative, an
    absolute path under folder becomes a search path too. A path that names no file, or a
    folder that is the network's own, leaves value as it is.

    Args:
        value: The path as the network has it.
        stage: The network's stage, for its folder and resolver context.
        folder: The folder of the file value is written into.
        make_relative: Whether an absolute path under folder becomes a search path.

    Returns:
        The path to write.
    """
    # Unsaved, with no folder of its own, a search path was found from the working directory.
    old_folder = network_folder(stage) or os.getcwd()
    moved = not _same_folder(old_folder, folder)
    how = kind(value)
    if how == "search" and moved and (found := resolve(value, stage)):
        return relative_to(folder, found)
    if how == "dot" and moved:
        return relative_to(folder, os.path.normpath(os.path.join(old_folder, value.strip())))
    if how == "absolute" and make_relative:
        return relative_to(folder, value)
    return value


def rebase_saved(layer: Sdf.Layer, scene, folder: str, make_relative: bool) -> int:
    """Fit the input file paths saved in layer to its folder. Returns how many changed.

    A search path found from the network's old folder is written as found from folder, so
    saving somewhere else keeps it naming the same file; where no search path reaches
    that file any more, it is written absolute. With make_relative, an absolute path under
    folder becomes a search path too; one elsewhere has no search-path form and is left,
    and warned about at cook. layer holds the chunk prims at their live paths.
    """
    stage = scene.get_stage()

    def fit(value: str) -> str:
        return refit(value, stage, folder, make_relative)

    changed = 0
    for chunk in scene.get_chunks():
        for param in _input_templates(chunk):
            attr_path = getattr(param, "_param_attr_path", None)
            spec = layer.GetAttributeAtPath(Sdf.Path(attr_path)) if attr_path else None
            if spec is None or spec.default is None:
                continue
            value = spec.default
            if isinstance(value, (str, Sdf.AssetPath)):
                old = value.path if isinstance(value, Sdf.AssetPath) else value
                new = fit(old)
                if new != old:
                    spec.default = Sdf.AssetPath(new) if isinstance(value, Sdf.AssetPath) else new
                    changed += 1
            elif isinstance(value, (Vt.StringArray, list, tuple)):
                new = [fit(v) for v in value]
                if list(new) != list(value):
                    spec.default = Vt.StringArray(new)
                    changed += sum(a != b for a, b in zip(new, value))
    return changed


def _same_folder(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _input_templates(chunk) -> Iterator:
    """Every input file param of chunk as stored: a list's own param, not one per row."""
    stack = list(chunk.get_my_params())
    while stack:
        param = stack.pop()
        if hasattr(param, "proxy_params"):  # a list stores each param once, as an array
            stack.extend(param.children.get_params())
        elif is_input(param):
            yield param
        elif hasattr(param, "get_my_params"):  # a group
            stack.extend(param.get_my_params())
