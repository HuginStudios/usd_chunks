#!/bin/sh
# Turn these hooks on for this clone:
#
#     sh hooks/install.sh
#
# It only sets core.hooksPath, which is per-clone config -- nothing is copied, so the
# hooks stay reviewable in git and update with a pull. Turn them off again with:
#
#     git config --unset core.hooksPath
#
# Any hook already in .git/hooks keeps running: core.hooksPath replaces that directory
# rather than adding to it, so each hook here chains to its .git/hooks counterpart first
# (see hooks/_common.sh). vexp installs pre-commit/post-merge/post-checkout hooks there.
set -e
root="$(git rev-parse --show-toplevel)"
git config core.hooksPath hooks
printf 'hooks enabled for %s\n' "$root"
printf '  pre-commit: ruff on staged files + ~4s of core tests + any test file you staged\n'
printf '  pre-push  : ruff check . + the full suite, against hooks/known-failures.txt\n'
printf 'Skip either with --no-verify.\n'
