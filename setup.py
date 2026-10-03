"""Ships the repository's chunks/ folder inside the wheel, as usd_chunks/chunks.

Everything else is configured in pyproject.toml. chunks/ stays at the repository root,
where USD_CHUNK_DIRS finds it in a checkout; this copies it into the built package so an
installed usd_chunks finds the same set there (see chunk_registry.USD_CHUNK_DIRS).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

# Patterns left out of the shipped set, as shutil.ignore_patterns takes them. Add a
# chunk's file or folder name here to keep it in the repository but out of the wheel.
# cameras.json: a stale download cache from before the camera chunk cached per user.
EXCLUDE = ("__pycache__", "*.pyc", "cameras.json")


class BuildPyWithChunks(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parent / "chunks"
        if source.is_dir():
            shutil.copytree(
                source,
                Path(self.build_lib) / "usd_chunks" / "chunks",
                ignore=shutil.ignore_patterns(*EXCLUDE),
                dirs_exist_ok=True,
            )


setup(cmdclass={"build_py": BuildPyWithChunks})
