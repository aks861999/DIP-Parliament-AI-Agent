import logging
import os

logger = logging.getLogger(__name__)

_ENABLED = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
_client = None
_langfuse_observe = None

if _ENABLED:
    try:
        from langfuse import get_client
        from langfuse import observe as _langfuse_observe

        _client = get_client()
        logger.info("Langfuse tracing enabled (%s)", os.getenv("LANGFUSE_HOST"))
    except Exception as e:
        logger.warning("Langfuse import/init failed (%s) — tracing disabled", e)
        _ENABLED = False


def observe():
    if not _ENABLED:
        def _noop_decorator(fn):
            return fn
        return _noop_decorator
    try:
        return _langfuse_observe()
    except TypeError:
        return _langfuse_observe


def update_current_trace(**kwargs) -> None:
    if not _ENABLED or _client is None:
        return
    try:
        _client.update_current_trace(**kwargs)
    except Exception as e:
        logger.debug("tracing.update_current_trace failed: %s", e)


def score_current_trace(name: str, value: float, comment: str | None = None) -> None:
    if not _ENABLED or _client is None:
        return
    try:
        _client.score_current_trace(name=name, value=value, comment=comment)
    except Exception as e:
        logger.debug("tracing.score_current_trace failed: %s", e)


def event(name: str, metadata: dict | None = None) -> None:
    if not _ENABLED or _client is None:
        return
    try:
        _client.event(name=name, metadata=metadata or {})
    except Exception as e:
        logger.debug("tracing.event failed: %s", e)


def flush() -> None:
    if not _ENABLED or _client is None:
        return
    try:
        _client.flush()
    except Exception as e:
        logger.debug("tracing.flush failed: %s", e)
