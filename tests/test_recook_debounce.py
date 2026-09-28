from __future__ import annotations

from PySide6 import QtWidgets

from usd_chunks.frontends.qt.nodegraph import ChunkGraphScene, ChunkGraphView


def test_chunk_deleted_debounces_recook_across_many_notices(qapp, monkeypatch) -> None:
    """Regression: deleting a chunk fires CHUNK_DELETED once per param it owns
    (ChunkGroupBase.request_delete() cascades request_delete() through every child), so a
    chunk with many params (e.g. the RBD solver, ~30 params) calls
    ChunkGraphScene._on_chunk_deleted that many times for one delete. The sibling
    _schedule_rebuild() call is already debounced (a "pending" guard collapses a burst into
    one deferred rebuild) but the _recook_active_view scheduling right next to it wasn't --
    each of those ~30 notices queued its own independent recook. Harmless when upstream
    chunks were side-effect-free on cook, but once a chunk (e.g. Scatter) became reactive,
    each redundant recook re-ran it for real -- looking like a runaway loop when deleting
    anything with many params.
    """
    scene = ChunkGraphScene()
    view = ChunkGraphView()
    view.setScene(scene)

    recook_calls = []
    monkeypatch.setattr(
        ChunkGraphView, "_recook_active_view", lambda self, changed_chunks=None: recook_calls.append(1)
    )

    # Simulate the burst: one CHUNK_DELETED notice per param on the deleted chunk.
    for _ in range(30):
        scene._on_chunk_deleted(None, None)

    assert recook_calls == [], "recook must be deferred, not run synchronously inside the notice handler"

    QtWidgets.QApplication.instance().processEvents()

    assert len(recook_calls) == 1, (
        f"a burst of {30} CHUNK_DELETED notices from one delete must collapse into a single "
        f"recook, got {len(recook_calls)}"
    )
