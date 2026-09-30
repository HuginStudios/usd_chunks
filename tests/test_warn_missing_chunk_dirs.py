"""An empty USD_CHUNK_DIRS says so in the log, once.

Without it a script failed later with "No chunk module or variant called 'cube'", and in
usdview the chunk menu was just short of chunks, with nothing saying why.
"""

from __future__ import annotations

import logging

import pytest

from usd_chunks import Scene, chunk_registry


@pytest.fixture
def fresh_warning(monkeypatch):
    monkeypatch.setattr(chunk_registry, "_warned_no_chunk_dirs", False, raising=False)


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if "Set USD_CHUNK_DIRS!" in r.getMessage()]


def test_empty_chunk_dirs_warns_once(monkeypatch, caplog, fresh_warning) -> None:
    monkeypatch.delenv("USD_CHUNK_DIRS", raising=False)
    with caplog.at_level(logging.WARNING):
        with Scene():
            pass
        with Scene():
            pass

    assert len(_warnings(caplog)) == 1


def test_set_chunk_dirs_does_not_warn(caplog, fresh_warning) -> None:
    with caplog.at_level(logging.WARNING), Scene():
        pass

    assert _warnings(caplog) == []
