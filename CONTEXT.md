# usd_chunks — CONTEXT.md

Single-context repo. See `docs/adr/` for point-in-time decisions; this file is the living glossary and current architecture summary for the areas it covers.

## Glossary

- **Chunk** — a Python module returning params/groups/callbacks; the core unit of the plugin system. See `README.md` and root `CLAUDE.md` for the full system overview.
- **Context** — a `ChunkBase` flagged `ChunkFlags.GroupFlags.CONTEXT`. Backs one **tab** in the tab bar (`ChunkTabWidget`) — either a top-level "Tab N" or a dived-into chunkgroup/subnet. A regular (non-context) chunk belongs to exactly one context via `get_context()`.
- **Tab** — UI term for a context's slot in the tab bar. Each tab has its own `ChunkTabContextStack` holding a Params page (page 0) and a Graph page (page 1) of the *same* context.
- **View mode** — the Params/Graph toggle (`_view_bar`, top of `ChunkWidgetDocker`). **Global**, not per-tab: flipping it switches every tab's stack to the same page index at once (`ChunkTabWidget.set_view_mode`).
- **Pinned** — a per-chunk boolean (new). A chunk pinned into its context shows in that context's Params page. Unpinned chunks exist and cook normally but are only visible/editable via the Graph page. Since a chunk belongs to exactly one context, pin state is automatically scoped correctly per tab and per chunkgroup/subnet dive-in tab — no separate per-tab list needed.
- **Shared chunk menu** — `ViewSlotState.show_chunk_menu()` in `plugins/chunk_qt_widgets.py`. The single right-click menu builder used by *both* `ChunkGraphView.contextMenuEvent` (graph node) and `ChunkWidget.contextMenuEvent` (Params-tab chunk widget). Anything added here appears in both surfaces for free.
- **Filter bar** — per-tab UI at the top of a context's Params page: a pin-filter toggle plus a fuzzy search box with a Both/Name/Type scope combobox. A second, orthogonal filter layer over **pinned** — composes via AND, doesn't change pin semantics. See "Params Filter Bar" below. `ParamsFilterBar` in `plugins/chunk_qt_widgets.py`.

## Pinned Chunk Context (shipped)

**Goal:** the Params tab stops showing every chunk in a context and instead shows only the chunks explicitly pinned to it. The Graph page remains the unfiltered, full view of a context's chunks — it stays the source of truth.

### Decisions

- **Persistence** — pin state is a new custom USD attribute, `chunk:pinned` (Bool, custom), following the existing `chunk:activeView` attribute convention (see `plugins/chunk_qt_nodegraph.py:2136`). It cannot reuse `ChunkFlags.State` — those bits (COLLAPSED/DISABLED/SELECTED) are runtime-only today and never round-trip through USD. `chunk:pinned` is read back on scene load the same way `view:slot` is.
- **Migration** — attribute absent = pinned. Scenes saved before this feature ships look unchanged on reopen: every existing chunk still shows in its tab's Params page. Only chunks created after the upgrade can end up unpinned.
- **Startup default** — `ChunkWidgetDocker._view_bar` now defaults to index 1 (Graph) instead of index 0 (Params) on a fresh usdview launch. The toggle itself stays global (see View mode above) — this was a deliberate scope cut, not an oversight.
- **Auto-pin on create** — reuses the existing `chunk_usd._params_tab_create` flag, which today is set only by `ChunkTabWidget.contextMenuEvent` (the Params tab's own "+"/create-chunk menu) and used for view-follows-tail ordering. Chunks created through it are auto-pinned; chunks created through the Graph page's create menu (`ChunkGraphView.contextMenuEvent`) are not. Paste and duplicate follow the *active view mode* at the time of the operation (Params active → pinned, Graph active → unpinned), not the source chunk's own pin state.
- **Menu + affordance** — "Pin to Params" / "Unpin from Params" added to `ViewSlotState.show_chunk_menu`, placed right after "Rename…" (an identity action, grouped with Rename/Inspect Layer rather than the Set View block below the separator). Pinned nodes get a small 📌 glyph on the graph node header, in the same header-glyph slot pattern already used for 🔒 (locked) and ▶ (group).
- **Scope cuts for v1** — no multi-select bulk pin/unpin (menu stays single-node-targeted, matching Rename/Inspect Layer/Set View today); no "pin all chunks in this tab" escape hatch. Both explicitly deferred rather than designed against, revisit if requested.

### Resolved during shipping

- Chunkgroup/subnet "dive in" needs no special-casing — confirmed. Since a chunk belongs to exactly one context and a dive-in tab is built via the identical `ChunkTabContextStack`/`ViewSlotState`/`ChunkWidget(is_tab_context=True)` machinery as any other tab, pin scoping just works.
- Unpinning the only chunk in a context's Params page needs no special handling — an empty Params page is a valid, expected state.
- **Real bug found in live usdview testing, fixed in issue #04**: the Params-page widget and `ChunkStageSync` both react to the same `PARAM_ADDED` notice for a newly-created chunk, and Tf.Notice delivery order between two independent listeners on one notice isn't guaranteed — in practice the widget's listener could run *before* `ChunkStageSync`'s had even created the prim, let alone authored `chunk:pinned`, so it fell back to the legacy pinned=true default and never rechecked. Fixed by making `_pin_new_chunks` a tri-state (`True`/`False`/`None`-idle) instead of a plain bool, so `is_chunk_pinned()` can tell "actively deciding this chunk's state right now" apart from "genuinely legacy, no creation flow at all" — every real creation surface (Params menu, Graph menu, paste) now explicitly sets it. Also: view chunks (`is_view_chunk`) unconditionally default to unpinned regardless of this — they're graph-wiring utility nodes, never meant for Params curation.

## Params Filter Bar: Pin Toggle + Search (shipped)

**Goal:** a second, orthogonal filter layer over the Params page, on top of (not replacing) pinning. Lets a tab narrow further than "pinned" — e.g. "only the pinned chunks whose name/type matches 'transform'" — or search across every chunk regardless of pin state by turning the pin filter off for that tab.

### Decisions

- **Searchable fields** — chunk `name` and chunk type (the `module`/`info:chunkType` string already shown as the node's subtitle in the graph — see `_CHUNK_TYPE_ATTR` in `plugins/chunk_usd.py`). Not category (category is a menu-organization concept, not something users think of a chunk "as").
- **Match style** — fuzzy, typo-tolerant matching via Python's stdlib `difflib` (`SequenceMatcher`/`get_close_matches`) — no new dependency. A `QComboBox` with three items (Both / Name / Type, "both" the default) sets the scope — a combobox rather than two independent toggle chips (the original design) since it structurally guarantees exactly one scope is ever selected, no "both off" edge case to guard against.
- **Combination with pin filter** — AND. A chunk shows if: (pin-filter off, or the chunk is pinned) AND (search empty, or name/type fuzzy-matches per the selected scope). Turning the pin filter off lets search run over every chunk in the tab regardless of pin state.
- **Graph page** — completely unaffected. Search is Params-only, the same asymmetry pinning already established (Graph stays the full, unfiltered source of truth).
- **Persistence** — per-tab, on the context's own prim: `filter:pinEnabled` (Bool, custom) and `filter:searchQuery` (String/Token, custom) — same custom-attribute pattern as `chunk:pinned`/`chunk:activeView`. Absent on either → migration default: pin-filter enabled, search empty (identical in spirit to `chunk:pinned`'s absent-means-pinned rule — old scenes look unchanged on reopen).
- **UI placement** — a filter bar (pin toggle + search box + scope combobox) at the top of each tab's own Params page (inside the `is_tab_context=True` `ChunkWidget`, above its scrollable chunk list) — not the global `_view_bar`, since filter state is per-tab, not global like the Params/Graph view mode is. Ordered: Prim Path field → `_lofted_container` (a context's own top-level params — e.g. `scripts/chunkgroup.py`'s "doc" Description `TextEdit` — already rendered here via `ChunkWidget.add_widget`'s pre-existing lofted-params branch, nothing new) → filter bar → a solid separator bar → the chunk list. A plain tab has no own params to loft, so `_lofted_container` stays empty/invisible and its filter bar effectively follows the Prim Path field directly.
- **No tab-level indicator** — no badge/dot on the tab label when a filter is active. The filter bar itself (search box has text, pin toggle visibly off) is the only cue, matching the original pin feature's minimalism (no bulk actions, no pin-all escape hatch — this feature effectively **is** that escape hatch, per-tab).

### Resolved during shipping

- **Real bug found while implementing #05**: a CONTEXT chunk's own prim path must be read from its own `.prim_path` attribute directly (`_context_own_prim_path()`), never via `get_chunk_prim_path(context)` — that function is for a *child* chunk resolving its path under a parent, and double-appends the context's own name when called on the context itself (`ChunkBase.get_context()` returns `self` for a chunk that already has the CONTEXT flag). Chunkgroups happen to dodge this because their branch of `get_chunk_prim_path` short-circuits on their own `.prim_path` before the buggy resolution path runs — which is why existing code (`chunk:activeView`) only ever called it on chunkgroup contexts, never plain tabs.
- **Also found**: a plain tab context's own prim is never authored by `_define_prim` at all (`SceneSender.add_chunk` passes `send_added=False` for a CONTEXT chunk with no module) — it only starts existing as an implicit ancestor once a child chunk is added under it. `set_pin_filter_enabled`/`set_search_query`/`set_search_scope` all define the prim on demand rather than silently no-op-ing on a still-empty tab.

## File map (for this area)

```
plugins/
  chunk_qt.py              ← ChunkTabWidget, ChunkTabContextStack, ChunkWidgetDocker (_view_bar), Params-tab create menu (_params_tab_create)
  chunk_qt_widgets.py       ← ViewSlotState.show_chunk_menu (shared menu), ChunkWidget.contextMenuEvent, ChunkWidget (filter bar lives here, top of the is_tab_context widget)
  chunk_qt_nodegraph.py     ← ChunkGraphView, ChunkNodeItem (header glyphs, node paint), graph create menu
  chunk_usd.py              ← _pin_new_chunks tri-state flag, chunk:pinned / chunk:activeView attr precedent, is_chunk_pinned/set_chunk_pinned
usd_chunks/
  chunk_flags.py            ← ChunkFlags.State (runtime-only, NOT the persistence mechanism for pinned or filter state)
```
