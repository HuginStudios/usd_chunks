"""How a cook can end other than by finishing: kept below every module that runs one."""

from __future__ import annotations


class CookCancelled(Exception):
    """Raised out of a module's execute() when the person cancelled its cook.

    It fails the cook like any error -- the chunk goes FAILED and nothing below it cooks
    (chunk_cook) -- but it is not tried again, and the pass around it does not raise it on:
    nothing went wrong.
    """
