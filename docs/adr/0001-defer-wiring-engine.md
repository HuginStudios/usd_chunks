# ADR-0001: Defer WiringEngine extraction

**Status:** Deferred  
**Date:** 2026-05-06

## Context

Port connect/disconnect in `graph_widget.py` handles three concerns inline: toggling trigger-port visibility (UI), resolving chunk params and calling `wire_from()` (chunk binding), and calling `mark_dirty()` (execution). `_resolve_chunk_param_for_port` is called from ~8 call sites.

A `WiringEngine` module was proposed to own `on_connected(src, dst)` and `on_disconnected(src, dst)` with `ChunkAdapter.resolve_param_for_port(port_name)` as a collaborator.

## Decision

Deferred. The wiring logic touches port-connect flow from NodeGraphQt, chunk-param resolution across nested groups/vectors, trigger-port visibility, and execution scheduling. Extracting it cleanly requires a careful surgical pass that is high risk relative to current codebase activity.

## When to revisit

Reopen this when a new wiring feature (e.g. type-safe port connections, multi-output fan-out) naturally requires refactoring port-connect handling.
