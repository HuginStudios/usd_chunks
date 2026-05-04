# Lift regular node side panel quality

Status: ready-for-agent

Depends on: issue #18 (structural refactor must land first)

## What
Non-chunk nodes currently show auto-generated bare `QLineEdit` inputs in the side panel.
Apply the same dark styling used for chunk widgets:

- Background: `#1e1e1e`
- Input fields: `#2a2a2a` border `#444`, text `#ddd`
- Labels: `#aaa`, 11px
- Add a `Type:` label showing the node's class name in muted text below the node name

Extract the CSS into a single `PANEL_STYLE` constant in `graph_widget.py` (or a new
`graph_theme.py`) shared by both chunk and non-chunk panel sections. Currently the dark
theme CSS is duplicated between `graph_widget.py` and `plugins/chunk_qt.py`.

## Where
- `scripts/node_graph/graph_widget.py` — side panel styling, CSS constant

## Decisions
- `PANEL_STYLE` constant replaces duplicated CSS; `plugins/chunk_qt.py` imports it or keeps
  its own (do not force a dependency from plugins → node_graph).

## Acceptance
- Non-chunk node side panel uses the same background color and input styling as chunk nodes.
