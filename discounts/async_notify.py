"""Fire-and-forget work after the DB transaction commits.

Avoids blocking Gunicorn workers on FCM / SMTP / WhatsApp. Uses a daemon
thread — fine for Render free tier without Celery/Redis. Failures are logged.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from django.db import close_old_connections, transaction

logger = logging.getLogger(__name__)


def run_after_commit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Schedule ``fn(*args, **kwargs)`` after the current transaction commits.

    If there is no active transaction, runs in a background thread immediately.
    """

    def _spawn() -> None:
        thread = threading.Thread(
            target=_safe_call,
            args=(fn, args, kwargs),
            daemon=True,
            name=f"async-{getattr(fn, '__name__', 'task')}",
        )
        thread.start()

    try:
        transaction.on_commit(_spawn)
    except Exception:
        # Outside atomic block or connection issue — still don't block the request.
        logger.exception("on_commit failed; spawning background task immediately")
        _spawn()


def _safe_call(fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
    close_old_connections()
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("Background notify task failed: %s", getattr(fn, "__name__", fn))
    finally:
        close_old_connections()
