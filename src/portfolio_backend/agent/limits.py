"""Wall-clock cancellation in Celery prefork workers; never abandon a writing thread."""

import signal
import threading
from contextlib import contextmanager

from portfolio_backend.errors import DomainError


@contextmanager
def deadline(seconds):
    if threading.current_thread() is not threading.main_thread():
        # Cooperative mode: Celery prefork deadlines use SIGALRM, which only
        # targets the main thread. Scheduled report summaries run inside bounded
        # worker threads (plan A); each HTTP/DB call there is already bounded by
        # its own timeout and the ReAct loop re-checks the wall clock between
        # rounds. Interactive answers stay on the main thread and keep SIGALRM.
        yield
        return

    def expired(signum, frame):
        raise DomainError("tool_timeout", "本次操作超时", 503)

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, max(0.001, seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, previous)
