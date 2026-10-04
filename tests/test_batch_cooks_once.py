"""A batch of changes cooks each chunk below it once, expressions included.

A Wedge sets every range inside one batch, and the first solve of that batch cooks
everything below with every new value in place -- expressions resolve on read. The params
reading those ranges through expressions re-announce their values only once the ranges
have changed, i.e. during that first solve, so their solves queued up behind it and cooked
every chunk below a second time with the same values: two cooks per chunk per change.
"""

from __future__ import annotations

import collections

from usd_chunks import Scene, chunk_notice, chunk_registry


def _count_cooks(module_name, counts):
    # The registry's copy: the one it calls execute on. Scanning sys.modules instead
    # touched every lazily imported module there, and loading kitti_writer that way fails
    # without OpenEXR.
    module = chunk_registry.ChunkRegistry().get_chunk_module(module_name).module
    original = module.execute

    def counted(node, *args, **kwargs):
        counts[node.name] += 1
        return original(node, *args, **kwargs)

    module.execute = counted
    return lambda: setattr(module, "execute", original)


def test_a_wedge_change_cooks_what_it_drives_once():
    with Scene() as scene:
        wedge = scene.create.wedge(seed=3, count=8, ranges="x 1 9\ny 1 9\nz 1 9")
        place = scene.create.transform(wedge, tx="@Wedge.x@", ty="@Wedge.y@", tz="@Wedge.z@")
        wedge.get_param("current_index").value = 1  # past the first change's binding

        counts = collections.Counter()
        restore = _count_cooks("transform", counts)
        try:
            wedge.get_param("current_index").value = 2
        finally:
            restore()

        assert counts[place.name] == 1, f"cooked {counts[place.name]} times for one change"
        assert place.get_param("tx").value == wedge.get_param("x").value


def test_deferred_work_runs_once_after_the_batch():
    """Asked for under one key any number of times, it runs once, after the solves."""
    runs = []
    assert chunk_notice.defer_to_batch_end("k", lambda: runs.append("now")) is False
    with chunk_notice.batched_solve():
        for _ in range(5):
            assert chunk_notice.defer_to_batch_end("k", lambda: runs.append("batch"))
        assert runs == []
    assert runs == ["batch"]
