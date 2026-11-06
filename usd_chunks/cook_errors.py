"""How a cook can end other than by finishing: kept below every module that runs one."""

from __future__ import annotations


class CookCancelled(Exception):
    """Raised out of a module's execute() when the person cancelled its cook.

    It fails the cook like any error -- the chunk goes FAILED and nothing below it cooks
    (chunk_cook) -- but it is not tried again, and the pass around it does not raise it on:
    nothing went wrong.
    """


class CookBlocked(Exception):
    """Raised when a chunk names a prim it cannot see: one made below it or on another branch.

    A chunk sees only what is wired above it, so a Transform wired above the Cube it names
    has nothing to move. chunk_cook catches it: the chunk goes FAILED, nothing below it
    cooks until it is rewired, and the message on the node says what to wire. It is never
    raised on, so a script or the scene being opened goes on.
    """
