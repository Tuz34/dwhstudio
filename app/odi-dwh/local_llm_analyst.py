"""On-demand Local LLM analyst helper with VRAM unload guard.

This helper is intentionally narrow. It sends only redaction-checked text to a
local OpenAI-compatible endpoint, can load an LM Studio model on demand, and
unloads the model only when this helper loaded it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from config import LOCAL_OPENAI_API_KEY, LOCAL_OPENAI_BASE_URL, LOCAL_OPENAI_MODEL

DEFAULT_MODEL = LOCAL_OPENAI_MODEL
DEFAULT_BASE_URL = LOCAL_OPENAI_BASE_URL
DEFAULT_TASK = "Summarize the handoff, risks, blockers, and next actions in short Turkish bullets."

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("env_assignment", re.compile(r"(?im)^\s*[A-Z0-9_]*(PASSWORD|SECRET|TOKEN|API_KEY|KEY)\s*=\s*\S+")),
    ("assignment_secret", re.compile(r"(?i)\b(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("url_credential", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@")),
    ("oracle_easy_connect_credential", re.compile(r"(?i)\b[A-Z0-9_]+/[^@\s]+@[A-Z0-9_.:-]+")),
)


class LocalAnalystError(RuntimeError):
    """Raised when local analyst execution fails before a model response."""


def scan_for_blocked_content(text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append({"type": name, "action": "blocked"})
    return findings


def _run_lms(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["lms", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def loaded_models() -> list[dict[str, Any]]:
    proc = _run_lms(["ps", "--json"], timeout=10)
    if proc.returncode != 0:
        raise LocalAnalystError(f"lms ps failed: {proc.stderr.strip() or proc.stdout.strip()}")
    if not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LocalAnalystError(f"lms ps returned invalid JSON: {exc}") from exc
    return data if isinstance(data, list) else []


def is_model_loaded(model: str) -> bool:
    for item in loaded_models():
        if model in {
            str(item.get("modelKey") or ""),
            str(item.get("identifier") or ""),
            str(item.get("path") or ""),
        }:
            return True
    return False


def load_model(model: str) -> None:
    proc = _run_lms(["load", model], timeout=180)
    if proc.returncode != 0:
        raise LocalAnalystError(f"lms load failed: {proc.stderr.strip() or proc.stdout.strip()}")


def unload_model(model: str) -> None:
    proc = _run_lms(["unload", model], timeout=60)
    if proc.returncode != 0:
        raise LocalAnalystError(f"lms unload failed: {proc.stderr.strip() or proc.stdout.strip()}")


def _chat_payload(model: str, task: str, text: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a local-only analyst for Oracle, ODI, DWH and agent handoff work. "
                    "Do not request or reveal secrets. Produce concise Turkish bullets."
                ),
            },
            {
                "role": "user",
                "content": f"Task:\n{task}\n\nRedacted input:\n{text}",
            },
        ],
        "temperature": 0.1,
        "max_tokens": 900,
    }


def analyze_text(
    text: str,
    *,
    task: str = DEFAULT_TASK,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = "",
    auto_load: bool = True,
    unload_after: bool = True,
    http_post: Callable[..., Any] = requests.post,
) -> dict[str, Any]:
    findings = scan_for_blocked_content(text)
    if findings:
        return {
            "status": "blocked_input",
            "secret_printed": False,
            "findings": findings,
            "message": "Input contains secret-like content; local model call was not attempted.",
        }

    loaded_before = is_model_loaded(model)
    loaded_by_helper = False
    started = time.perf_counter()
    try:
        if not loaded_before:
            if not auto_load:
                return {
                    "status": "blocked_model_not_loaded",
                    "secret_printed": False,
                    "loaded_before": False,
                    "message": "Model is not loaded and auto_load is disabled.",
                }
            load_model(model)
            loaded_by_helper = True

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = http_post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=_chat_payload(model, task, text),
            timeout=180,
        )
        elapsed = time.perf_counter() - started
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        try:
            output = body["choices"][0]["message"]["content"]
        except Exception:
            output = json.dumps(body, ensure_ascii=False)[:2000]
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        return {
            "status": "ready" if response.status_code < 400 else "http_error",
            "secret_printed": False,
            "model": model,
            "loaded_before": loaded_before,
            "loaded_by_helper": loaded_by_helper,
            "unload_after": unload_after,
            "elapsed_seconds": round(elapsed, 3),
            "completion_tokens": usage.get("completion_tokens"),
            "analysis": output,
        }
    finally:
        if loaded_by_helper and unload_after:
            unload_model(model)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", help="Path to redacted input text.")
    parser.add_argument("--text", help="Redacted input text. Prefer --input-file for longer content.")
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--model", default=os.getenv("LOCAL_OPENAI_MODEL") or DEFAULT_MODEL)
    parser.add_argument("--base-url", default=os.getenv("LOCAL_OPENAI_BASE_URL") or DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=os.getenv("LOCAL_OPENAI_API_KEY") or LOCAL_OPENAI_API_KEY)
    parser.add_argument("--no-auto-load", action="store_true")
    parser.add_argument("--keep-loaded", action="store_true")
    parser.add_argument("--out", default="logs/local_llm_analyst.jsonl")
    args = parser.parse_args()

    if args.input_file:
        text = Path(args.input_file).read_text(encoding="utf-8", errors="ignore")
    else:
        text = args.text or ""
    result = analyze_text(
        text,
        task=args.task,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        auto_load=not args.no_auto_load,
        unload_after=not args.keep_loaded,
    )
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "input_chars": len(text),
        "task": args.task,
        "result": result,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"ready", "blocked_input", "blocked_model_not_loaded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
