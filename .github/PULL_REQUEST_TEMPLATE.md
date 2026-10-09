## What and why

<!-- What does this change, and why is it needed? Link the issue if there is one. -->

## How it was verified

<!-- What you actually ran. "Tests pass" is less useful than the command and what you
checked by hand (usdview, a DCC, the CLI). -->

## Checklist

- [ ] One focused change (a packaging fix and a feature go in separate PRs)
- [ ] A bug fix comes with a test that fails before the change and passes after
- [ ] `ruff check .` is clean
- [ ] README/docs updated if a user would notice the change
- [ ] No change to the chunk module contract (`main()` / `config()` / `execute(node)`) or
      the callback signatures -- or, if there is, it is called out above as breaking

By opening this PR you agree your contribution is licensed under the Apache License 2.0.
