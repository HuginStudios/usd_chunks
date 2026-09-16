"""A decorated callback is called with the arguments it declares.

Whether a callback gets (owner, notice, sender) or (notice, sender) is read from its
signature. A decorator's wrapper is usually (*args, **kwargs), which reads as taking no
arguments, so an (owner, notice, sender) callback under a decorator was called with two
and failed -- scripts/examples/example.py does exactly this with its nice_print.
"""
from __future__ import annotations

from functools import wraps

from usd_chunks import chunk
from usd_chunks.chunk_notice import NoticeConnection


def _logged(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


def _unwrapped_decorator(func):
    """A decorator without functools.wraps: nothing records what it wraps."""
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


@_logged
def _with_owner(owner, notice, sender):
    pass


@_logged
def _without_owner(notice, sender):
    pass


class _Holder:
    @_logged
    def method(self, owner, notice, sender):
        pass


def test_a_decorated_owner_callback_is_given_the_owner():
    assert NoticeConnection._wants_owner(_with_owner)


def test_a_decorated_two_argument_callback_is_not():
    assert not NoticeConnection._wants_owner(_without_owner)


def test_a_decorated_method_is_read_through_its_decorator():
    assert NoticeConnection._wants_owner(_Holder().method)


def test_without_wraps_the_wrapper_decides():
    assert not NoticeConnection._wants_owner(_unwrapped_decorator(lambda owner, notice, sender: None))


def test_a_decorated_callback_runs_on_a_change(qapp):
    seen = []

    @_logged
    def on_change(owner, notice, sender):
        seen.append(owner)

    c = chunk.Chunk("decorated")
    slider = c.add_param(chunk.Slider("s"))
    slider.add_callback(on_change)
    slider.value = 0.5

    assert seen and seen[-1] is slider
