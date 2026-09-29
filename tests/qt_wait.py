"""Wait for deferred Qt work in a test."""
from __future__ import annotations

import time


def pump_until(qapp, done, seconds: float = 10.0) -> bool:
    """Process Qt events until done() is true, for at most `seconds`.

    Node bodies, ports and auto-created views are built on timers. A fixed short pump ran
    out on a busy machine; this returns as soon as the thing is there, so the limit costs
    nothing when a test passes.

    Args:
        qapp: The QApplication.
        done: Returns True once the awaited state is reached.
        seconds: The most to wait.

    Returns:
        Whether done() became true.
    """
    end = time.monotonic() + seconds
    while not done():
        if time.monotonic() > end:
            return False
        qapp.processEvents()
    return True
