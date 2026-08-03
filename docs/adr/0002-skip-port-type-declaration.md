# ADR-0002: Keep port-type introspection, skip explicit PORTS_IN/PORTS_OUT

**Status:** Accepted  
**Date:** 2026-05-06

## Context

`_get_node_port_types()` in `nodes/_base.py` uses `inspect.getsource` + regex to infer which port types each node declares, building a cache. This is fragile (breaks on line wraps, falls back silently to `{"any","trigger"}`).

The proposed fix was `PORTS_IN: ClassVar[set]` and `PORTS_OUT: ClassVar[set]` on each node class.

## Decision

Skipped. The introspection is not causing observable bugs; the fallback `{"any","trigger"}` is safe for type enforcement. Adding explicit declarations to ~60 node classes is high-noise mechanical work with no current user-visible benefit.

## When to revisit

Reopen if the regex fallback causes a visible bug, or if a new feature (e.g. strict port-type enforcement UI) needs reliable type information at import time.
