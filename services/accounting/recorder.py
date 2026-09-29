"""Optional recording at the actual provider boundary, independent of routing."""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from uuid import uuid4

from common import site

from .store import RequestStore, now, sanitize_context

_log = logging.getLogger(__name__)
_warning_lock = threading.Lock()
_last_warning = float("-inf")


def database_path():
    return Path(
        site.get(
            "ACCOUNTING_DB", str(Path(site.SITE_DIR) / "accounting/requests.sqlite")
        )
    )


@lru_cache(maxsize=4)
def _store(path):
    # Automated recording intentionally retains no request or response content.
    return RequestStore(path, timeout=0.25)


def _warn():
    global _last_warning
    with _warning_lock:
        current = time.monotonic()
        if current - _last_warning >= 60:
            _last_warning = current
            # Exceptions can contain provider credentials or user text. Never log them here.
            _log.warning(
                "Request accounting failed; model requests continue. Check accounting database permissions and disk space; totals may be incomplete."
            )


@dataclass
class Outcome:
    result: object = None


@contextmanager
def record_attempt(model, context=None):
    """One row per dispatched provider attempt; recording never retries a request.

    Set outcome.result immediately after parsing the provider JSON, before
    validating its content, so billed invalid responses retain their usage.
    """
    outcome = Outcome()
    store = None
    request_id = uuid4().hex
    started = time.monotonic()
    if site.get("ACCOUNTING_ENABLED", "0") == "1":
        try:
            store = _store(str(database_path()))
            metadata = sanitize_context({"hdm_context": context})
            store.create(
                dict(request_id=request_id, timestamp=now(), model=model, **metadata),
                {},
            )
            store.dispatch(request_id)
        except Exception:
            store = None
            _warn()
    status, http_status = "interrupted", None
    try:
        yield outcome
    except Exception as error:
        if isinstance(error, HTTPError):
            status, http_status = "http_error", error.code
        elif isinstance(error, (URLError, OSError)):
            status = "connection_error"
        else:
            status = "invalid_response"
        raise
    else:
        status = "success"
    finally:
        if store is not None:
            try:
                store.finish(
                    request_id,
                    status,
                    result=outcome.result,
                    seconds=time.monotonic() - started,
                    http_status=http_status,
                )
            except Exception:
                _warn()
