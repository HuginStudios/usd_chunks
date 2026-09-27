# Shared by the hooks in this directory. Not a hook itself.

# core.hooksPath REPLACES .git/hooks rather than adding to it, so anything already
# installed there stops running the moment these hooks are enabled -- vexp keeps its index
# in sync from pre-commit/post-merge/post-checkout, and silently losing that would be a
# nasty surprise. Run the local hook first and take its exit code seriously.
run_local_hook() {
    name="$1"
    shift
    local_hook="$(git rev-parse --git-dir)/hooks/$name"
    if [ -x "$local_hook" ]; then
        "$local_hook" "$@" || exit $?
    fi
}

# CI runs the suite with USD_CHUNK_DIRS=chunks, i.e. the chunks of the checkout under
# test. Locally the variable is usually an ABSOLUTE path to whichever worktree was set up
# first, so a hook running in any other worktree would validate that other checkout's
# chunks and pass or fail for reasons that are not in the commit. Point it at this
# checkout, always.
export USD_CHUNK_DIRS="$(git rev-parse --show-toplevel)/chunks"

say() { printf '%s\n' "$*" >&2; }
