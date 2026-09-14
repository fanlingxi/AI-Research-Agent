"""Bound first-party retrieval IO and retry only transient transport failures."""

from importlib import import_module

IO_TIMEOUT_SECONDS = 30
MAX_EXTERNAL_ATTEMPTS = 2
MAX_CONTEXT_PREPARATIONS = 2
MAX_REPORT_SCOPE_ATTEMPTS = 2


def is_transient_error(error: Exception) -> bool:
    # A missing optional SDK must still degrade, not fail while classifying the
    # original ImportError. Import only exception types, never initialize clients.
    try:
        qdrant_errors = import_module("qdrant_client.http.exceptions")
    except ImportError:
        pass
    else:
        if isinstance(error, qdrant_errors.ResponseHandlingException):
            error = error.source
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    for module, names in (
        ("httpx", ("TimeoutException", "NetworkError", "RemoteProtocolError")),
        ("openai", ("APIConnectionError",)),
        ("neo4j.exceptions", ("ServiceUnavailable", "SessionExpired", "TransientError")),
    ):
        try:
            exceptions = import_module(module)
        except ImportError:
            continue
        if isinstance(error, tuple(getattr(exceptions, name) for name in names)):
            return True
    return False
