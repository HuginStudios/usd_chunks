# ADR-0003: Persist chunk pin state as a custom USD attribute, not ChunkFlags.State

**Status:** Accepted
**Date:** 2026-07-21

## Context

The Params-tab redesign ("Pinned Chunk Context") needs a per-chunk boolean — whether a chunk shows in its context's Params page — that survives save/reload of the USD scene.

`ChunkFlags.State` (COLLAPSED, DISABLED, SELECTED) looks like the obvious place to add a `PINNED` bit: it already has `set_state`/`has_state`/`unset_state` and a notice channel (`attrname == "state"` in `chunk_core.py`). But it is runtime-only — no code path writes `self.state` to the prim or reads it back on load. Reusing it would make pin state silently reset on every reload, contradicting the migration requirement (old scenes must look unchanged on reopen).

`chunk_qt_nodegraph.py` already has a precedent for a persisted, chunk-scoped UI attribute: `chunk:activeView` (`Int`, custom) written directly onto the context's prim.

## Decision

Store pin state as its own custom USD attribute, `chunk:pinned` (`Bool`, custom), written on the chunk's own prim and read back during `CHUNK_LOADED` handling — the same pattern `view:slot` and `chunk:activeView` already use. Absence of the attribute is treated as pinned=true (see CONTEXT.md migration decision).

## When to revisit

If more per-chunk UI state ends up needing persistence (this is the second one after `chunk:activeView`), consider consolidating into a single namespaced dict/customData block instead of one bespoke attribute per flag.
