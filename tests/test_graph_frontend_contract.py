"""Every graph frontend implements the same verbs.

This is the enforcement half of chunk_graph.GraphFrontend. The Protocol documents the
contract; a Protocol cannot make anyone obey it, and on the Qt side it cannot even be
inherited -- ABCMeta and PySide6's metaclass conflict, and the usual combined-metaclass
workaround constructs but skips the abstract check entirely. So the check is a test.

It exists because the second frontend silently lacked half of the first one for months,
including input handlers that raised on first touch. A third (QML, ImGui, a web client)
should find out from a red test, not from a user.
"""
from __future__ import annotations

import inspect

import pytest

from usd_chunks.plugins import chunk_graph
from usd_chunks.plugins import chunk_flet_nodegraph as fg


def frontends():
    """(name, class) for every graph frontend that claims to implement the contract.

    Add a new frontend here the day you start it: the failures are the to-do list.
    """
    found = [("flet", fg.GraphView)]
    try:
        from usd_chunks.plugins import chunk_qt_nodegraph as qg
    except ImportError:  # pragma: no cover - Qt not installed
        return found
    facade = getattr(qg, "QtGraphFrontend", None)
    if facade is not None:
        found.append(("qt", facade))
    return found


FRONTENDS = frontends()


@pytest.mark.parametrize("name,cls", FRONTENDS, ids=[n for n, _ in FRONTENDS])
class TestConformance:
    def test_implements_every_verb(self, name, cls):
        """Reported as a list, not one failure at a time, so a new frontend sees the whole
        job at once rather than discovering it a name per run."""
        missing = [m for m in chunk_graph.GRAPH_FRONTEND_METHODS if not hasattr(cls, m)]
        assert not missing, f"{name} is missing {len(missing)} verb(s): {missing}"

    def test_every_verb_is_callable(self, name, cls):
        not_callable = [m for m in chunk_graph.GRAPH_FRONTEND_METHODS
                        if not callable(getattr(cls, m, None))]
        assert not not_callable, f"{name} has non-callable verbs: {not_callable}"

    def test_signatures_match_the_protocol(self, name, cls):
        """Names alone are not a contract.

        runtime_checkable isinstance() only checks that an attribute exists -- a
        delete_selected(self, force) would satisfy it and then break every caller. This
        compares the positional parameters against the Protocol's own.
        """
        problems = []
        for verb in chunk_graph.GRAPH_FRONTEND_METHODS:
            want = inspect.signature(getattr(chunk_graph.GraphFrontend, verb))
            got = inspect.signature(getattr(cls, verb))
            want_required = [
                p.name for p in want.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            got_required = [
                p.name for p in got.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            if want_required != got_required:
                problems.append(f"{verb}: contract{want_required} vs {name}{got_required}")
        assert not problems, "signature drift:\n  " + "\n  ".join(problems)


class TestTheContractItself:
    def test_the_verb_list_matches_the_protocol(self):
        """GRAPH_FRONTEND_METHODS is what the test iterates; the Protocol is what a person
        reads. If they drift, the documentation stops describing what is enforced."""
        declared = {n for n, v in vars(chunk_graph.GraphFrontend).items()
                    if callable(v) and not n.startswith("_")}
        assert declared == set(chunk_graph.GRAPH_FRONTEND_METHODS)

    def test_rendering_is_not_in_the_contract(self):
        """Drawing and input have no shared shape across toolkits -- Flet computes shapes
        and positions controls, Qt builds self-placing QGraphicsItems, QML binds delegates.
        Requiring any of them would smuggle one toolkit's design into the contract."""
        for excluded in ("shapes", "node_controls", "port_xy", "on_tap", "on_key",
                         "on_scroll", "refresh"):
            assert excluded not in chunk_graph.GRAPH_FRONTEND_METHODS

    def test_an_incomplete_frontend_is_detected(self):
        """The test above can only be trusted if it can fail. This is a frontend that
        implements everything but one verb."""
        class Incomplete:
            pass

        for verb in chunk_graph.GRAPH_FRONTEND_METHODS[:-1]:
            setattr(Incomplete, verb, lambda self, *a, **k: None)

        missing = [m for m in chunk_graph.GRAPH_FRONTEND_METHODS
                   if not hasattr(Incomplete, m)]
        assert missing == [chunk_graph.GRAPH_FRONTEND_METHODS[-1]]

    def test_isinstance_agrees_for_a_real_frontend(self, tmp_path):
        """runtime_checkable works for the cheap case; the signature test above covers
        what it cannot see."""
        from graph_fixtures import make_network
        path, context = make_network(tmp_path / "contract.usda")
        view = fg.GraphView(path, context)
        view.live = False
        assert isinstance(view, chunk_graph.GraphFrontend)
