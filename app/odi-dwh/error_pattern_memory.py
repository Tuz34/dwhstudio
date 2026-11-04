"""Sanitized ODI/ORA error-pattern capture for local RAG.

This module stores only high-level troubleshooting patterns. It deliberately
blocks secrets, connection strings, .env-like assignments, and raw row samples
instead of trying to redact and guess.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import rag_manager


class SensitivePatternError(ValueError):
    """Raised when an error pattern contains data that must not be persisted."""


SECRET_LIKE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b[A-Z0-9_.-]*(password|passwd|pwd|secret|token|api[_-]?key)"
            r"\s*[:=]\s*['\"]?[^'\"\s,;]+"
        ),
    ),
    (
        "connection_string",
        re.compile(
            r"(?i)\b(jdbc:oracle:thin:@|jdbc:|user id\s*=|uid\s*=|pwd\s*=|"
            r"password\s*=|://[^/\s:@]+:[^@\s]+@)"
        ),
    ),
    (
        "raw_rows",
        re.compile(r"(?i)\b(raw_rows|sample_rows|preview_rows|row_data|rows)\s*[:=]\s*[\[{(]"),
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
)

ERROR_CODE_RE = re.compile(r"^(ORA|ODI|MSSQL)-[A-Z0-9_]+$")


@dataclass(frozen=True)
class ErrorPattern:
    code: str
    title: str
    context: str
    symptom: str
    root_cause: str
    resolution: str
    guardrails: str = ""
    source: str = "project_learning"
    tags: tuple[str, ...] = ()


def _scan_sensitive(value: str) -> list[str]:
    findings: list[str] = []
    for name, pattern in SECRET_LIKE_PATTERNS:
        if pattern.search(value):
            findings.append(name)
    return findings


def assert_safe_for_rag(fields: dict[str, str]) -> None:
    """Block persistence if any field looks like a secret or raw-data sample."""

    for field, value in fields.items():
        findings = _scan_sensitive(value or "")
        if findings:
            raise SensitivePatternError(
                f"{field} contains blocked sensitive pattern(s): {', '.join(sorted(findings))}"
            )


def normalize_error_code(code: str) -> str:
    normalized = (code or "").strip().upper().replace(" ", "_")
    if not ERROR_CODE_RE.match(normalized):
        raise ValueError(f"Unsupported error code format: {code!r}")
    return normalized


def _stable_doc_id(pattern: ErrorPattern) -> str:
    base = f"{pattern.code}|{pattern.title}|{pattern.context}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Z0-9]+", "_", pattern.code.upper()).strip("_").lower()
    return f"learned_{slug}_{digest}"


def build_pattern_text(pattern: ErrorPattern) -> str:
    parts = [
        f"{pattern.code}: {pattern.title}",
        f"Context: {pattern.context}",
        f"Symptom: {pattern.symptom}",
        f"Root cause: {pattern.root_cause}",
        f"Resolution: {pattern.resolution}",
    ]
    if pattern.guardrails:
        parts.append(f"Guardrails: {pattern.guardrails}")
    if pattern.tags:
        parts.append("Tags: " + ", ".join(pattern.tags))
    return "\n".join(parts)


def add_error_pattern(pattern: ErrorPattern, doc_id: str | None = None) -> dict[str, Any]:
    """Validate and add a sanitized error pattern to local SimpleRAG."""

    normalized = ErrorPattern(
        code=normalize_error_code(pattern.code),
        title=pattern.title.strip(),
        context=pattern.context.strip(),
        symptom=pattern.symptom.strip(),
        root_cause=pattern.root_cause.strip(),
        resolution=pattern.resolution.strip(),
        guardrails=pattern.guardrails.strip(),
        source=pattern.source.strip() or "project_learning",
        tags=tuple(tag.strip() for tag in pattern.tags if tag.strip()),
    )
    fields = {
        "code": normalized.code,
        "title": normalized.title,
        "context": normalized.context,
        "symptom": normalized.symptom,
        "root_cause": normalized.root_cause,
        "resolution": normalized.resolution,
        "guardrails": normalized.guardrails,
        "source": normalized.source,
        "tags": " ".join(normalized.tags),
    }
    assert_safe_for_rag(fields)

    text = build_pattern_text(normalized)
    assert_safe_for_rag({"document_text": text})

    final_doc_id = doc_id or _stable_doc_id(normalized)
    metadata = {
        "category": "learned_error_pattern",
        "error_code": normalized.code,
        "source": normalized.source,
        "tags": list(normalized.tags),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "safety": "blocked_secret_like_and_raw_rows",
    }
    saved_id = rag_manager.add_document("error_patterns", text, metadata, final_doc_id)
    return {
        "status": "stored",
        "doc_id": saved_id,
        "error_code": normalized.code,
        "metadata": metadata,
    }


def search_error_patterns(query: str, n_results: int = 3) -> list[str]:
    assert_safe_for_rag({"query": query})
    return rag_manager.search_documents("error_patterns", query, n_results=n_results)


RECENT_SAFE_PATTERNS: tuple[ErrorPattern, ...] = (
    ErrorPattern(
        code="ODI-METAMODEL_DATA_FORMAT",
        title="Missing DATA_FORMAT property definition in ODI component metamodel",
        context="ODI SDK DatastoreComponent creation or ODI Studio Create As validation",
        symptom="Mapping component creation fails while resolving DATA_FORMAT.",
        root_cause=(
            "Repository seeded component metadata is incomplete or stale for the ODI binary version."
        ),
        resolution=(
            "Use the official seeded object refresh path or a clean RCU rebuild, then verify the "
            "DATA_FORMAT definition exists before creating mappings."
        ),
        guardrails="Do not insert or patch SNP_* metamodel rows manually.",
        tags=("odi", "metamodel", "mapping"),
    ),
    ErrorPattern(
        code="ODI-STUDIO_FREEZE_METAMODEL",
        title="ODI Studio freezes after unsupported metamodel patch",
        context="ODI Studio opens a mapping whose repository metamodel was modified manually",
        symptom="Studio UI freezes or becomes unusable while opening mapping metadata.",
        root_cause=(
            "Unsupported manual repository metamodel edits can create invalid seeded properties."
        ),
        resolution=(
            "Remove the unsupported change only after reference checks, block the unsafe script path, "
            "and recreate mappings through supported ODI APIs."
        ),
        guardrails="Treat direct SNP_* metamodel edits as blocked unless an explicit vendor runbook exists.",
        tags=("odi", "studio", "safety"),
    ),
    ErrorPattern(
        code="ODI-16171",
        title="Oracle Merge KM requires a valid update key",
        context="ODI mapping physical design uses merge or incremental-update KM",
        symptom="KM assignment or validation reports that the target update key is missing.",
        root_cause="The target datastore does not expose a suitable key for merge semantics.",
        resolution=(
            "Reverse-engineer or define target key metadata before using merge KM, or choose a KM "
            "that matches the available target metadata."
        ),
        guardrails="Do not force KM assignment to hide missing target-key metadata.",
        tags=("odi", "km", "physical_design"),
    ),
    ErrorPattern(
        code="ORA-01031",
        title="Insufficient privileges during dev/TST setup or DDL",
        context="Oracle setup scripts or audit infrastructure initialization",
        symptom="DDL or grant operation is rejected because the current user lacks the needed privilege.",
        root_cause="The runtime user is narrower than the schema owner or setup user required for DDL.",
        resolution=(
            "Use the intended narrow setup user for initialization, keep runtime users scoped, and "
            "avoid broad grants when a direct owner connection is sufficient."
        ),
        guardrails="Do not store credentials in RAG or Linear; record only the role boundary and action.",
        tags=("oracle", "security", "ddl"),
    ),
)


def seed_recent_safe_patterns() -> list[dict[str, Any]]:
    return [add_error_pattern(pattern) for pattern in RECENT_SAFE_PATTERNS]


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage sanitized local ODI/ORA error patterns.")
    parser.add_argument("--seed-recent", action="store_true", help="Seed recent safe project learnings.")
    parser.add_argument("--search", help="Search error_patterns with a sanitized query.")
    args = parser.parse_args()

    if args.seed_recent:
        results = seed_recent_safe_patterns()
        print(f"stored={len(results)}")
        for result in results:
            print(f"{result['error_code']} {result['doc_id']}")
        return 0
    if args.search:
        for item in search_error_patterns(args.search):
            print(item)
            print("---")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
