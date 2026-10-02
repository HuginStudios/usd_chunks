"""${SHOT} in a render output path resolves, so one graph renders a whole sequence.

Asked for directly: "for the render products output file I tried something like this:
{$SEQ}_{$SHOT}.png but that didn't work, would be nice if it did then we could just switch
the stage_expression and hit render".

It could not work, and not because of the spelling. USD substitutes expression variables
into **asset paths and variant selections**; a RenderProduct's `productName` is a `token`,
and a render output filename is not a composition input at all -- it is a file we open. So
`${SHOT}` there stayed literal text however it was written, and ten shots meant retyping
the filename ten times.

So usd_chunks resolves it, in one named place (chunk_utils.resolve_output_path), which is a
mechanism of ours rather than a claim about OpenUSD -- the same honest split the Stage
Expression Switch draws.
"""

from __future__ import annotations

import importlib

import pytest
from pxr import Usd, UsdRender

from usd_chunks import chunk_utils

render_product_mod = importlib.import_module("chunks.usd.render_product")


@pytest.fixture
def stage():
    stage = Usd.Stage.CreateInMemory()
    chunk_utils.set_stage_expression_variables(stage, {"SEQ": "sq010", "SHOT": "sh030"})
    return stage


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------

def test_it_substitutes_the_variables(stage):
    assert chunk_utils.resolve_output_path(r"C:\out\${SEQ}_${SHOT}.png", stage) == (
        r"C:\out\sq010_sh030.png"
    )


def test_an_unknown_variable_is_left_as_written(stage):
    """Rather than blanked. Every shot collapsing onto "C:\\out\\_sh030.png" is a silent
    overwrite; a filename with ${TAKE} still in it says which variable is missing."""
    assert chunk_utils.resolve_output_path(r"C:\out\${TAKE}_${SHOT}.png", stage) == (
        r"C:\out\${TAKE}_sh030.png"
    )


def test_a_path_with_no_variables_is_untouched(stage):
    assert chunk_utils.resolve_output_path(r"C:\out\beauty.####.exr", stage) == (
        r"C:\out\beauty.####.exr"
    )


def test_an_empty_path_is_untouched(stage):
    assert chunk_utils.resolve_output_path("", stage) == ""


def test_the_reversed_spelling_is_reported(stage, caplog):
    """{$SEQ} is what people reach for first, and it is backwards. It looks close enough to
    right that the braces simply end up in the filename, which reads as a broken renderer --
    so say so by name rather than leaving them to spot it on disk."""
    with caplog.at_level("WARNING"):
        result = chunk_utils.resolve_output_path(r"C:\out\{$SEQ}_{$SHOT}.png", stage)

    assert result == r"C:\out\{$SEQ}_{$SHOT}.png", "left alone, so the name is visibly wrong"
    assert "${SEQ}" in caplog.text and "{$SEQ}" in caplog.text


def test_both_spellings_in_one_path_still_resolve_the_right_one(stage):
    out = chunk_utils.resolve_output_path(r"C:\out\{$SEQ}_${SHOT}.png", stage)
    assert out == r"C:\out\{$SEQ}_sh030.png"


# ---------------------------------------------------------------------------
# the USD spelling: the whole path in backticks, as in an asset path
# ---------------------------------------------------------------------------

def test_a_backtick_expression_is_evaluated_by_usd(stage):
    out = chunk_utils.resolve_output_path('`"C:/out/${SEQ}_${SHOT}.png"`', stage)
    assert out == "C:/out/sq010_sh030.png"


def test_a_backtick_expression_can_branch(stage):
    text = '`if(eq(${SHOT}, "sh030"), "C:/out/night.png", "C:/out/day.png")`'
    assert chunk_utils.resolve_output_path(text, stage) == "C:/out/night.png"


def test_doubled_backslashes_are_windows_separators(stage):
    out = chunk_utils.resolve_output_path(r'`"C:\\out\\${SHOT}.png"`', stage)
    assert out == r"C:\out\sh030.png"


def test_an_unknown_variable_in_a_backtick_expression_leaves_the_path(stage, caplog):
    """USD evaluates an unset variable to its own name without an error, so `"${TAKE}.png"`
    would quietly become "TAKE.png" and every shot would write over the same file."""
    text = '`"C:/out/${TAKE}_${SHOT}.png"`'
    with caplog.at_level("WARNING"):
        assert chunk_utils.resolve_output_path(text, stage) == text
    assert "TAKE" in caplog.text


def test_single_backslashes_in_a_backtick_expression_are_reported(stage, caplog):
    r"""\o and \$ are escapes in a USD string, so this would evaluate to "C:out${SHOT}.png"
    with no error from USD -- separators gone and the variable never substituted."""
    text = r'`"C:\out\${SHOT}.png"`'
    with caplog.at_level("WARNING"):
        assert chunk_utils.resolve_output_path(text, stage) == text
    assert "backslash" in caplog.text


def test_a_backtick_expression_that_is_not_a_string_is_left(stage, caplog):
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh030", "N": 3})
    with caplog.at_level("WARNING"):
        assert chunk_utils.resolve_output_path("`${N}`", stage) == "`${N}`"
    assert "not a path" in caplog.text


# ---------------------------------------------------------------------------
# through the Render Product chunk, which is where it was asked for
# ---------------------------------------------------------------------------

def test_the_product_authors_the_resolved_name(stage):
    """What actually reaches the renderer: productName on the prim, already substituted."""
    product = UsdRender.Product.Define(stage, "/Render/Products/beauty")
    product.CreateProductNameAttr().Set(
        chunk_utils.resolve_output_path(r"C:\out\${SEQ}_${SHOT}.png", stage)
    )

    assert product.GetProductNameAttr().Get() == r"C:\out\sq010_sh030.png"


def test_changing_the_shot_changes_the_filename(stage):
    """The point of the whole thing: set the Stage Expression, press Render, get a
    differently-named file -- with nothing else touched."""
    first = chunk_utils.resolve_output_path(r"C:\out\${SEQ}_${SHOT}.png", stage)

    chunk_utils.set_stage_expression_variables(stage, {"SEQ": "sq020", "SHOT": "sh050"})
    second = chunk_utils.resolve_output_path(r"C:\out\${SEQ}_${SHOT}.png", stage)

    assert first == r"C:\out\sq010_sh030.png"
    assert second == r"C:\out\sq020_sh050.png"
