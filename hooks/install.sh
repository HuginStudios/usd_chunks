#!/bin/sh
# Turn these hooks on for this clone:
#
#     sh hooks/install.sh                 # fast pre-commit checks only
#     sh hooks/install.sh --with-prepush  # ...and the full suite before every push
#
# It only sets local git config -- nothing is copied, so the hooks stay reviewable in git
# and update when you pull. Undo with:
#
#     git config --unset core.hooksPath
#
# Each clone has to run this once. A repository cannot install hooks on your behalf:
# core.hooksPath is local config, deliberately, since a hook is code that runs on your
# machine whenever you commit.
#
# Any hook already in .git/hooks keeps running: core.hooksPath replaces that directory
# rather than adding to it, so each hook here chains to its .git/hooks counterpart first
# (see hooks/_common.sh). vexp installs pre-commit/post-merge/post-checkout hooks there.
#
# Requires ruff and pytest, both of which come with:  pip install -e ".[dev,ui]"
# Nothing else -- these are plain git hooks, not the `pre-commit` framework.
set -e
root="$(git rev-parse --show-toplevel)"
git config core.hooksPath hooks

# The full-suite pre-push hook is off unless asked for: seven minutes on every push is how
# you get someone to uninstall the whole thing, fast pre-commit checks included.
if [ "$1" = "--with-prepush" ]; then
    git config usdchunks.prepush true
else
    git config usdchunks.prepush false
fi

printf 'hooks enabled for %s\n' "$root"
printf '  pre-commit: ruff on the staged Python, ~3s of core tests, and any test file you staged\n'
if [ "$(git config --bool usdchunks.prepush)" = "true" ]; then
    printf '  pre-push  : ruff + the full suite (~7min), against hooks/known-failures.txt\n'
    printf '              turn off with: git config usdchunks.prepush false\n'
else
    printf '  pre-push  : off. Turn the full-suite check on with:\n'
    printf '              git config usdchunks.prepush true\n'
fi
printf 'Skip any hook once with --no-verify.\n'
