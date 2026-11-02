"""chunk_stage.network_target: is a referenced file a chunk network, or scene content?

The Reference and Payload chunks ask this of every file they are pointed at; a network is
hosted (its chunks join the graph) and anything else is placed as geometry. Getting it wrong
either way is silent: a network placed as geometry shows nothing, and an asset taken for a
network loses its location.
"""
from __future__ import annotations

import pytest
from pxr import Sdf

from usd_chunks import chunk_stage

_GROUP = '''def Chunk "{name}"
    {{
        uniform token info:chunkType = "chunkgroup"
    }}'''


def _write(tmp_path, name, body, header=""):
    path = tmp_path / name
    path.write_text(f"#usda 1.0\n{header}\n{body}\n", encoding="utf-8")
    return str(path)


def test_a_plain_scene_file_is_not_a_network(tmp_path):
    path = _write(tmp_path, "scene.usda", 'def Xform "World" { def Sphere "Ball" {} }',
                  '(\n    defaultPrim = "World"\n)')

    assert chunk_stage.network_target(path) is None


def test_a_default_prim_that_is_a_chunkgroup_is_the_network(tmp_path):
    path = _write(tmp_path, "net.usda", _GROUP.format(name="Net"), '(\n    defaultPrim = "Net"\n)')

    assert chunk_stage.network_target(path) == "/Net"


def test_a_chunks_scope_stands_for_the_first_group_in_it(tmp_path):
    body = 'def Scope "chunks"\n{\n    ' + _GROUP.format(name="Layout") + "\n}"
    path = _write(tmp_path, "saved.usda", body)

    assert chunk_stage.network_target(path) == "/chunks/Layout"


def test_an_explicit_import_path_is_used(tmp_path):
    body = 'def Scope "chunks"\n{\n    ' + _GROUP.format(name="A") + "\n    " + \
        _GROUP.format(name="B") + "\n}"
    path = _write(tmp_path, "two.usda", body)

    assert chunk_stage.network_target(path, "/chunks/B") == "/chunks/B"


def test_an_import_path_at_scene_content_is_not_a_network(tmp_path):
    body = 'def Xform "World" {}\ndef Scope "chunks"\n{\n    ' + _GROUP.format(name="A") + "\n}"
    path = _write(tmp_path, "mixed.usda", body)

    assert chunk_stage.network_target(path, "/World") is None


def test_a_chunk_that_is_not_a_group_is_not_a_network(tmp_path):
    body = 'def Chunk "Thing"\n{\n    uniform token info:chunkType = "transform"\n}'
    path = _write(tmp_path, "one.usda", body, '(\n    defaultPrim = "Thing"\n)')

    assert chunk_stage.network_target(path) is None


def test_a_network_held_only_through_a_sublayer_is_found(tmp_path):
    body = 'def Scope "chunks"\n{\n    ' + _GROUP.format(name="Lighting") + "\n}"
    _write(tmp_path, "lighting.usda", body)
    path = _write(tmp_path, "sequence.usda", "", '(\n    subLayers = [@./lighting.usda@]\n)')

    assert chunk_stage.network_target(path) == "/chunks/Lighting"


@pytest.mark.parametrize("path", ["", "Z:/no/such/file.usda"])
def test_no_file_is_not_a_network(path):
    assert chunk_stage.network_target(path) is None


class _OpenSpy:
    """chunk_stage's Sdf, with Layer.FindOrOpen recording what it is asked to open."""

    def __init__(self):
        self.opened = []
        spy = self

        class Layer:
            @staticmethod
            def FindOrOpen(path):
                spy.opened.append(path)
                return Sdf.Layer.FindOrOpen(path)

        self.Layer = Layer

    def __getattr__(self, name):
        return getattr(Sdf, name)


def test_a_file_in_another_format_is_not_opened_to_ask(tmp_path, monkeypatch):
    # A material Reference points at a .mtlx: opening it ran the MaterialX plugin's
    # conversion and hundreds of change notices, only for the reference to open it again.
    # Only a USD file can hold a network.
    if Sdf.FileFormat.FindByExtension("mtlx") is None:
        # pip usd-core has no MaterialX plugin: .mtlx is an unknown format, opened as before.
        pytest.skip("no MaterialX file format plugin in this USD build")
    mtlx = tmp_path / "Metal.mtlx"
    mtlx.write_text('<?xml version="1.0"?>\n<materialx version="1.38"/>\n', encoding="utf-8")
    usda = _write(tmp_path, "net.usda", _GROUP.format(name="Net"), '(\n    defaultPrim = "Net"\n)')
    spy = _OpenSpy()
    monkeypatch.setattr(chunk_stage, "Sdf", spy)

    assert chunk_stage.network_target(str(mtlx)) is None
    assert spy.opened == []
    assert chunk_stage.network_target(usda) == "/Net"
    assert spy.opened == [usda]
