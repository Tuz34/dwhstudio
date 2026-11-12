"""One audited catch-all for a best-effort step."""

from collections.abc import Callable
from typing import Any


def contained(fn: Callable[..., Any], *args: Any) -> tuple[Any, str | None]:
    """Run a best-effort step. Returns (result, None) or (None, exception class name).

    Never lets an Exception out; reads nothing from the exception but its class name.
    """
    try:
        return fn(*args), None
    except Exception as exc:
        return None, type(exc).__name__
