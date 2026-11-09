"""git skips the skill files seen through a link_skills.py junction.

On Windows without symlinks, skills/link_skills.py turns .agents/skills, .claude/skills and
.codex/skills into junctions to skills/. git reads a junction as a folder, so every skill
file showed again under each link as untracked, and `git add -A` staged 24 copies.

The rule is checked in a scratch repo holding only this repo's .gitignore, not in the
checkout: where the links are real symlinks (CI), git refuses any path through them
("beyond a symbolic link") before it looks at an ignore rule at all.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LINKS = [".agents/skills", ".claude/skills", ".codex/skills"]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture(scope="module")
def ignore_repo(tmp_path_factory) -> Path:
    """An empty repo with this repo's .gitignore, and nothing on disk for it to trip over."""
    root = tmp_path_factory.mktemp("ignore_repo")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    shutil.copyfile(REPO / ".gitignore", root / ".gitignore")
    return root


def _ignored(root: Path, path: str) -> bool:
    seen = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=root, capture_output=True)
    assert seen.returncode in (0, 1), seen.stderr.decode()
    return seen.returncode == 0


@pytest.mark.parametrize("link", LINKS)
def test_files_under_a_skill_link_folder_are_ignored(ignore_repo: Path, link: str) -> None:
    assert _ignored(ignore_repo, f"{link}/usd-chunks-write-chunk/SKILL.md"), (
        f"git would add the files under {link}/ as copies of skills/"
    )


@pytest.mark.parametrize("link", LINKS)
def test_the_link_itself_is_not_ignored(ignore_repo: Path, link: str) -> None:
    # The link is tracked as a symlink; only a folder (a junction) at that path is skipped.
    assert not _ignored(ignore_repo, link)


def test_the_skills_folder_itself_is_not_ignored(ignore_repo: Path) -> None:
    assert not _ignored(ignore_repo, "skills/usd-chunks-write-chunk/SKILL.md")
