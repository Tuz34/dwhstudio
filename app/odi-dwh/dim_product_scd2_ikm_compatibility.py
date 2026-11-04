"""Offline, byte-preserving preparation only; this is not an importable IKM release."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from xml.parsers import expat

VENDOR_SHA256 = "B22B61DA35FCE13B4E15B2EE2CA29888EFF4DC2CD92D85BFF4DCA8B5FE5070F4"
PROFILE = "YN_9999_V1"
CANDIDATE_IKM_NAME = "IKM Oracle SCD2 YN9999 ODI68 V1"
STOCK_IKM_NAME = "IKM Oracle Slowly Changing Dimension"
_PREFIX = "com.sunopsis.dwg.dbobj."
_OLD_END = b"to_date ('01-01-2400', 'mm-dd-yyyy')"
_NEW_END = b"to_date ('12-31-9999', 'mm-dd-yyyy')"
_INACTIVE_CALL = (
    r'odiRef.getTargetColList("", "0", ",\n\t\t\t", "", "(SCD_FLAG and REW)")'
)


@dataclass(frozen=True)
class _Task:
    name: str
    task_id: str
    text_id: str
    order: str
    active_call: str
    end_sites: int
    inactive_sites: int = 0


_TASKS = (
    _Task("Insert flow into I$ table", "277", "1301", "222",
          r'odiRef.getColList("and\t","[COL_NAME]\t= 1","\nand\t","","SCD_FLAG")', 2),
    _Task("Flag rows for update", "285", "1309", "292",
          r'odiRef.getTargetColList("\t\tand\t", "T.[COL_NAME]\t= 1", "\n\t\tand\t", "", "SCD_FLAG")', 1),
    _Task("Update existing rows", "293", "1317", "322",
          r'odiRef.getTargetColList("and\t", "T.[COL_NAME]\t= 1", "\nand\t", "", "SCD_FLAG")', 1),
    _Task("Historize old rows", "294", "1318", "332",
          r'odiRef.getTargetColList("and\t", "T.[COL_NAME]\t= 1", "\nand\t", "", "(SCD_FLAG)")', 1, 1),
    _Task("Update surrogate keys", "297", "1321", "342",
          r'odiRef.getTargetColList(",", "[COL_NAME]\t= 1", "\n\t\tand\t", "", "SCD_FLAG")', 0),
)


class _ContractError(ValueError):
    """An expected source identity, cardinality or literal did not match."""


@dataclass(frozen=True)
class CompatibilityPlan:
    """Only report is suitable for logging; XML stays in memory and out of repr."""

    report: dict[str, Any]
    transformed_xml: bytes | None = field(default=None, repr=False)


def _report(status: str, **counts: Any) -> dict[str, Any]:
    return {
        "status": status,
        "profile": PROFILE,
        "candidate_ikm_name": CANDIDATE_IKM_NAME,
        "deployment_authorized": False,
        "xml_identity_rewritten": False,
        "audit_semantics_rewritten": False,
        "connection_attempted": False,
        "write_attempted": False,
        "raw_xml_included": False,
        "raw_sql_included": False,
        "transformed_in_memory": False,
        "transformed_task_count": 0,
        "active_flag_site_count": 0,
        "inactive_flag_site_count": 0,
        "end_date_site_count": 0,
        "replacement_site_count": 0,
        **counts,
    }


def _records(xml: bytes) -> list[tuple[str, dict[str, str]]]:
    # The pinned export has neither a DTD nor entities; do not expand either.
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise _ContractError()
    root = ET.fromstring(xml)
    objects = root.findall("Object")
    if root.tag != "SunopsisExport" or len(objects) != len(root.findall(".//Object")):
        raise _ContractError()
    records = []
    for obj in objects:
        fields = obj.findall("Field")
        names = [node.get("name") for node in fields]
        if None in names or len(names) != len(set(names)) or any(len(node) for node in fields):
            raise _ContractError()
        records.append((obj.get("class", ""), {node.attrib["name"]: node.text or "" for node in fields}))
    return records


def _text_spans(xml: bytes) -> dict[int, list[tuple[int, int]]]:
    """Use parser byte offsets so unrelated XML bytes are never reserialized."""
    parser = expat.ParserCreate()
    path: list[str] = []
    object_index = -1
    text_field = False
    start_offset: int | None = None
    spans: dict[int, list[tuple[int, int]]] = {}

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal object_index, text_field
        path.append(name)
        if path == ["SunopsisExport", "Object"]:
            object_index += 1
        if path == ["SunopsisExport", "Object", "Field"]:
            text_field = attrs.get("name") == "Txt"

    def end(name: str) -> None:
        nonlocal text_field
        if path == ["SunopsisExport", "Object", "Field"]:
            text_field = False
        path.pop()

    def start_cdata() -> None:
        nonlocal start_offset
        if text_field:
            start_offset = parser.CurrentByteIndex + len(b"<![CDATA[")

    def end_cdata() -> None:
        nonlocal start_offset
        if text_field and start_offset is not None:
            spans.setdefault(object_index, []).append((start_offset, parser.CurrentByteIndex))
            start_offset = None

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartCdataSectionHandler = start_cdata
    parser.EndCdataSectionHandler = end_cdata
    parser.Parse(xml, True)
    return spans


def _unique(
    records: list[tuple[str, dict[str, str]]], kind: str, key: str, value: str,
) -> tuple[int, dict[str, str]]:
    matches = [(index, fields) for index, (cls, fields) in enumerate(records)
               if cls == _PREFIX + kind and fields.get(key) == value]
    if len(matches) != 1:
        raise _ContractError()
    return matches[0]


def _replace_body(body: bytes, task: _Task) -> bytes:
    active = task.active_call.encode("ascii")
    inactive = _INACTIVE_CALL.encode("ascii")
    if (
        body.count(active) != 1
        or body.count(inactive) != task.inactive_sites
        or body.count(_OLD_END) != task.end_sites
        or body.count(b"01-01-2400") != task.end_sites
        or _NEW_END in body
        or b"= 'Y'" in body
        or b'"\'N\'"' in body
    ):
        raise _ContractError()
    result = body.replace(active, active.replace(b"= 1", b"= 'Y'"))
    if task.inactive_sites:
        result = result.replace(inactive, inactive.replace(b'"0"', b'"\'N\'"'))
    return result.replace(_OLD_END, _NEW_END)


def _transform(xml: bytes) -> tuple[bytes, int]:
    """Private structural transformer; the public entry point pins input bytes first."""
    records = _records(xml)
    roots = [fields for cls, fields in records if cls == _PREFIX + "SnpTrt"]
    if len(roots) != 1 or any(roots[0].get(key) != value for key, value in {
        "ITrt": "163", "TrtName": STOCK_IKM_NAME,
        "IntgType": "SLOWLY_CHANGING_DIMENSION", "TrtType": "KI",
    }.items()):
        raise _ContractError()
    spans = _text_spans(xml)
    edits = []
    for task in _TASKS:
        task_index, fields = _unique(records, "SnpLineTrt", "SqlName", task.name)
        if any(fields.get(key) != value for key, value in {
            "ITrt": "163", "ILineTrt": task.task_id, "DefITxt": task.text_id, "OrdTrt": task.order,
        }.items()):
            raise _ContractError()
        if _unique(records, "SnpLineTrt", "ILineTrt", task.task_id)[0] != task_index:
            raise _ContractError()
        if _unique(records, "SnpLineTrt", "DefITxt", task.text_id)[0] != task_index:
            raise _ContractError()
        index, text = _unique(records, "SnpTxtHeader", "ITxt", task.text_id)
        ranges = spans.get(index, [])
        if len(ranges) != 1:
            raise _ContractError()
        begin, end = ranges[0]
        body = xml[begin:end]
        if body.decode("iso-8859-1").replace("\r\n", "\n").replace("\r", "\n") != text.get("Txt"):
            raise _ContractError()
        edits.append((begin, end, _replace_body(body, task)))

    result = xml
    for begin, end, body in sorted(edits, reverse=True):
        result = result[:begin] + body + result[end:]
    # Validate the artifact, without ever writing or exposing a partial transformation.
    after = _records(result)
    changed = [i for i, (before, later) in enumerate(zip(records, after)) if before != later]
    expected = {_unique(records, "SnpTxtHeader", "ITxt", task.text_id)[0] for task in _TASKS}
    if len(after) != len(records) or set(changed) != expected:
        raise _ContractError()
    return result, len(records)


def plan_dim_product_scd2_ikm_compatibility(vendor_xml: bytes) -> CompatibilityPlan:
    """Prepare exactly one pinned stock export; no paths, writers or runtime callbacks.

    Even a valid transform is blocked_pending_audit_contract, never ready/passed.
    The candidate fork name is metadata only: source XML identities remain intact.
    Transformed XML cannot be reapplied because it no longer has the vendor hash.
    """
    if type(vendor_xml) is not bytes:
        return CompatibilityPlan(_report("blocked_input_type"))
    source_hash = hashlib.sha256(vendor_xml).hexdigest().upper()
    if source_hash != VENDOR_SHA256:
        return CompatibilityPlan(_report("blocked_unknown_vendor_hash"))
    try:
        transformed, object_count = _transform(vendor_xml)
    except (_ContractError, ET.ParseError, expat.ExpatError, UnicodeError):
        return CompatibilityPlan(_report("blocked_template_contract"))
    return CompatibilityPlan(
        _report(
            "blocked_pending_audit_contract",
            transformed_in_memory=True,
            transformed_task_count=5,
            active_flag_site_count=5,
            inactive_flag_site_count=1,
            end_date_site_count=5,
            replacement_site_count=11,
            unchanged_object_count=object_count - 5,
            source_sha256=source_hash,
            transformed_sha256=hashlib.sha256(transformed).hexdigest().upper(),
        ),
        transformed,
    )
