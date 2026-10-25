"""Progress delivery without coupling services to a user interface."""
from contextlib import contextmanager
from contextvars import ContextVar


_sink = ContextVar("review_event_sink", default=None)


def emit(kind, **fields):
    sink = _sink.get()
    if sink is not None:
        sink(kind, **fields)


@contextmanager
def event_sink(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)
