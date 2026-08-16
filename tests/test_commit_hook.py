"""Tests for the reserved commit(node) chunk hook.

commit() is the write-to-disk counterpart to execute(): dispatched once per chunk that
defines it, in topological order, after the DAG solve has settled — rather than from
inside the notice propagation loop, the way execute() is re-run throughout a solve.

Covers the dispatch itself (chunk_scheduler.commit_all), Export USD's implementation of
it, and the CLI wiring that makes `usdchunks run` write a network's declared outputs.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import types
from pathlib import Path

from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_scheduler
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags, SceneSender
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_usd import export_chunks
from usd_chunks.plugins.chunk_usd import initialize as init_chunk_usd
from usd_chunks.plugins.usdchunks_cli import cmd_run


class _DummyScene(SceneSender):
    def __init__(self):
        super().__init__()
        self._stage = None

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _setup_stage():
    scene = _DummyScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    stage = Usd.Stage.CreateInMemory()
    scene._stage = stage
    init_chunk_usd(stage, scene)
    return scene, stage


def _make(scene, mod, module_name):
    """Create a chunk from mod and add it to scene, auto-chaining it onto the previous
    one exactly as the Params tab does — Edit Target resolves from exec-chain topology,
    so chunks have to actually be wired into the chain."""
    c = mod.main()
    c._config = ChunkConfig(module=module_name)
    c._module = mod
    prev = chunk_usd._params_tab_create
    chunk_usd._params_tab_create = True
    try:
        scene.add_chunk(c)
    finally:
        chunk_usd._params_tab_create = prev
    return c


class TestCommitDispatch:
    """chunk_scheduler.commit_all, against chunks carrying stub modules."""

    def _chunk(self, name, calls, with_commit=True):
        c = chunk.Chunk(name)
        c._config = ChunkConfig(module=name)
        if with_commit:
            c._module = types.SimpleNamespace(commit=lambda node: calls.append(node.name))
        else:
            c._module = types.SimpleNamespace()
        return c

    def test_runs_hook_once_per_chunk_defining_it(self):
        chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")
        calls = []
        a = self._chunk("a", calls)

        committed = chunk_scheduler.commit_all([a])

        assert calls == ["a"]
        assert committed == [a]

    def test_commits_in_topological_order(self):
        chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")
        calls = []
        a = self._chunk("a", calls)
        b = self._chunk("b", calls)
        c = self._chunk("c", calls)
        b.add_dependency(a)
        c.add_dependency(b)

        # Handed over scrambled: the order must come from the graph, not the iterable.
        chunk_scheduler.commit_all([c, a, b])

        assert calls == ["a", "b", "c"]

    def test_param_wired_chunks_still_ordered(self):
        """Drag-to-connect wires param to param and never calls Chunk.add_dependency, so
        there is no chunk-level edge to sort on — the ordering has to come from depth,
        where ownership pulls a chunk at least as deep as its deepest param."""
        chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")
        calls = []
        upstream = self._chunk("upstream", calls)
        downstream = self._chunk("downstream", calls)

        out = chunk.Slider("out", default_value=0.0)
        upstream.add_param(out)
        inp = chunk.Slider("in", default_value=0.0)
        downstream.add_param(inp)
        inp.add_dependency(out)

        assert downstream.get_dependencies() == []
        chunk_scheduler.commit_all([downstream, upstream])

        assert calls == ["upstream", "downstream"]

    def test_disabled_chunk_does_not_commit(self):
        chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")
        calls = []
        a = self._chunk("a", calls)
        b = self._chunk("b", calls)
        b.add_dependency(a)
        b.set_state(ChunkFlags.State.DISABLED)

        committed = chunk_scheduler.commit_all([a, b])

        assert calls == ["a"]
        assert committed == [a]

    def test_module_without_commit_is_untouched(self):
        chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")
        calls = []
        plain = self._chunk("plain", calls, with_commit=False)
        exporter = self._chunk("exporter", calls)

        committed = chunk_scheduler.commit_all([plain, exporter])

        assert calls == ["exporter"]
        assert committed == [exporter]


class TestExportUsdCommit:
    """Export USD's commit() and its Export button must be the same implementation."""

    def setup_method(self):
        self.create_mod = importlib.import_module("scripts.create_layer")
        self.export_mod = importlib.import_module("scripts.export_usd")

    def _network(self, tmp_path, out_name="out"):
        scene, stage = _setup_stage()
        shot = _make(scene, self.create_mod, "create_layer")
        shot.get_param("layer_name").value = "shot"
        shot.execute()

        export_chunk = _make(scene, self.export_mod, "export_usd")
        out_dir = tmp_path / out_name
        export_chunk.get_param("output_dir").value = str(out_dir)
        return scene, stage, export_chunk, out_dir

    def test_commit_writes_the_configured_output(self, tmp_path):
        _scene, _stage, export_chunk, out_dir = self._network(tmp_path)

        self.export_mod.commit(export_chunk)

        assert (out_dir / "shot.usda").exists()

    def test_button_delegates_to_commit(self, tmp_path, monkeypatch):
        """One implementation, not two: the Export button must call commit(), or the
        interactive and headless paths are free to drift apart."""
        _scene, _stage, export_chunk, _out_dir = self._network(tmp_path)
        seen = []
        monkeypatch.setattr(self.export_mod, "commit", seen.append)

        self.export_mod.export_all(export_chunk.get_param("export_all"), None, None)

        assert seen == [export_chunk]

    def test_button_and_commit_write_the_same_files(self, tmp_path):
        """Only the file *set* is compared, not the bytes: setting output_dir for the
        second run is itself an authored param change, so the exported bookkeeping
        legitimately differs between the two."""
        _scene, _stage, export_chunk, hook_dir = self._network(tmp_path, "hook")
        self.export_mod.commit(export_chunk)

        button_dir = tmp_path / "button"
        export_chunk.get_param("output_dir").value = str(button_dir)
        self.export_mod.export_all(export_chunk.get_param("export_all"), None, None)

        assert {p.name for p in hook_dir.iterdir()} == {p.name for p in button_dir.iterdir()}

    def test_flatten_honours_the_same_checkbox_the_button_does(self, tmp_path):
        _scene, _stage, export_chunk, _out_dir = self._network(tmp_path)
        flat_file = tmp_path / "flat.usda"
        export_chunk.get_param("flatten").value = True
        export_chunk.get_param("output_file").value = str(flat_file)

        self.export_mod.commit(export_chunk)

        assert flat_file.exists()

    def test_disabled_export_chunk_writes_nothing(self, tmp_path):
        scene, _stage, export_chunk, out_dir = self._network(tmp_path)
        export_chunk.set_state(ChunkFlags.State.DISABLED)

        chunk_scheduler.commit_all(scene.get_chunks())

        assert not out_dir.exists()


class TestCliRunCommits:
    """`usdchunks run` on a network containing an Export USD chunk writes that chunk's
    configured output — the deliberate behaviour change that makes the chunk reachable
    headlessly at all, since it has no execute() to reach it through."""

    def _write_network(self, tmp_path):
        """Author a Light -> Export USD network and serialize it, returning the network
        file and the output path the Export USD chunk is configured to write."""
        export_mod = importlib.import_module("scripts.export_usd")
        light_mod = importlib.import_module("scripts.light")

        scene, _stage = _setup_stage()
        light = _make(scene, light_mod, "light")
        light.execute()

        result = tmp_path / "result.usda"
        export_chunk = _make(scene, export_mod, "export_usd")
        export_chunk.get_param("flatten").value = True
        export_chunk.get_param("output_file").value = str(result)

        net = tmp_path / "net.usda"
        export_chunks(scene, net)
        assert net.exists()
        assert not result.exists(), "nothing should be exported until the network is run"
        return net, result

    def test_cmd_run_commits_the_export_chunk(self, tmp_path):
        net, result = self._write_network(tmp_path)

        # The layer manager is a singleton; leaving it pointed at the authoring stage
        # leaks its layers into the run below (see test_export_load_roundtrip).
        chunk_registry.ChunkLayerManager().reset()

        cmd_run(types.SimpleNamespace(usda=str(net), params=[], export=None, printout=False))

        assert result.exists(), "run should have committed the Export USD chunk"

    def test_run_writes_the_networks_driven_content(self, tmp_path):
        """The same thing end to end, in a clean interpreter. Loading a second network
        into a process that already holds a live scene does not re-cook the loaded
        chunks — `--printout` comes back empty there too, so it is not something commit
        introduced — and only a fresh process shows that what `run` commits is the real
        driven content, not an empty stage."""
        net, result = self._write_network(tmp_path)

        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["USD_CHUNK_DIRS"] = str(repo_root / "scripts")
        env["PYTHONPATH"] = str(repo_root)
        proc = subprocess.run(
            [sys.executable, "-m", "usd_chunks.plugins.usdchunks_cli", "run", str(net)],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, proc.stderr
        assert result.exists(), f"run should have committed the export\n{proc.stdout}\n{proc.stderr}"
        out_stage = Usd.Stage.Open(str(result))
        assert [p for p in out_stage.Traverse() if p.GetTypeName() == "DiskLight"], (
            "the committed export should carry the network's driven content"
        )
