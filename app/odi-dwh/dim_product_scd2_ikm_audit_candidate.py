"""Pinned, offline task preparation only. The split candidate is NOT an import package."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from xml.parsers import expat

import dim_product_scd2_ikm_compatibility as compat

TARGET_COLUMNS = (
    "PRODUCT_SK", "PROD_ID", "PROD_NAME", "PROD_DESC", "PROD_SUBCATEGORY",
    "PROD_SUBCATEGORY_ID", "PROD_SUBCATEGORY_DESC", "PROD_CATEGORY",
    "PROD_CATEGORY_ID", "PROD_CATEGORY_DESC", "PROD_WEIGHT_CLASS",
    "PROD_UNIT_OF_MEASURE", "PROD_PACK_SIZE", "SUPPLIER_ID", "PROD_STATUS",
    "PROD_LIST_PRICE", "PROD_MIN_PRICE", "PROD_TOTAL", "PROD_TOTAL_ID",
    "EFFECTIVE_FROM", "EFFECTIVE_TO", "CURRENT_FLAG", "INSERT_DATE",
    "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID",
)
OPTION_LOCKS = (
    ("CREATE_TARG_TABLE", "0"), ("TRUNCATE", "0"), ("DELETE_ALL", "0"),
    ("RECYCLE_ERRORS", "0"), ("FLOW_CONTROL", "1"), ("COMMIT", "1"),
    ("STATIC_CONTROL", "0"), ("CONSTRAINTS", "None"), ("INDEXES", "None"),
)
_AUDIT_TASKS = (
    ("Create flow table (I$)", "273", "1297", "162"),
    ("Insert changing and new dimensions", "283", "1307", "352"),
    ("Historize old rows", "294", "1318", "332"),
    ("Validate KM options", "295", "1319", "72"),
)
_CHANGED_TEXT_IDS = frozenset({"1301", "1309", "1317", "1318", "1321", "1307", "1319"})
_PREFIX = "com.sunopsis.dwg.dbobj."
_MASK = re.compile(r"<%.*?%>|/\*.*?\*/|--[^\r\n]*|'(?:''|[^'])*'", re.DOTALL)
_COLUMN_LIST = re.compile(
    r'<%=\s*odiRef\.getTargetColList\(\s*""\s*,\s*"\[COL_NAME\]"\s*,\s*'
    r'"(?:\\.|[^"\\])*"\s*,\s*"[ \t]*"\s*,\s*""\s*\)\s*%>'
)


@dataclass(frozen=True)
class NewAuditTask:
    name: str = "Populate ODI68 flow audit"
    order: int = 253
    repository_id: None = None
    connection_id: str = "1"
    commit: str = "0"
    technology: str = "ORACLE"
    body: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class CandidateRepresentation:
    """Two separate pieces, deliberately lacking identity allocation and variable binding.

    existing_objects_xml alone omits the new task and must NEVER be imported.
    No method serializes these pieces to an export/import package.
    """

    existing_objects_xml: bytes = field(repr=False)
    new_task: NewAuditTask
    target_columns: tuple[str, ...] = TARGET_COLUMNS
    option_locks: tuple[tuple[str, str], ...] = OPTION_LOCKS


@dataclass(frozen=True)
class AuditCandidate:
    """Only report is a diagnostic surface; do not recursively serialize the dataclass."""

    report: dict[str, Any]
    candidate: CandidateRepresentation | None = field(default=None, repr=False)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _report(status: str, **counts: Any) -> dict[str, Any]:
    return {
        "status": status,
        "profile": "YN_9999_AUDIT_CANDIDATE_V1",
        "candidate_ikm_name": compat.CANDIDATE_IKM_NAME,
        "representation": "existing_objects_plus_unallocated_task",
        "deployment_authorized": False,
        "exportable_xml": False,
        "identity_rewritten": False,
        "batch_binding_verified": False,
        "source_binding_verified": False,
        "rendered_metadata_verified": False,
        "connection_attempted": False,
        "write_attempted": False,
        "raw_xml_included": False,
        "raw_sql_included": False,
        "transformed_in_memory": False,
        "unresolved_bindings": ["caller_batch", "source_system"],
        "unresolved_binding_count": 2,
        "required_source_system": "SH",
        "required_scenario_version": "002",
        "compatibility_replacement_site_count": 0,
        "changed_existing_task_count": 0,
        "new_task_count": 0,
        **counts,
    }


def _require(condition: bool) -> None:
    if not condition:
        raise compat._ContractError()


def _validate_records(records: list[tuple[str, dict[str, str]]]) -> int:
    tasks = [f for c, f in records if c == _PREFIX + "SnpLineTrt"]
    for key in ("ILineTrt", "OrdTrt", "DefITxt"):
        values = [f.get(key) for f in tasks if f.get(key) != "null"]
        _require(None not in values and len(values) == len(set(values)))
    _require(not any(f.get("OrdTrt") == "253" or f.get("SqlName") == "Populate ODI68 flow audit" for f in tasks))
    for name, task_id, text_id, order in _AUDIT_TASKS:
        _, task = compat._unique(records, "SnpLineTrt", "SqlName", name)
        _require(all(task.get(k) == v for k, v in {
            "ITrt": "163", "ILineTrt": task_id, "DefITxt": text_id, "OrdTrt": order,
        }.items()))
        compat._unique(records, "SnpTxtHeader", "ITxt", text_id)
    for name, value in OPTION_LOCKS:
        _, option = compat._unique(records, "SnpUserExit", "UeName", name)
        _require(option.get("ITrt") == "163" and option.get("ShortValue") == value)
    return len(tasks)


def _body_span(
    xml: bytes, records: list[tuple[str, dict[str, str]]],
    spans: dict[int, list[tuple[int, int]]], text_id: str,
) -> tuple[int, int, bytes]:
    index, record = compat._unique(records, "SnpTxtHeader", "ITxt", text_id)
    ranges = spans.get(index, [])
    _require(len(ranges) == 1)
    begin, end = ranges[0]
    body = xml[begin:end]
    normalized = body.decode("iso-8859-1").replace("\r\n", "\n").replace("\r", "\n")
    _require(normalized == record.get("Txt"))
    return begin, end, body


def _mask_sql(body: str) -> str:
    # Only this hash-pinned template grammar is accepted; this is not a SQL parser.
    return _MASK.sub(lambda match: " " * len(match.group()), body)


def _positions(mask: str, word: str, expected_count: int) -> list[re.Match[str]]:
    matches = list(re.finditer(r"\b" + word + r"\b", mask, re.IGNORECASE))
    _require(len(matches) == expected_count)
    return matches


def _validate_flow(body: bytes) -> None:
    text = body.decode("iso-8859-1")
    calls = re.findall(
        r'odiRef\.getTargetColList\(\s*""\s*,\s*"([^"\r\n]*)"\s*,\s*'
        r'"(?:\\.|[^"\\])*"\s*,\s*","\s*,\s*""\s*\)', text,
    )
    _require(len(calls) == 1)
    _require(calls[0].replace(r"\t", " ").upper().split() == ["[COL_NAME]", "[DEST_WRI_DT]", "NULL"])
    _require(len(re.findall(r"\bIND_UPDATE\s+CHAR\(1\)", text, re.IGNORECASE)) == 1)


def _expire_body(body: bytes) -> bytes:
    text = body.decode("iso-8859-1")
    mask = _mask_sql(text)
    _require('"UPDATE_DATE"' not in text and "SYSTIMESTAMP" not in text.upper())
    start = _positions(mask, "set", 1)[0].end()
    select = _positions(mask, "select", 2)[0]
    source = _positions(mask, "from", 2)[0]
    opening = re.match(r"\s*\(", mask[start:])
    _require(opening is not None)
    left = start + opening.end() - 1
    depth = 1
    right = left
    while depth and right + 1 < len(mask):
        right += 1
        depth += (mask[right] == "(") - (mask[right] == ")")
    _require(depth == 0 and left < right < select.start() < source.start())
    _require(re.fullmatch(r"\s*=\s*\(\s*", mask[right + 1:select.start()]) is not None)
    # Insert two fragments, retaining the original predicates, correlation and expressions.
    result = text[:source.start()] + ",\n\tSYSTIMESTAMP\n" + text[source.start():]
    result = result[:right] + ',\n\t"UPDATE_DATE"' + result[right:]
    return result.encode("iso-8859-1")


def _insert_body(body: bytes) -> bytes:
    text = body.decode("iso-8859-1")
    mask = _mask_sql(text)
    select = _positions(mask, "select", 1)[0]
    source = _positions(mask, "from", 1)[0]
    _positions(mask, "insert", 1)
    _positions(mask, "where", 1)
    _require(select.end() < source.start())
    _require(_COLUMN_LIST.fullmatch(text[select.end():source.start()].strip()) is not None)
    expressions = [
        "SYSTIMESTAMP" if column == "INSERT_DATE" else
        "CAST(NULL AS TIMESTAMP)" if column == "UPDATE_DATE" else f'"{column}"'
        for column in TARGET_COLUMNS
    ]
    projection = "\n\t" + ",\n\t".join(expressions) + "\n"
    return (text[:select.end()] + projection + text[source.start():]).encode("iso-8859-1")


def _option_guard() -> bytes:
    checks = [
        f'if (!"{value}".equals(odiRef.getOption("{name}"))) {{\n'
        '    throw new IllegalStateException("ODI68OptionContract");\n}'
        for name, value in OPTION_LOCKS
    ]
    return ("<%\n" + "\n".join(checks) + "\n%>\n").encode("ascii")


def _population_task() -> NewAuditTask:
    # Deliberately invalid binding tokens: this task cannot silently use a session/default value.
    body = (
        'update <%=odiRef.getTable("L", "INT_NAME", "A")%>\n'
        'set "EFFECTIVE_FROM" = SYSDATE,\n'
        '    "INSERT_DATE" = SYSTIMESTAMP,\n'
        '    "UPDATE_DATE" = CAST(NULL AS TIMESTAMP),\n'
        '    "SOURCE_SYSTEM" = __ODI68_UNVERIFIED_SOURCE_SYSTEM__,\n'
        '    "ETL_BATCH_ID" = __ODI68_UNVERIFIED_CALLER_BATCH__\n'
        "where IND_UPDATE = 'I'"
    )
    return NewAuditTask(body=body.encode("ascii"))


def _audit_transform(xml: bytes) -> tuple[CandidateRepresentation, int]:
    records = compat._records(xml)
    task_count = _validate_records(records)
    spans = compat._text_spans(xml)
    _validate_flow(_body_span(xml, records, spans, "1297")[2])
    edits = []
    for text_id in ("1318", "1307", "1319"):
        begin, end, body = _body_span(xml, records, spans, text_id)
        if text_id == "1318":
            changed = _expire_body(body)
        elif text_id == "1307":
            changed = _insert_body(body)
        else:
            _require(b"ODI68OptionContract" not in body)
            changed = _option_guard() + body
        edits.append((begin, end, changed))
    result = xml
    for begin, end, body in sorted(edits, reverse=True):
        result = result[:begin] + body + result[end:]
    return CandidateRepresentation(result, _population_task()), task_count


def _verify_changed_objects(original: bytes, candidate: bytes) -> int:
    before, after = compat._records(original), compat._records(candidate)
    _require(len(before) == len(after))
    expected = {compat._unique(before, "SnpTxtHeader", "ITxt", key)[0] for key in _CHANGED_TEXT_IDS}
    changed = {i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]}
    _require(changed == expected)
    for index in changed:
        old_class, old = before[index]
        new_class, new = after[index]
        _require(old_class == new_class and old.keys() == new.keys())
        _require(all(old[key] == new[key] for key in old if key != "Txt"))
    return len(before) - len(changed)


def prepare_dim_product_scd2_ikm_audit_candidate(vendor_xml: bytes) -> AuditCandidate:
    """Pinned bytes -> substantive, in-memory candidate; no I/O or binding overrides.

    A successful transformation is still blocked: variable transport, actual rendered
    metadata, identity duplication and runtime safety have NOT been verified.
    """
    if type(vendor_xml) is not bytes:
        return AuditCandidate(_report("blocked_input_type"))
    if _digest(vendor_xml) != compat.VENDOR_SHA256:
        return AuditCandidate(_report("blocked_unknown_vendor_hash"))
    base = compat.plan_dim_product_scd2_ikm_compatibility(vendor_xml)
    if base.transformed_xml is None:
        return AuditCandidate(_report("blocked_compatibility_template_contract"))
    try:
        candidate, task_count = _audit_transform(base.transformed_xml)
        unchanged = _verify_changed_objects(vendor_xml, candidate.existing_objects_xml)
    except (compat._ContractError, ET.ParseError, expat.ExpatError, UnicodeError):
        return AuditCandidate(_report("blocked_audit_template_contract"))
    return AuditCandidate(_report(
        "blocked_pending_bindings_and_runtime_verification",
        transformed_in_memory=True,
        source_sha256=_digest(vendor_xml),
        existing_objects_sha256=_digest(candidate.existing_objects_xml),
        new_task_sha256=_digest(candidate.new_task.body),
        compatibility_replacement_site_count=11,
        changed_existing_task_count=7,
        new_task_count=1,
        new_task_order=253,
        existing_task_count=task_count,
        candidate_task_count=task_count + 1,
        unchanged_object_count=unchanged,
        fixed_projection_column_count=26,
        expected_flow_column_count=27,
        option_lock_count=len(OPTION_LOCKS),
        candidate_effective_clock_statement_count=1,
        additional_time_variable_required=False,
    ), candidate)
