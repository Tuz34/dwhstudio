from __future__ import annotations

import json


def safe_error_payload(action: str, exc: BaseException) -> dict[str, str]:
    return {
        "error": f"{action} failed.",
        "error_type": type(exc).__name__,
    }


def safe_error_json(action: str, exc: BaseException) -> str:
    return json.dumps(safe_error_payload(action, exc), ensure_ascii=False)
