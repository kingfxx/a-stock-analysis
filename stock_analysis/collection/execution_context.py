"""Request-scoped parent metadata and cancellation for composed collectors."""
from contextvars import ContextVar
from contextlib import contextmanager

current_execution = ContextVar('collection_execution', default=None)


@contextmanager
def execution_context(**values):
    token = current_execution.set(values)
    try:
        yield
    finally:
        current_execution.reset(token)
