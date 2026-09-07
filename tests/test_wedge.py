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


class TestColour:
    def test_colours_are_returned_linear_not_srgb(self):
        """base_color is linear. Handing sRGB values over as linear lifts every dark
        channel and the paint goes pastel -- a wrong-looking render rather than a failure,
        so nothing catches it but a test."""
        assert wedge._srgb_to_linear(0.5) == pytest.approx(0.2140, abs=1e-3)
        assert wedge._srgb_to_linear(0.0) == 0.0
        assert wedge._srgb_to_linear(1.0) == pytest.approx(1.0)

    def test_a_colour_is_three_channels_in_range(self):
        node = _wedge(colours=["CarPaint"])
        rgb = wedge.sample(node, 11)["CarPaint"]
        assert len(rgb) == 3
        assert all(0.0 <= c <= 1.0 for c in rgb)

    def test_hue_does_not_track_saturation(self):
        """Each channel is keyed by a different name, so two colours on one wedge -- and the
        h/s/v within one colour -- do not move together across variants."""
        node = _wedge(colours=["CarPaint", "Rim"])
        paint = [wedge.sample(node, i)["CarPaint"] for i in range(30)]
        rim = [wedge.sample(node, i)["Rim"] for i in range(30)]
        assert paint != rim

    def test_the_bands_are_honoured(self):
        node = _wedge(colours=["CarPaint"])
        node.set_blocked(True)
        try:
            node.get_param("val_max").value = 0.25
            node.get_param("sat_min").value = 0.0
            node.get_param("sat_max").value = 0.0
        finally:
            node.set_blocked(False)
        # Zero saturation makes r == g == b, so the band is directly checkable -- against
        # the *linearised* ceiling, because base_color is linear and sample() converts.
        ceiling = wedge._srgb_to_linear(0.25)
        for i in range(50):
            r, g, b = wedge.sample(node, i)["CarPaint"]
            assert r == pytest.approx(g) == pytest.approx(b)
            assert r <= ceiling + 1e-6


def test_execute_writes_the_sample_onto_the_range_params():
    node = _wedge(index=4, ranges=[("tz", 1.6, 2.9)])
    wedge.execute(node)
    assert node.get_param("tz").value == pytest.approx(wedge.sample(node, 4)["tz"])


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
