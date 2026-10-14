"""Context-local cooperative deadline for the bounded knowledge workflow."""
import math
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

from model.factory import ModelError

_current = ContextVar("knowledge_deadline", default=None)


class Deadline:
    def __init__(self, seconds=45.0, *, clock=monotonic):
        if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 < seconds <= 120:
            raise ValueError("Invalid knowledge time budget")
        self.seconds, self.clock = seconds, clock
        self.started = clock()

    def elapsed_ms(self):
        return round(max(0, self.clock() - self.started) * 1000, 3)

    def remaining(self):
        value = self.seconds - (self.clock() - self.started)
        if value <= 0:
            raise ModelError("KNOWLEDGE_TIME_BUDGET", "本次资料问答已达到时间上限，未继续请求或展示迟到结果；可以重新提问。", 504)
        return value


@contextmanager
def use_deadline(deadline):
    token = _current.set(deadline)
    try:
        deadline.remaining()
        yield deadline
    finally:
        _current.reset(token)


def check_deadline():
    deadline = _current.get()
    if deadline is not None:
        deadline.remaining()


def bounded_timeout(default):
    deadline = _current.get()
    return min(default, deadline.remaining()) if deadline is not None else default
