"""git skips the skill files seen through a link_skills.py junction.

On Windows without symlinks, skills/link_skills.py turns .agents/skills, .claude/skills and
.codex/skills into junctions to skills/. git reads a junction as a folder, so every skill
file showed again under each link as untracked, and `git add -A` staged 24 copies.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs git and a checkout"
)


@pytest.mark.parametrize("link", [".agents/skills", ".claude/skills", ".codex/skills"])
def test_files_under_a_skill_link_folder_are_ignored(link: str) -> None:
    # --no-index: the link itself is tracked (as a symlink); what is asked is the files a
    # junction there shows.
    seen = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", f"{link}/usd-chunks-write-chunk/SKILL.md"],
        cwd=REPO, capture_output=True,
    )
    assert seen.returncode == 0, f"git would add the files under {link}/ as copies of skills/"


def test_the_skills_folder_itself_is_not_ignored() -> None:
    seen = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "skills/usd-chunks-write-chunk/SKILL.md"],
        cwd=REPO, capture_output=True,
    )
    assert seen.returncode == 1
