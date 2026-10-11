from __future__ import annotations

import pytest

from usd_chunks.chunk_glob import (
    ANY,
    CLASS,
    GLOBSTAR,
    GLOBSTAR_DIR,
    GlobError,
    LITERAL,
    STAR,
    capture_count,
    expand,
    is_pattern,
    match,
    substitute,
    tokenize,
)


# ---------------------------------------------------------------------------
# is_pattern
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("assets/*.fbx", True),
        ("assets/**/*.fbx", True),
        ("assets/hero_??.fbx", True),
        ("assets/hero_[ab].fbx", True),
        ("assets/hero.fbx", False),
        ("", False),
        ("/geo/mesh", False),
        ("./assets/asset_A.usd", False),
    ],
)
def test_is_pattern_detects_only_glob_metacharacters(value: str, expected: bool) -> None:
    assert is_pattern(value) is expected


def test_is_pattern_ignores_non_strings() -> None:
    assert is_pattern(None) is False
    assert is_pattern(3) is False


# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pattern, kinds",
    [
        ("assets/*.fbx", [LITERAL, STAR, LITERAL]),
        ("assets/**/*.fbx", [LITERAL, GLOBSTAR_DIR, STAR, LITERAL]),
        ("**/*.fbx", [GLOBSTAR_DIR, STAR, LITERAL]),
        ("assets/**", [LITERAL, GLOBSTAR]),
        ("a?c.fbx", [LITERAL, ANY, LITERAL]),
        ("hero_[abc].fbx", [LITERAL, CLASS, LITERAL]),
        ("plain.fbx", [LITERAL]),
    ],
)
def test_tokenize_splits_into_literals_and_wildcards(pattern: str, kinds: list) -> None:
    assert [kind for kind, _ in tokenize(pattern)] == kinds


def test_tokenize_keeps_negated_class_body_intact() -> None:
    assert tokenize("x[!ab]y") == [(LITERAL, "x"), (CLASS, "[!ab]"), (LITERAL, "y")]


def test_tokenize_rejects_unterminated_class() -> None:
    with pytest.raises(GlobError):
        tokenize("assets/[ab.fbx")


@pytest.mark.parametrize(
    "pattern, count",
    [("assets/*.fbx", 1), ("assets/**/*.fbx", 2), ("**/*_[ab]?.fbx", 4), ("plain.fbx", 0)],
)
def test_capture_count_matches_wildcard_count(pattern: str, count: int) -> None:
    assert capture_count(pattern) == count


# ---------------------------------------------------------------------------
# match — ordered multi-capture
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pattern, path, captures",
    [
        ("assets/*.fbx", "assets/hero.fbx", ("hero",)),
        ("assets/*.fbx", "assets/sub/hero.fbx", None),
        ("assets/*.fbx", "other/hero.fbx", None),
        ("assets/*.fbx", "assets/hero.obj", None),
        # ** captures a multi-segment subpath, and collapses to "" at the top of the tree
        ("assets/**/*.fbx", "assets/char/hero.fbx", ("char", "hero")),
        ("assets/**/*.fbx", "assets/prop/crate.fbx", ("prop", "crate")),
        ("assets/**/*.fbx", "assets/tree.fbx", ("", "tree")),
        ("assets/**/*.fbx", "assets/a/b/c/leaf.fbx", ("a/b/c", "leaf")),
        # captures come out left-to-right, one per wildcard
        ("*/*/*.fbx", "assets/char/hero.fbx", ("assets", "char", "hero")),
        ("*_*_*.fbx", "a_b_c.fbx", ("a", "b", "c")),
        ("v*/o*/i*.usd", "v1/o2/i3.usd", ("1", "2", "3")),
        # single-char wildcards
        ("hero_?.fbx", "hero_1.fbx", ("1",)),
        ("hero_?.fbx", "hero_12.fbx", None),
        ("hero_[ab].fbx", "hero_a.fbx", ("a",)),
        ("hero_[ab].fbx", "hero_c.fbx", None),
        ("hero_[!ab].fbx", "hero_c.fbx", ("c",)),
        # mixed kinds keep their order
        ("**/v?/*.usd", "shots/sh01/v2/geo.usd", ("shots/sh01", "2", "geo")),
        ("**/v?/*.usd", "v2/geo.usd", ("", "2", "geo")),
        # trailing ** crosses separators
        ("assets/**", "assets/a/b.fbx", ("a/b.fbx",)),
        # matching is case-sensitive
        ("assets/*.fbx", "assets/hero.FBX", None),
    ],
)
def test_match_returns_ordered_captures(pattern: str, path: str, captures) -> None:
    assert match(pattern, path) == captures


def test_match_normalizes_windows_separators() -> None:
    assert match("assets/**/*.fbx", "assets\\char\\hero.fbx") == ("char", "hero")


# ---------------------------------------------------------------------------
# substitute
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pattern, captures, expected",
    [
        ("out/*.usda", ("hero",), "out/hero.usda"),
        ("out/**/*.usda", ("char", "hero"), "out/char/hero.usda"),
        ("out/**/*.usda", ("", "tree"), "out/tree.usda"),
        ("out/**/*.usda", ("a/b/c", "leaf"), "out/a/b/c/leaf.usda"),
        ("out/*/*.usda", ("char", "hero"), "out/char/hero.usda"),
        # captures are consumed left-to-right, whatever kind of wildcard receives them
        ("*/*/*.usda", ("a", "b", "c"), "a/b/c.usda"),
        ("out/*_*_*.usda", ("a", "b", "c"), "out/a_b_c.usda"),
        ("out/hero_?.usda", ("3",), "out/hero_3.usda"),
        ("out/hero_[ab].usda", ("b",), "out/hero_b.usda"),
        ("out/**", ("a/b.usda",), "out/a/b.usda"),
        ("literal.usda", (), "literal.usda"),
    ],
)
def test_substitute_fills_wildcards_in_order(pattern: str, captures: tuple, expected: str) -> None:
    assert substitute(pattern, captures) == expected


@pytest.mark.parametrize(
    "pattern, captures",
    [
        ("out/*.usda", ("char", "hero")),
        ("out/**/*.usda", ("hero",)),
        ("out/literal.usda", ("hero",)),
    ],
)
def test_substitute_rejects_a_wildcard_count_mismatch(pattern: str, captures: tuple) -> None:
    with pytest.raises(GlobError):
        substitute(pattern, captures)


@pytest.mark.parametrize(
    "source_pattern, sink_pattern, path, expected",
    [
        ("assets/**/*.fbx", "out/**/*.usda", "assets/char/hero.fbx", "out/char/hero.usda"),
        ("assets/**/*.fbx", "out/**/*.usda", "assets/prop/crate.fbx", "out/prop/crate.usda"),
        ("assets/**/*.fbx", "out/**/*.usda", "assets/tree.fbx", "out/tree.usda"),
        ("assets/*.fbx", "out/*.usda", "assets/tree.fbx", "out/tree.usda"),
        ("in/*/*.abc", "out/*/*.usd", "in/shot/take.abc", "out/shot/take.usd"),
    ],
)
def test_match_then_substitute_round_trips(source_pattern, sink_pattern, path, expected) -> None:
    captures = match(source_pattern, path)
    assert captures is not None
    assert substitute(sink_pattern, captures) == expected


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------

@pytest.fixture
def asset_tree(tmp_path):
    for rel in [
        "assets/tree.fbx",
        "assets/char/hero.fbx",
        "assets/prop/crate.fbx",
        "assets/prop/deep/bolt.fbx",
        "assets/notes.txt",
        "other/stray.fbx",
    ]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    (tmp_path / "assets" / "empty_dir").mkdir()
    return tmp_path


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("assets/*.fbx", [("assets/tree.fbx", ("tree",))]),
        (
            "assets/**/*.fbx",
            [
                ("assets/char/hero.fbx", ("char", "hero")),
                ("assets/prop/crate.fbx", ("prop", "crate")),
                ("assets/prop/deep/bolt.fbx", ("prop/deep", "bolt")),
                ("assets/tree.fbx", ("", "tree")),
            ],
        ),
        ("assets/*/*.fbx", [("assets/char/hero.fbx", ("char", "hero")), ("assets/prop/crate.fbx", ("prop", "crate"))]),
        ("assets/*.usd", []),
        ("missing/*.fbx", []),
        ("**/stray.fbx", [("other/stray.fbx", ("other",))]),
    ],
)
def test_expand_finds_existing_files_sorted(asset_tree, pattern, expected) -> None:
    assert expand(pattern, str(asset_tree)) == expected


def test_expand_never_returns_directories(asset_tree) -> None:
    assert expand("assets/*", str(asset_tree)) == [
        ("assets/notes.txt", ("notes.txt",)),
        ("assets/tree.fbx", ("tree.fbx",)),
    ]


# ---------------------------------------------------------------------------
# ./-prefixed patterns
#
# Saved networks author paths in this form already — chunkgroups/examples/
# variant_switch.usda holds `string inputs:file_path = "./assets/asset_A.usd"`.
# The walk yields root-relative paths with no such prefix, so before this was
# handled the most common real-world pattern matched nothing at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefixed, bare",
    [
        ("./assets/*.fbx", "assets/*.fbx"),
        ("./assets/**/*.fbx", "assets/**/*.fbx"),
        ("./**/stray.fbx", "**/stray.fbx"),
    ],
)
def test_expand_dot_slash_captures_match_bare_pattern(asset_tree, prefixed, bare) -> None:
    """A leading ./ changes the shape of the path, never the captures."""
    got = expand(prefixed, str(asset_tree))
    assert [captures for _, captures in got] == [captures for _, captures in expand(bare, str(asset_tree))]
    assert got, "expected the ./-prefixed pattern to match the same files as the bare one"


def test_expand_dot_slash_keeps_prefix_on_results(asset_tree) -> None:
    """The concrete path stays in the shape the pattern was written in, so what gets
    authored back onto the chunk prim is still relative to the network."""
    assert expand("./assets/*.fbx", str(asset_tree)) == [("./assets/tree.fbx", ("tree",))]


def test_expand_dot_slash_on_a_pattern_that_matches_nothing(asset_tree) -> None:
    assert expand("./missing/*.fbx", str(asset_tree)) == []


# ---------------------------------------------------------------------------
# absolute patterns
#
# An override on the command line is the natural place to write one
# (`usdchunks batch net.usda Reference.file_path='/myassets/*.usd'`), and it
# has to work without regard to where the network file happens to live.
# ---------------------------------------------------------------------------


def test_expand_absolute_pattern_ignores_root(asset_tree) -> None:
    """An absolute pattern carries its own root, so the anchor is not consulted."""
    pattern = f"{str(asset_tree).replace(chr(92), '/')}/assets/*.fbx"
    from_anchor = expand(pattern, str(asset_tree))
    from_elsewhere = expand(pattern, str(asset_tree / "other"))
    assert from_anchor == from_elsewhere
    assert [captures for _, captures in from_anchor] == [("tree",)]


def test_expand_absolute_pattern_returns_absolute_paths(asset_tree) -> None:
    """Shape is preserved: an absolute pattern yields absolute paths, since that is what
    gets authored back onto the chunk prim."""
    root = str(asset_tree).replace(chr(92), "/")
    got = expand(f"{root}/assets/*.fbx", str(asset_tree))
    assert got == [(f"{root}/assets/tree.fbx", ("tree",))]


def test_expand_absolute_globstar(asset_tree) -> None:
    root = str(asset_tree).replace(chr(92), "/")
    got = expand(f"{root}/assets/**/*.fbx", str(asset_tree))
    assert [captures for _, captures in got] == [
        ("char", "hero"),
        ("prop", "crate"),
        ("prop/deep", "bolt"),
        ("", "tree"),
    ]
    assert all(path.startswith(root) for path, _ in got)


def test_expand_absolute_pattern_matching_nothing(asset_tree) -> None:
    root = str(asset_tree).replace(chr(92), "/")
    assert expand(f"{root}/missing/*.fbx", str(asset_tree)) == []
