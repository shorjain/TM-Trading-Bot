"""Keep Windows from sleeping while the bot runs. No-op on macOS/Linux (use `caffeinate -i`)."""

from __future__ import annotations

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set(flags: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


class KeepAwake:
    def __enter__(self):
        self.active = _set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return self

    def __exit__(self, *exc):
        if getattr(self, "active", False):
            _set(ES_CONTINUOUS)  # back to normal sleep behaviour
        return False
