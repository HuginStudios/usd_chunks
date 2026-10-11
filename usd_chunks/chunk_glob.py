"""Pure glob pattern math for batch processing — no pxr/Qt dependency, so the capture and
substitution rules are directly unit-testable as tables without building a stage. See
``chunk_batch`` for the USD glue: finding the patterns on Chunk prims, picking the
driver among them, and handing each run its concrete values.

A pattern is a shell-style glob, not a regex. Its wildcards capture left to right, and a sink
pattern's wildcards consume those captures in the same order, so structure survives the
conversion::

    driver  assets/**/*.fbx        sink  out/**/*.usda
    assets/char/hero.fbx     ->    out/char/hero.usda      captures ("char", "hero")
    assets/prop/crate.fbx    ->    out/prop/crate.usda     captures ("prop", "crate")
    assets/tree.fbx          ->    out/tree.usda           captures ("", "tree")

``**`` followed by a separator is one token that matches zero or more path segments, which is
why the last row above collapses cleanly instead of leaving an empty ``out//tree.usda``.

A sink can also name a capture by number, ``{1}`` for the first, as often as it likes -- the
way to use one twice, which consuming left to right cannot::

    driver  assets/*.usdc          sink  proxies/{1}/{1}.usda
    assets/fern.usdc         ->    proxies/fern/fern.usda

A placeholder consumes nothing, so a sink may mix them with wildcards, and one holding only
placeholders is a pattern too.
Matching is case-sensitive and always over ``/``-separated relative paths, so a plan reads the
same on every platform.
"""

from __future__ import annotations

import os
import re
from typing import Iterator, List, Tuple

WILDCARD_CHARS = "*?["

LITERAL = "literal"
GLOBSTAR_DIR = "globstar_dir"
GLOBSTAR = "globstar"
STAR = "star"
ANY = "any"
CLASS = "class"
REF = "ref"

# A sink's numbered placeholder: {1} is the first capture.
_REF_RE = re.compile(r"\{(\d+)\}")

Token = Tuple[str, str]
Captures = Tuple[str, ...]


class GlobError(Exception):
    """Raised for a malformed pattern or a substitution whose captures don't fit."""


def is_pattern(value: str) -> bool:
    """Whether a param value should be treated as a glob rather than a plain path."""
    return isinstance(value, str) and (any(ch in value for ch in WILDCARD_CHARS) or bool(_REF_RE.search(value)))


def normalize(path: str) -> str:
    """Path with native separators folded to ``/``, the only form this module speaks."""
    return path.replace("\\", "/")


def tokenize(pattern: str) -> List[Token]:
    """Split a pattern into literal runs and wildcard tokens, in order."""
    tokens: List[Token] = []
    buf: List[str] = []
    i = 0
    n = len(pattern)

    def flush() -> None:
        if buf:
            tokens.append((LITERAL, "".join(buf)))
            buf.clear()

    while i < n:
        ch = pattern[i]
        if ch == "*":
            flush()
            if pattern[i:i + 2] == "**":
                if pattern[i + 2:i + 3] == "/":
                    tokens.append((GLOBSTAR_DIR, "**/"))
                    i += 3
                else:
                    tokens.append((GLOBSTAR, "**"))
                    i += 2
            else:
                tokens.append((STAR, "*"))
                i += 1
        elif ch == "?":
            flush()
            tokens.append((ANY, "?"))
            i += 1
        elif ch == "{" and (ref := _REF_RE.match(pattern, i)):
            flush()
            tokens.append((REF, ref.group(1)))
            i = ref.end()
        elif ch == "[":
            end = pattern.find("]", i + 2 if pattern[i + 1:i + 2] in ("!", "^") else i + 1)
            if end == -1:
                raise GlobError(f"unterminated character class in pattern {pattern!r}")
            flush()
            tokens.append((CLASS, pattern[i:end + 1]))
            i = end + 1
        else:
            buf.append(ch)
            i += 1

    flush()
    return tokens


def capture_count(pattern: str) -> int:
    """How many captures the pattern produces (== how many its wildcards consume as a sink)."""
    return sum(1 for kind, _ in tokenize(pattern) if kind not in (LITERAL, REF))


def to_regex(pattern: str) -> str:
    """Anchored regex for the pattern, one capture group per wildcard, in order."""
    parts = ["\\A"]
    for kind, text in tokenize(pattern):
        if kind == LITERAL:
            parts.append(re.escape(text))
        elif kind == REF:
            parts.append(re.escape(f"{{{text}}}"))  # names no file: only a sink fills one
        elif kind == GLOBSTAR_DIR:
            parts.append("(?:(.*)/)?")
        elif kind == GLOBSTAR:
            parts.append("(.*)")
        elif kind == STAR:
            parts.append("([^/]*)")
        elif kind == ANY:
            parts.append("([^/])")
        elif kind == CLASS:
            body = text[1:-1]
            if body[:1] in ("!", "^"):
                body = "^" + body[1:]
            parts.append(f"([{body}])")
    parts.append("\\Z")
    return "".join(parts)


def match(pattern: str, path: str) -> Captures | None:
    """Captures for a path matching the pattern, or None. An unmatched ``**/`` captures ``""``."""
    m = re.match(to_regex(pattern), normalize(path))
    if not m:
        return None
    return tuple(g if g is not None else "" for g in m.groups())


def substitute(pattern: str, captures: Captures) -> str:
    """Fill a sink pattern's wildcards from captures, left to right, and its ``{N}``s by number.

    An empty ``**/`` capture drops the separator with it, so a driver match that lived at the
    top of the tree writes its output at the top of the tree instead of under a blank folder.
    """
    tokens = tokenize(pattern)
    wanted = sum(1 for kind, _ in tokens if kind not in (LITERAL, REF))
    refs = any(kind == REF for kind, _ in tokens)
    if (wanted or not refs) and wanted != len(captures):
        raise GlobError(
            f"pattern {pattern!r} has {wanted} wildcard(s) but {len(captures)} capture(s) "
            f"to fill them with — give both patterns the same number of wildcards, or name "
            f"captures by number: {{1}}"
        )
    for kind, text in tokens:
        if kind == REF and not 1 <= int(text) <= len(captures):
            raise GlobError(
                f"pattern {pattern!r} names capture {{{text}}} but there are {len(captures)}"
            )

    out: List[str] = []
    it = iter(captures)
    for kind, text in tokens:
        if kind == LITERAL:
            out.append(text)
            continue
        if kind == REF:
            out.append(captures[int(text) - 1])
            continue
        value = next(it)
        if kind == GLOBSTAR_DIR:
            out.append(f"{value}/" if value else "")
        else:
            out.append(value)
    return "".join(out)


def literal_prefix_dir(pattern: str) -> str:
    """The deepest directory of the pattern that holds no wildcard, so a walk can start there."""
    tokens = tokenize(pattern)
    head = normalize(tokens[0][1]) if tokens and tokens[0][0] == LITERAL else ""
    return head.rsplit("/", 1)[0] if "/" in head else ""


def iter_files(root: str, start: str = "") -> Iterator[str]:
    """Yield every file under ``root/start`` as a ``/``-separated path relative to root."""
    base = os.path.join(root, start.replace("/", os.sep)) if start else root
    if not os.path.isdir(base):
        return
    for dirpath, _dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir == "." else normalize(rel_dir) + "/"
        for name in sorted(filenames):
            yield prefix + name


def strip_dot_slash(pattern: str) -> Tuple[str, str]:
    """Split a leading ``./`` off a pattern, returning (prefix, remainder).

    Networks author paths in that form already (``"./assets/asset_A.usd"``), and the walk
    yields root-relative paths that never carry it, so the prefix has to come off before
    matching and go back on afterwards — keeping the concrete path in the same shape the
    pattern was written in.
    """
    body = normalize(pattern)
    prefix = ""
    while body.startswith("./"):
        prefix += "./"
        body = body[2:]
    return prefix, body


def expand(pattern: str, root: str) -> List[Tuple[str, Captures]]:
    """Existing files matching the pattern, as (path, captures).

    A relative pattern is resolved under ``root`` and reported relative to it; an absolute one
    ignores ``root`` entirely and is reported absolute. Either way the path comes back in the
    same shape the pattern was written in, since that path is what gets authored back onto the
    chunk prim — rewriting a relative network into absolute paths, or vice versa, would change
    what the network means.

    Sorted by path so a batch plan is stable run to run. Only files are returned — a pattern
    is how a batch names the things it reads, and it reads files.
    """
    prefix, body = strip_dot_slash(pattern)
    regex = re.compile(to_regex(body))
    results = []

    if os.path.isabs(body):
        # The pattern carries its own root, so the walk starts at its literal head and matches
        # against whole absolute paths. `root` is not consulted at all.
        base = literal_prefix_dir(body)
        for rel in iter_files(base):
            full = f"{base}/{rel}"
            m = regex.match(full)
            if m:
                results.append((full, tuple(g if g is not None else "" for g in m.groups())))
    else:
        for rel in iter_files(root, literal_prefix_dir(body)):
            m = regex.match(rel)
            if m:
                results.append((prefix + rel, tuple(g if g is not None else "" for g in m.groups())))

    results.sort(key=lambda item: item[0])
    return results
