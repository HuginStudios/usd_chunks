"""`usdchunks list` and `show`: what chunks are installed, and what one takes.

Nothing listed the installed chunks outside a running Scene (dir(scene.create)), and a
chunk's param names only showed once it was created. In an agent benchmark most of the
agent's tool calls went on grepping chunk sources for names and params.
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from usd_chunks import Scene, chunk_catalog, cli


@pytest.fixture(scope="module")
def registry():
    with Scene() as scene:
        yield scene.registry


def _leaves(params):
    for p in params:
        if p.kind == "group":
            yield from _leaves(p.children)
        elif p.kind != "list":
            yield p


def test_every_listed_name_is_one_scene_create_makes(registry) -> None:
    rows = chunk_catalog.entries(registry)
    assert len(rows) > 50
    with Scene() as scene:
        for row in rows:
            getattr(scene.create, row.name)  # raises for a name scene.create does not know


def test_a_module_name_wins_over_a_menu_entry(registry) -> None:
    """Light is a module; MaterialX also has a "light" node. scene.create.light is the module."""
    light = chunk_catalog.find(registry, "light")
    assert light.module == "light" and light.presets is None


def test_show_gives_the_names_a_script_sets(registry) -> None:
    entry, description, variants, params = chunk_catalog.describe(registry, "ground")
    assert entry.module == "mesh" and entry.presets["mesh_type"] == "grid"
    assert "cube" in {v.name for v in variants}
    names = {p.name for p in _leaves(params)}
    assert {"prim_path", "mesh_type", "width1", "height2", "radius1"} <= names
    with Scene() as scene:  # every one of them is accepted
        mesh = scene.create.mesh(prim_path="/m")
        scene.set(mesh, **{p.name: p.default for p in _leaves(params) if p.kind in ("int", "float", "bool")})


def test_list_rows_and_folds_big_menus(registry, capsys) -> None:
    cli.cmd_list(Namespace(query="", json=False))
    out = capsys.readouterr().out
    assert any(line.startswith("mesh ") for line in out.splitlines())
    assert "menu entries: usdchunks list <word>" in out, "MaterialX's node entries were not folded"
    cli.cmd_list(Namespace(query="cube", json=True))
    names = {row["name"] for row in json.loads(capsys.readouterr().out)}
    assert {"cube", "mesh"} <= names


def test_show_prints_params_and_a_list_groups_rows(capsys) -> None:
    cli.cmd_show(Namespace(name="scatter_points", json=False))
    out = capsys.readouterr().out
    assert "prototypes=[{param: value, ...}, ...]" in out and "prototypes.<i>.<param>" in out
    assert "proto_path" in out
    assert "max_slope" in out and "seed" in out


def test_show_lists_the_row_params_of_a_list_with_no_rows(registry) -> None:
    # A new Sublayer has no rows; its row param was missing, and with it what to set.
    _, _, _, params = chunk_catalog.describe(registry, "sublayer")
    imports = next(p for p in params if p.name == "imports")
    assert [c.name for c in imports.children] == ["sublayer_path"]


def test_show_an_unknown_name_fails(capsys) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.cmd_show(Namespace(name="no_such_chunk", json=False))
    assert stop.value.code == 1
