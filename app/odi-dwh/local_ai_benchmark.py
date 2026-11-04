"""Local AI benchmark helper for RTX/local-first planning.

This script does not download models. It probes an OpenAI-compatible local
endpoint, sends a small Oracle/ODI task, and records basic latency plus GPU
memory before/after. Use it after LM Studio, Ollama OpenAI mode, llama.cpp,
vLLM, or NIM exposes an OpenAI-compatible API.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests

from config import LOCAL_OPENAI_API_KEY, LOCAL_OPENAI_BASE_URL, LOCAL_OPENAI_MODEL


DEFAULT_PROMPT = (
    "Suggest a column strategy for modeling HR.EMPLOYEES in the ODS layer, "
    "audit columns, and reconciliation tests in short bullet points."
)


def gpu_snapshot() -> dict:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"available": False, "error": proc.stderr.strip()}
        name, total, used, util, temp = [p.strip() for p in proc.stdout.strip().split(",", 4)]
        return {
            "available": True,
            "name": name,
            "memory_total_mib": int(float(total)),
            "memory_used_mib": int(float(used)),
            "gpu_util_percent": int(float(util)),
            "temperature_c": int(float(temp)),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def call_openai_compatible(base_url: str, model: str, prompt: str, api_key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a local assistant giving concise, technical, test focused answers about Oracle, ODI, and DWH.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }
    started = time.perf_counter()
    res = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload, timeout=180)
    elapsed = time.perf_counter() - started
    body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {"text": res.text}
    text = ""
    try:
        text = body["choices"][0]["message"]["content"]
    except Exception:
        text = json.dumps(body, ensure_ascii=False)[:2000]
    usage = body.get("usage", {}) if isinstance(body, dict) else {}
    completion_tokens = usage.get("completion_tokens") or 0
    return {
        "status_code": res.status_code,
        "elapsed_seconds": round(elapsed, 3),
        "completion_tokens": completion_tokens,
        "approx_tokens_per_second": round(completion_tokens / elapsed, 2) if completion_tokens else None,
        "text_preview": text[:1200],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("LOCAL_OPENAI_BASE_URL") or LOCAL_OPENAI_BASE_URL)
    parser.add_argument("--model", default=os.getenv("LOCAL_OPENAI_MODEL") or LOCAL_OPENAI_MODEL)
    parser.add_argument("--api-key", default=os.getenv("LOCAL_OPENAI_API_KEY") or LOCAL_OPENAI_API_KEY)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--out", default="logs/local_ai_benchmark.jsonl")
    args = parser.parse_args()

    before = gpu_snapshot()
    result = call_openai_compatible(args.base_url, args.model, args.prompt, args.api_key)
    after = gpu_snapshot()
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "model": args.model,
        "prompt": args.prompt,
        "gpu_before": before,
        "gpu_after": after,
        "result": result,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if result["status_code"] < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
