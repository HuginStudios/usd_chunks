"""Make .agents/skills, .claude/skills and .codex/skills point at this folder.

The three are committed as symlinks to ../skills, so on macOS, Linux and Windows with
symlinks enabled (Developer Mode plus `git config core.symlinks true`) there is nothing
to do. Elsewhere git checks each one out as a small text file holding "../skills", which
no agent follows. Run this once after cloning, from anywhere:

    python skills/link_skills.py

It replaces each placeholder with a symlink, or on Windows without symlink rights a
directory junction (no admin needed), and tells git not to report the junction as a
change. A link that already works is left alone. A real folder with files in it is never
touched: move its contents out first.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import _winapi  # CPython's own junction call on Windows, no admin rights needed
except ImportError:
    _winapi = None

SKILLS = Path(__file__).resolve().parent
REPO = SKILLS.parent
LINKS = (".agents/skills", ".claude/skills", ".codex/skills")
TARGET = "../skills"


def _points_at_skills(path: Path) -> bool:
    return path.is_dir() and path.resolve() == SKILLS


def _is_placeholder(path: Path) -> bool:
    """The text file git writes for a symlink when it can't make one."""
    try:
        return path.is_file() and path.stat().st_size < 64 and path.read_text().strip() == TARGET
    except OSError:
        return False


def _make_link(path: Path) -> str:
    try:
        path.symlink_to(TARGET, target_is_directory=True)
        return "symlink"
    except OSError:
        if _winapi is None:
            raise
    _winapi.CreateJunction(str(SKILLS), str(path))
    return "junction"


def _hide_from_git(rel: str) -> None:
    """Keep git from listing a junction where it expects a symlink as a change."""
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel], cwd=REPO, capture_output=True
    ).returncode == 0
    if tracked:
        subprocess.run(["git", "update-index", "--skip-worktree", rel], cwd=REPO, check=True)


def link(rel: str) -> bool:
    path = REPO / rel
    if _points_at_skills(path):
        print(f"ok        {rel}")
        return True

    if _is_placeholder(path):
        path.unlink()
    elif path.is_dir() and not path.is_symlink():
        if any(path.iterdir()):
            print(f"skipped   {rel}: a real folder with files in it; move them out, then run again")
            return False
        path.rmdir()
    elif path.is_symlink() or path.exists():
        print(f"skipped   {rel}: something else is there; remove it, then run again")
        return False

    path.parent.mkdir(exist_ok=True)
    kind = _make_link(path)
    if kind == "junction":
        _hide_from_git(rel)
    print(f"{kind:<9} {rel} -> skills/")
    return True


def main() -> int:
    results = [link(rel) for rel in LINKS]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
