"""Checks a release tag and prints its notes, for the release workflow.

Usage:
    python .github/scripts/release.py check v1.0.0-rc
    python .github/scripts/release.py notes v1.0.0-rc > notes.md
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


def tag_version(tag: str) -> str:
    """Returns the version a tag names.

    Args:
        tag: A release tag, such as ``v1.0.0-rc``.

    Returns:
        The tag without its leading ``v``, such as ``1.0.0-rc``.
    """
    if not tag.startswith("v"):
        raise ValueError(f"release tags start with 'v', got {tag!r}")
    return tag[1:]


def check(tag: str, pyproject: Path = ROOT / "pyproject.toml") -> None:
    """Fails if the tag and the version in pyproject.toml differ.

    The two are compared as Python versions, so ``v1.0.0-rc`` matches ``1.0.0rc0``.

    Args:
        tag: A release tag, such as ``v1.0.0-rc``.
        pyproject: The pyproject.toml to read the version from.
    """
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    if Version(tag_version(tag)) != Version(version):
        raise ValueError(f"tag {tag} does not match version {version} in pyproject.toml")


def notes(tag: str, changelog: Path = ROOT / "CHANGELOG.md") -> str:
    """Returns the changelog section for a tag, without its heading.

    Args:
        tag: A release tag, such as ``v1.0.0-rc``.
        changelog: The changelog to read.

    Returns:
        The text under ``## [1.0.0-rc]``, up to the next ``## `` heading.
    """
    version = tag_version(tag)
    text = changelog.read_text(encoding="utf-8")
    heading = re.compile(rf"^## \[{re.escape(version)}\].*$", re.MULTILINE)
    found = heading.search(text)
    if not found:
        raise ValueError(f"CHANGELOG.md has no '## [{version}]' section")
    following = re.compile(r"^## ", re.MULTILINE).search(text, found.end())
    body = text[found.end():following.start() if following else len(text)].strip()
    if not body:
        raise ValueError(f"CHANGELOG.md's '## [{version}]' section is empty")
    return body + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "notes"):
        print(__doc__, file=sys.stderr)
        return 2
    command, tag = argv
    try:
        if command == "check":
            check(tag)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stdout.write(notes(tag))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
