Status: needs-triage

# Panel builder tests

## What to build

Add `test_panel_builder.py` covering `build_group_promo_panel` and the failure path of `build_chunk_panel`.

Both functions take a `QFormLayout` and a callable — no full widget or executor is required, making these cleanly headless tests.

Scenarios to cover:

1. **`build_group_promo_panel` with promotions** — given a GroupNode with one promoted param, the form receives exactly one labeled row with a `QLineEdit` pre-filled with the default value.
2. **`build_group_promo_panel` with no promotions** — form receives one row containing the placeholder `QLabel` (no params promoted message).
3. **`build_chunk_panel` when `plugins.chunk_qt` is unavailable** — function should add an error `QLabel` to the form rather than raising.
4. **`build_chunk_panel` with a real chunk** — form receives a `ChunkWidget` child row (integration path; may be skipped if `plugins.chunk_qt` is not importable in the test environment).

## Acceptance criteria

- [ ] `test_panel_builder.py` exists under `scripts/node_graph/tests/`
- [ ] Scenarios 1–3 pass in the existing headless test environment
- [ ] Scenario 4 is present and either passes or skips gracefully with an informative `pytest.skip` message
- [ ] All existing tests still pass

## Blocked by

None — can start immediately.
