"""Tests for the reserved commit(node) chunk hook.

commit() is the write-to-disk counterpart to execute(): dispatched once per chunk that
defines it, in topological order, after the DAG solve has settled -- rather than from
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

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry, chunk_scheduler
from usd_chunks.chunk_core import ChunkConfig, ChunkFlags, SceneSender
from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import export_chunks
from usd_chunks.chunk_stage import initialize as init_chunk_usd
from usd_chunks.cli import cmd_run


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
    one exactly as the Params tab does -- Edit Target resolves from exec-chain topology,
    so chunks have to actually be wired into the chain."""
    c = mod.main()
    c._config = ChunkConfig(module=module_name)
    c._module = mod
    prev = chunk_stage._params_tab_create
    chunk_stage._params_tab_create = True
    try:
        scene.add_chunk(c)
    finally:
        chunk_stage._params_tab_create = prev
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
        """A param-level add_dependency leaves no chunk-level edge to sort on, so the
        ordering has to come from depth, where ownership pulls a chunk at least as deep as
        its deepest param.

        Note this is the in-memory add_dependency API, NOT what the node graph does -- a
        dragged param port authors an expression and creates no DAG edge at all. That path
        is covered by TestCommitOrderFromStage, which orders from the stage instead."""
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
        self.create_mod = importlib.import_module("scripts.usd.create_layer")
        self.export_mod = importlib.import_module("scripts.core.export_usd")

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
    configured output -- the deliberate behaviour change that makes the chunk reachable
    headlessly at all, since it has no execute() to reach it through."""

    def _write_network(self, tmp_path):
        """Author a Light -> Export USD network and serialize it, returning the network
        file and the output path the Export USD chunk is configured to write."""
        export_mod = importlib.import_module("scripts.core.export_usd")
        light_mod = importlib.import_module("scripts.modelling.light")

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
        chunks -- `--printout` comes back empty there too, so it is not something commit
        introduced -- and only a fresh process shows that what `run` commits is the real
        driven content, not an empty stage."""
        net, result = self._write_network(tmp_path)

        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["USD_CHUNK_DIRS"] = str(repo_root / "scripts")
        # Prepended, not assigned: USD's own bindings are often found through PYTHONPATH,
        # and replacing it left the child unable to import pxr at all.
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo_root), os.environ.get("PYTHONPATH")]))
        proc = subprocess.run(
            [sys.executable, "-m", "usd_chunks.cli", "run", str(net)],
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


class TestCommitOrderFromStage:
    """chunk_stage.commit_order_from_stage -- ordering read from USD connections.

    Depth used to be blind to a param port: drag-to-connect authors an @Node.param@
    expression whose live link is a NoticeConnection, not a DAG edge, so both chunks sat at
    depth 0 and committed in arbitrary order. reconcile_exec_dependencies now mirrors param
    wires into chunk-level DAG edges as well (see _wired_dependencies), so depth sees
    them too and the two orderings agree.

    This function is still the one the CLI uses: it reads the stage directly, and
    deliberately ignores connections to chunks outside the batch being committed.
    """

    def _chunk(self, scene, name, calls, with_commit=True):
        c = chunk.Chunk(name)
        c._config = ChunkConfig(module="prim")
        if with_commit:
            c._module = types.SimpleNamespace(commit=lambda node: calls.append(node.name))
        else:
            c._module = types.SimpleNamespace()
        c.add_param(chunk.Slider("p", default_value=0.0))
        scene.add_chunk(c)
        return c

    def test_expression_wired_chunks_are_ordered(self):
        """The only wire between them is a param expression, and both orderings see it."""
        scene, stage = _setup_stage()
        calls = []
        upstream = self._chunk(scene, "upstream", calls)
        downstream = self._chunk(scene, "downstream", calls)
        upstream.get_param("p").value = 2.0
        downstream.get_param("p").value = "@upstream.p@"

        ordered = chunk_stage.commit_order_from_stage([downstream, upstream], stage)

        assert ordered == [upstream, downstream]
        assert chunk_scheduler.commit_order([downstream, upstream]) == [upstream, downstream], (
            "depth used to be blind to a param wire; reconcile now mirrors it into the DAG"
        )

    def test_a_param_wire_is_not_authored_as_an_exec_edge(self):
        """The DAG edge a param wire now creates must not become an inputs:chunk.

        _author_prim falls back to the Python DAG when a chunk has no exec wiring of its
        own, and that fallback would otherwise write out an exec edge the user never drew.
        """
        scene, stage = _setup_stage()
        calls = []
        upstream = self._chunk(scene, "upstream", calls)
        downstream = self._chunk(scene, "downstream", calls)
        upstream.get_param("p").value = 2.0
        downstream.get_param("p").value = "@upstream.p@"

        assert any(
            dep is upstream for _, dep in downstream.get_dependencies()
        ), "precondition: the param wire was reconciled into a DAG edge"

        prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(downstream)))
        assert not chunk_stage.exec_input_connections(prim), (
            "a param wire must not surface as an exec connection"
        )

    def test_exec_wired_chunks_are_ordered(self):
        scene, stage = _setup_stage()
        calls = []
        first = self._chunk(scene, "first", calls)
        second = self._chunk(scene, "second", calls)
        second.add_dependency(first)  # authors inputs:chunk on second

        ordered = chunk_stage.commit_order_from_stage([second, first], stage)

        assert ordered == [first, second]

    def test_unrelated_chunks_keep_their_order(self):
        scene, stage = _setup_stage()
        calls = []
        a = self._chunk(scene, "a", calls)
        b = self._chunk(scene, "b", calls)

        assert chunk_stage.commit_order_from_stage([b, a], stage) == [b, a]

    def test_cycle_keeps_every_chunk_exactly_once(self):
        """A cycle can't be ordered, but it must not drop or duplicate anyone.

        The connections are authored directly rather than through two mutually-referencing
        expression values: a live expression cycle sends the notice system into unbounded
        recursion long before any of this is reached, which is a separate problem from
        whether the ordering survives one.
        """
        scene, stage = _setup_stage()
        calls = []
        a = self._chunk(scene, "a", calls)
        b = self._chunk(scene, "b", calls)
        a.get_param("p").value = 1.0  # non-default, so the storage attrs exist
        b.get_param("p").value = 2.0

        for owner, other in ((a, b), (b, a)):
            attr = stage.GetAttributeAtPath(
                Sdf.Path(f"{chunk_stage.get_chunk_prim_path(owner)}.inputs:p")
            )
            attr.SetConnections(
                [Sdf.Path(f"{chunk_stage.get_chunk_prim_path(other)}.inputs:p")]
            )

        ordered = chunk_stage.commit_order_from_stage([a, b], stage)

        assert sorted(c.name for c in ordered) == ["a", "b"]
        assert len(ordered) == 2

    def test_no_stage_falls_back_to_depth(self):
        chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")
        a = chunk.Chunk("a")
        b = chunk.Chunk("b")
        b.add_dependency(a)

        assert chunk_stage.commit_order_from_stage([b, a], None) == [a, b]

    def test_commit_all_honours_the_order_hook(self):
        """The CLI passes this hook; the dispatch must actually use it."""
        scene, stage = _setup_stage()
        calls = []
        upstream = self._chunk(scene, "upstream", calls)
        downstream = self._chunk(scene, "downstream", calls)
        upstream.get_param("p").value = 2.0
        downstream.get_param("p").value = "@upstream.p@"

        chunk_scheduler.commit_all(
            [downstream, upstream],
            order=lambda cs: chunk_stage.commit_order_from_stage(cs, stage),
        )

        assert calls == ["upstream", "downstream"]
