"""A prim-path param given text that isn't a prim path neither freezes nor errors.

Wiring Edge Field's Primvar output ("edges") into Scatter Points' Painted Points froze usdview.
"edges" is a relative path, and skip_cook's _covered walked its parents up to the root:
"edges" -> "." -> ".." -> "../.." for ever. Any chunk that looked up a prim by a path param
hung the same way (Camera's Aim At was the first one an all-chunks sweep reached).

So every chunk is tried here with each of its prim-path params set to text a wire or a typo
can put there. Each check runs in its own Python with a time limit: a hang is the bug, and a
hang inside the test run would stall it rather than fail it.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd
from PySide6 import QtWidgets

from usd_chunks import Scene, chunk, skip_cook
from usd_chunks.chunk_core import ChunkFlags
from usd_chunks.chunk_script import Scene as ScriptScene

ROOT = Path(__file__).resolve().parents[1]
CHUNKS = ROOT / "chunks"

#: What a wire from a name output, a typo or a half-typed path can leave in a path param.
NOT_PRIM_PATHS = ["edges", "a/b", "..", "1.5", "/", "/no/such/prim", "%nope", "@Nope.param@"]

#: Chunks that can't be built here for a reason that has nothing to do with paths.
_NEEDS_MORE = {"kitti_writer"}  # needs pxr.CameraUtil, missing from some usd-core wheels

#: Seconds a whole check may take. The sweep of every chunk takes about five.
_LIMIT = 180


def _run(call: str, *args: str) -> object:
    """Run `call(*args)` from this module in a fresh Python; its JSON result, or fail on a hang."""
    env = dict(os.environ)
    env["USD_CHUNK_DIRS"] = str(CHUNKS)
    env["USD_CHUNK_AUTO_INSTALL"] = "0"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "tests"), env.get("PYTHONPATH", "")])
    code = f"import json, sys, test_path_params_take_any_text as t; print(json.dumps(t.{call}(*sys.argv[1:])))"
    try:
        done = subprocess.run([sys.executable, "-c", code, *args], env=env, capture_output=True, text=True,
                              timeout=_LIMIT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{call}{args} did not finish in {_LIMIT}s: a hang")
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


# -- what the subprocess runs --------------------------------------------------------


def covered(path: str) -> bool:
    return skip_cook._covered(Sdf.Path(path), {Sdf.Path("/World")})


def wired_primvar_readout() -> list:
    """The reported case: Edge Field's Primvar output wired into Scatter Points' Painted Points."""
    with ScriptScene(Usd.Stage.CreateInMemory(), "pytest") as scene:
        table = scene.add("mesh", mesh_type="cube", prim_path="/Table/top")
        edges = scene.add("edge_field", table, prim_path="/Table/top", primvar="edges")
        scatter = scene.add("scatter_points", edges, surface_prim="/Table/top")
        scatter.get_param("points_prim").connect_value(edges.get_param("primvar"))
        return [scatter.get_param("points_prim").value, scatter.get_param("readout").value]


def sweep(text: str) -> dict:
    """{chunk: what went wrong} with every prim-path param of every chunk set to `text`."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])  # noqa: F841 - for widget chunks

    def path_params(group):
        for param in group.get_my_params():
            if chunk.is_group(param):
                yield from path_params(param)
            elif chunk.is_param(param) and (param.has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION)
                                            or param.has_flag(ChunkFlags.ParamFlags.PRIM_PATHS)):
                yield param

    errors = []

    class _Errors(logging.Handler):
        def emit(self, record):
            errors.append(record.getMessage())

    logging.getLogger().addHandler(_Errors(logging.ERROR))
    with Scene() as scene:
        names = sorted(m.module_name for m in scene.registry.get_chunk_modules()
                       if CHUNKS in Path(m.module_file).resolve().parents and m.module_name not in _NEEDS_MORE)
    broken = {}
    for name in names:
        errors.clear()
        scene = ScriptScene(Usd.Stage.CreateInMemory(), "pytest")
        try:
            node = scene.add(name)
            for param in path_params(node):
                param.value = text
        except Exception as exc:  # noqa: BLE001 - every failure is collected and reported
            broken[name] = " ".join(str(exc).split())[:200]
        finally:
            scene.close()
        if errors and name not in broken:
            broken[name] = " ".join(errors[0].split())[:200]
    return broken


# -- the tests --------------------------------------------------------------------------


def test_a_relative_path_is_not_under_any_masked_prim() -> None:
    """The walk that hung: a relative path's parents never reach the root."""
    assert _run("covered", "edges") is False
    assert _run("covered", "/World/a") is True


def test_a_primvar_name_wired_into_painted_points_explains_itself() -> None:
    value, readout = _run("wired_primvar_readout")
    assert value == "edges"
    assert "Painted Points 'edges' is not a prim path" in readout


@pytest.mark.parametrize("text", NOT_PRIM_PATHS)
def test_every_chunk_takes_any_text_in_its_path_params(text) -> None:
    broken = _run("sweep", text)
    assert not broken, "\n".join(f"{name}: {why}" for name, why in sorted(broken.items()))
