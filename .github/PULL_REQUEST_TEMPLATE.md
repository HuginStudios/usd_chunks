# What and why

<!-- What changes, and the problem it solves. Link an issue if there is one. -->

# How it was verified

<!--
What you actually ran, not just "tests pass". For a bug fix, the most convincing thing is a
test you watched fail before the change and pass after it.
-->

# Checklist

- [ ] `USD_CHUNK_DIRS=scripts pytest` passes
- [ ] `ruff check .` is clean
- [ ] A test covers the change, where it is testable
- [ ] README updated, if user-visible behaviour changed
- [ ] Does not change the chunk module contract (`main()` / `config()` / `execute()`) or
      the callback signature — or, if it does, that is called out above, because it breaks
      every existing chunk
