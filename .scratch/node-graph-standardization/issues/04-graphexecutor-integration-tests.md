Status: needs-triage

# GraphExecutor integration tests — error propagation and group execution

## What to build

Extend `test_executor.py` with tests that cover the three behavioral invariants documented in CONTEXT.md but not yet asserted:

1. **Error propagation** — when `_exec_node` returns `False` (error), all nodes downstream in the same `run_full` pass are set to "skipped" and their `_output_cache` is cleared.
2. **`run_from` subtree scoping** — `run_from(node)` re-executes the upstream ancestors of `node` and then `node` and its descendants; it does NOT re-execute nodes outside that subgraph.
3. **Group execution (promoted value flow)** — `_exec_group` applies the `default_value` from a promotion dict to the inner node's property before executing, so the inner chunk receives the promoted value. (The fixture and helper `_add_promotion` from `test_group_nodes.py` can be reused.)

Use the existing `_StubWidget`, `node_graph`, and `stage` fixtures; no new fixtures needed.

## Acceptance criteria

- [ ] Test: errored node → downstream nodes have state "skipped" and empty `_output_cache`
- [ ] Test: `run_from(B)` does not re-execute node A when A is not upstream of B
- [ ] Test: promoted `default_value` is visible in inner node's property after `_exec_group`
- [ ] All tests pass
- [ ] Progress is printed per scenario in this format:

```
── Adding GraphExecutor integration tests ────────
  [1/3] Error propagation test        ✓
  [2/3] run_from subtree scoping test ✓
  [3/3] Group promoted value test     ✓
── Tests ─────────────────────────────────────────
  40 passed
```

## Blocked by

None — can start immediately.
