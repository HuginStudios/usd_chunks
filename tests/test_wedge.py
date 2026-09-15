"""The wedge samples the same values every time, everywhere.

Everything the wedge is for rests on one property: a value is a pure function of
`(seed, index, range name)`. That is what lets a 50KB recipe stand in for the dataset it
generates -- variant 5,432 regenerates on its own, on another machine, in any order, without
producing the 5,431 before it.

A sequential RNG would pass a naive "does a full run reproduce" check and fail every one of
those, so the tests below pin the properties individually rather than just the happy path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wedge  # noqa: E402

from usd_chunks import chunk  # noqa: E402

_FLAGS = chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED


def _wedge(seed=7, index=0, ranges=(), colours=()):
    """A wedge built outside a scene.

    Blocked while populated: every assignment otherwise tries to cook, reaching
    ChunkRegistry().scene, which no test has.
    """
    node = wedge.main()
    node.set_blocked(True)
    try:
        node.get_param("seed").value = seed
        node.get_param("current_index").value = index
    finally:
        node.set_blocked(False)
    for name, low, high in ranges:
        node.add_param(chunk.Slider(name, name, default_value=low, min_value=low,
                                    max_value=high, flags=_FLAGS))
    for name in colours:
        node.add_param(chunk.ColorPicker(name, name, default_value=(0.5, 0.5, 0.5), flags=_FLAGS))
    return node


class TestUnit:
    def test_the_same_triple_always_gives_the_same_number(self):
        assert wedge._unit(42, 7, "tz") == wedge._unit(42, 7, "tz")

    def test_index_seed_and_name_each_change_it(self):
        base = wedge._unit(42, 7, "tz")
        assert wedge._unit(42, 8, "tz") != base
        assert wedge._unit(43, 7, "tz") != base
        assert wedge._unit(42, 7, "mass") != base

    def test_it_stays_inside_the_unit_interval(self):
        values = [wedge._unit(1, i, "x") for i in range(500)]
        assert all(0.0 <= v < 1.0 for v in values)

    def test_it_hashes_stably(self):
        """Python randomises string hashing per process unless PYTHONHASHSEED is set, so a
        built-in `hash(name)` here would reproduce perfectly within one run and silently
        never across two -- the hardest version of this bug to notice. The behavioural
        check is test_the_values_are_identical_in_a_separate_process; this one just pins
        that a stable digest is what is reached for."""
        assert "hashlib.sha256" in Path(wedge.__file__).read_text(encoding="utf-8")


class TestSampling:
    def test_a_value_lands_inside_its_range(self):
        node = _wedge(ranges=[("tz", 1.6, 2.9)])
        for i in range(200):
            assert 1.6 <= wedge.sample(node, i)["tz"] <= 2.9

    def test_an_inverted_range_is_swapped_not_rejected(self):
        node = _wedge(ranges=[("tz", 2.9, 1.6)])
        assert 1.6 <= wedge.sample(node, 3)["tz"] <= 2.9

    def test_an_int_range_yields_ints(self):
        node = _wedge()
        node.add_param(chunk.Slider("floors", "floors", default_value=1, min_value=1,
                                    max_value=6, is_int=True, flags=_FLAGS))
        assert isinstance(wedge.sample(node, 5)["floors"], int)

    def test_one_variant_does_not_depend_on_the_ones_before_it(self):
        """The property a running RNG would break: 5,432 sampled alone must equal 5,432
        sampled after the 5,431 before it."""
        alone = _wedge(ranges=[("tz", 0.0, 1.0)])
        direct = wedge.sample(alone, 5432)["tz"]

        walked = _wedge(ranges=[("tz", 0.0, 1.0)])
        for i in range(5433):
            wedge.sample(walked, i)

        assert direct == wedge.sample(walked, 5432)["tz"]
        # ...and the walk really did visit different numbers on the way.
        assert wedge.sample(walked, 5431)["tz"] != direct


class TestRanges:
    def test_groups_are_not_mistaken_for_ranges(self):
        """GroupFlags and ParamFlags are separate IntFlag enums whose bits overlap, so a
        Group answers has_flag(ParamFlags.OUTPUT) with True. Reached as a range it dies on
        `Group has no attribute min_value`."""
        node = _wedge(ranges=[("tz", 0.0, 1.0)])
        assert [p.name for p in wedge._ranges(node)] == ["tz"]

    def test_the_controls_are_not_ranges(self):
        """seed, current_index and count are editable inputs, not things to vary."""
        node = _wedge(ranges=[("tz", 0.0, 1.0)])
        names = {p.name for p in wedge._ranges(node)}
        assert names.isdisjoint({"seed", "current_index", "count"})


def _row(node, name, kind=wedge.KIND_NUMBER, low=0.0, high=1.0, is_int=False, prim="", vset="", weights=""):
    """Add a row to the Ranges list, the way the UI's + button and its fields do."""
    rows = node.get_param("range_rows")
    node.set_blocked(True)
    try:
        rows.size += 1
        i = rows.size - 1
        for field, value in (("r_name", name), ("r_kind", kind), ("r_min", low), ("r_max", high),
                             ("r_int", is_int), ("r_prim", prim), ("r_set", vset), ("r_weights", weights)):
            rows.get_indexed_param(rows.get_param(field), i).value = value
    finally:
        node.set_blocked(False)
    return i


class TestColour:
    """A Colour row samples each channel between its Min Colour and Max Colour."""

    def test_a_colour_is_three_channels_in_range(self):
        node = _wedge(colours=["CarPaint"])
        rgb = wedge.sample(node, 11)["CarPaint"]
        assert len(rgb) == 3
        assert all(0.0 <= c <= 1.0 for c in rgb)

    def test_channels_do_not_track_each_other(self):
        """Each channel is keyed by its own name, so two colours on one wedge -- and the
        r/g/b within one colour -- do not move together across variants."""
        node = _wedge(colours=["CarPaint", "Rim"])
        paint = [wedge.sample(node, i)["CarPaint"] for i in range(30)]
        rim = [wedge.sample(node, i)["Rim"] for i in range(30)]
        assert paint != rim
        reds = [c[0] for c in paint]
        greens = [c[1] for c in paint]
        assert reds != greens

    def test_the_per_channel_range_is_honoured(self):
        """Every shade of red: R wide, G and B held narrow. A channel with Min == Max is
        held exactly."""
        node = _wedge()
        _row(node, "CarPaint", kind=wedge.KIND_COLOUR)
        rows = node.get_param("range_rows")
        node.set_blocked(True)
        try:
            rows.get_indexed_param(rows.get_param("r_col_min"), 0).value = (0.4, 0.0, 0.05)
            rows.get_indexed_param(rows.get_param("r_col_max"), 0).value = (1.0, 0.1, 0.05)
        finally:
            node.set_blocked(False)
        wedge._sync_ranges(node)

        for i in range(50):
            r, g, b = wedge.sample(node, i)["CarPaint"]
            assert 0.4 <= r <= 1.0
            assert 0.0 <= g <= 0.1
            assert b == pytest.approx(0.05)

    def test_inverted_channel_bounds_are_swapped_not_rejected(self):
        node = _wedge()
        _row(node, "Tint", kind=wedge.KIND_COLOUR)
        rows = node.get_param("range_rows")
        node.set_blocked(True)
        try:
            rows.get_indexed_param(rows.get_param("r_col_min"), 0).value = (1.0, 1.0, 1.0)
            rows.get_indexed_param(rows.get_param("r_col_max"), 0).value = (0.5, 0.5, 0.5)
        finally:
            node.set_blocked(False)
        wedge._sync_ranges(node)
        for i in range(20):
            assert all(0.5 <= c <= 1.0 for c in wedge.sample(node, i)["Tint"])


def test_execute_writes_the_sample_onto_the_range_params():
    node = _wedge(index=4)
    _row(node, "tz", low=1.6, high=2.9)
    wedge.execute(node)
    assert node.get_param("tz").value == pytest.approx(wedge.sample(node, 4)["tz"])


class TestRows:
    """The Ranges list is the data; the lofted params are a view of it, and follow edits."""

    def test_a_row_lofts_a_range_param(self):
        node = _wedge()
        _row(node, "tz", low=1.6, high=2.9)
        wedge._sync_ranges(node)
        tz = node.get_param("tz")
        assert tz is not None and (tz.min_value, tz.max_value) == (1.6, 2.9)
        assert tz.has_flag(chunk.ChunkFlags.ParamFlags.OUTPUT)

    def test_editing_the_bounds_moves_the_same_param(self):
        """In place, not re-lofted: the outport's wires must survive a bounds edit."""
        node = _wedge()
        i = _row(node, "tz", low=1.6, high=2.9)
        wedge._sync_ranges(node)
        before = node.get_param("tz")

        rows = node.get_param("range_rows")
        node.set_blocked(True)
        rows.get_indexed_param(rows.get_param("r_max"), i).value = 5.0
        node.set_blocked(False)
        wedge._sync_ranges(node)

        assert node.get_param("tz") is before
        assert node.get_param("tz").max_value == 5.0
        assert 1.6 <= wedge.sample(node, 3)["tz"] <= 5.0

    def test_integer_rows_loft_int_sliders_and_sample_ints(self):
        node = _wedge()
        _row(node, "floors", low=1, high=6, is_int=True)
        wedge._sync_ranges(node)
        assert node.get_param("floors").is_int
        for i in range(20):
            v = wedge.sample(node, i)["floors"]
            assert isinstance(v, int) and 1 <= v <= 6

    def test_switching_integer_relofts_the_param(self):
        node = _wedge()
        i = _row(node, "n", low=0, high=10)
        wedge._sync_ranges(node)
        assert not node.get_param("n").is_int

        rows = node.get_param("range_rows")
        node.set_blocked(True)
        rows.get_indexed_param(rows.get_param("r_int"), i).value = True
        node.set_blocked(False)
        wedge._sync_ranges(node)
        assert node.get_param("n").is_int

    def test_removing_a_row_removes_its_param(self):
        node = _wedge()
        _row(node, "a")
        _row(node, "b")
        wedge._sync_ranges(node)
        assert {p.name for p in wedge._ranges(node)} == {"a", "b"}

        node.get_param("range_rows").remove_index(0)
        wedge._sync_ranges(node)
        assert {p.name for p in wedge._ranges(node)} == {"b"}

    def test_a_row_without_a_name_is_not_a_range_yet(self):
        node = _wedge()
        _row(node, "")
        wedge._sync_ranges(node)
        assert wedge._ranges(node) == []

    def test_legacy_text_is_imported_into_rows(self):
        """Files saved before the list carry the spec as text; setting it still works."""
        node = _wedge()
        node.set_blocked(True)
        node.get_param("ranges").value = (
            "height 1 9\nCarPaint color\nPaint variants /World/car color 3,1,1"
        )
        node.set_blocked(False)
        wedge._import_spec(node)
        wedge._sync_ranges(node)

        rows = wedge._rows(node)
        assert [r["name"] for r in rows] == ["height", "CarPaint", "Paint"]
        assert rows[0] == {"name": "height", "kind": "float", "low": 1.0, "high": 9.0, "int": False}
        assert rows[1]["kind"] == "color"
        assert rows[2] == {"name": "Paint", "kind": "variants", "prim": "/World/car", "set": "color",
                           "weights": [3.0, 1.0, 1.0]}
        assert node.get_param("ranges").value == "", "imported, then emptied"
        assert isinstance(node.get_param("CarPaint"), chunk.ColorPicker)


def test_the_values_are_identical_in_a_separate_process():
    """The claim in full: same seed and index, different process, same numbers.

    In-process equality proves nothing about a `hash()` slip, because PYTHONHASHSEED is
    fixed for the life of an interpreter.
    """
    script = (
        "import sys;"
        f"sys.path.insert(0, {str(Path(wedge.__file__).parent)!r});"
        "import wedge;"
        "print([round(wedge._unit(7, i, 'tz'), 12) for i in range(5)])"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str([round(wedge._unit(7, i, "tz"), 12) for i in range(5)])


class TestWeights:
    def test_a_heavier_option_is_picked_more_often(self):
        """3,1,1 should land on the first option about three times in five."""
        counts = [0, 0, 0]
        for i in range(3000):
            counts[wedge._weighted_pick(wedge._unit(1, i, "Paint"), [3.0, 1.0, 1.0])] += 1
        share = counts[0] / sum(counts)
        assert 0.55 < share < 0.65, counts
        assert all(c > 0 for c in counts), counts

    def test_uniform_weights_match_uniform_selection(self):
        counts = [0, 0, 0]
        for i in range(3000):
            counts[wedge._weighted_pick(wedge._unit(2, i, "Paint"), [1.0, 1.0, 1.0])] += 1
        assert all(0.28 < c / sum(counts) < 0.39 for c in counts), counts

    def test_a_zero_weight_is_never_picked(self):
        picks = {wedge._weighted_pick(wedge._unit(3, i, "P"), [1.0, 0.0, 1.0]) for i in range(500)}
        assert 1 not in picks

    def test_it_stays_in_range_at_the_boundaries(self):
        assert wedge._weighted_pick(0.0, [1.0, 1.0]) == 0
        assert wedge._weighted_pick(0.999999, [1.0, 1.0]) == 1
        # Degenerate rather than a crash: nothing to choose between.
        assert wedge._weighted_pick(0.5, [0.0, 0.0]) == 0

    def test_weights_are_read_off_the_spec(self):
        row = wedge._parse_spec("Paint variants /World/car color 3,1,1")[0]
        assert row["weights"] == [3.0, 1.0, 1.0]

    def test_a_spec_without_weights_carries_none(self):
        """Absent means uniform, so nothing downstream has to special-case a default."""
        row = wedge._parse_spec("Paint variants /World/car color")[0]
        assert "weights" not in row

    def test_unreadable_weights_fall_back_rather_than_fail(self):
        row = wedge._parse_spec("Paint variants /World/car color 3,oops,1")[0]
        assert "weights" not in row
        assert row["kind"] == "variants"
