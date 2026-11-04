"""Synthetic, offline transformation contracts; no vendor export is a fixture."""

import ast
import hashlib
import inspect
import json
from pathlib import Path

import pytest

import dim_product_scd2_ikm_compatibility as compat

_TASKS = (
    ("Insert flow into I$ table", "277", "1301", "222",
     r'odiRef.getColList("and\t","[COL_NAME]\t= 1","\nand\t","","SCD_FLAG")', 2),
    ("Flag rows for update", "285", "1309", "292",
     r'odiRef.getTargetColList("\t\tand\t", "T.[COL_NAME]\t= 1", "\n\t\tand\t", "", "SCD_FLAG")', 1),
    ("Update existing rows", "293", "1317", "322",
     r'odiRef.getTargetColList("and\t", "T.[COL_NAME]\t= 1", "\nand\t", "", "SCD_FLAG")', 1),
    ("Historize old rows", "294", "1318", "332",
     r'odiRef.getTargetColList("and\t", "T.[COL_NAME]\t= 1", "\nand\t", "", "(SCD_FLAG)")', 1),
    ("Update surrogate keys", "297", "1321", "342",
     r'odiRef.getTargetColList(",", "[COL_NAME]\t= 1", "\n\t\tand\t", "", "SCD_FLAG")', 0),
)
_INACTIVE = r'odiRef.getTargetColList("", "0", ",\n\t\t\t", "", "(SCD_FLAG and REW)")'
_OLD_END = "to_date ('01-01-2400', 'mm-dd-yyyy')"
_NEW_END = "to_date ('12-31-9999', 'mm-dd-yyyy')"
_VENDOR = Path(
    r"C:\Oracle\Middleware\Oracle_Home\odi\sdk\xml-reference\KM_IKM Oracle Slowly Changing Dimension.xml"
)


def _digest(value):
    return hashlib.sha256(value).hexdigest().upper()


def _field(name, value):
    return f'<Field name="{name}" type="java.lang.String"><![CDATA[{value}]]></Field>'


def _object(kind, fields):
    return f'<Object class="com.sunopsis.dwg.dbobj.{kind}">\r\n{fields}\r\n</Object>'


def _body(index, *, transformed=False):
    task = _TASKS[index]
    active = task[4].replace("= 1", "= 'Y'") if transformed else task[4]
    inactive = _INACTIVE.replace('"0"', '"\'N\'"') if transformed else _INACTIVE
    sentinel = _NEW_END if transformed else _OLD_END
    pieces = ["/* synthetic-template-only */", active]
    if task[2] == "1318":
        pieces.append(inactive)
    pieces.extend([sentinel] * task[5])
    pieces.append("/* untouched audit markers: INSERT_DATE UPDATE_DATE SOURCE_SYSTEM ETL_BATCH_ID */")
    return "\r\n".join(pieces)


def _fixture(*, transformed=False):
    parts = [
        '<?xml version="1.0" encoding="ISO-8859-1"?>',
        "<SunopsisExport>",
        '<Admin RepositoryVersion="synthetic" />',
        _object("SnpTrt", "".join(_field(key, value) for key, value in {
            "ITrt": "163", "TrtName": "IKM Oracle Slowly Changing Dimension",
            "IntgType": "SLOWLY_CHANGING_DIMENSION", "TrtType": "KI",
        }.items())),
    ]
    for index, (name, task_id, text_id, order, _active, _dates) in enumerate(_TASKS):
        parts.append(_object("SnpLineTrt", "".join(_field(key, value) for key, value in {
            "SqlName": name, "ITrt": "163", "ILineTrt": task_id,
            "DefITxt": text_id, "OrdTrt": order,
        }.items())))
        parts.append(_object("SnpTxtHeader", _field("ITxt", text_id) + _field("Txt", _body(
            index, transformed=transformed,
        ))))
    # Matching literal tokens in another object must remain byte-identical.
    parts.append(_object("SnpTxtHeader", _field("ITxt", "unrelated") + _field("Txt", _body(3))))
    parts.extend(["<!-- trailing whitespace and CRLF must survive -->", "</SunopsisExport>\t\r\n"])
    return "\r\n".join(parts).encode("iso-8859-1")


def _synthetic_plan(monkeypatch, data):
    # Production has no caller-supplied hash/profile override. Only tests patch the pin.
    monkeypatch.setattr(compat, "VENDOR_SHA256", _digest(data))
    return compat.plan_dim_product_scd2_ikm_compatibility(data)


def _assert_refused(plan, status):
    assert plan.report["status"] == status
    assert plan.transformed_xml is None
    assert plan.report["transformed_in_memory"] is False
    assert plan.report["replacement_site_count"] == 0
    assert plan.report["deployment_authorized"] is False


def test_exact_synthetic_transform_preserves_every_unrelated_byte_and_stays_blocked(monkeypatch):
    original = _fixture()
    before = _digest(original)
    plan = _synthetic_plan(monkeypatch, original)
    assert plan.report["status"] == "blocked_pending_audit_contract"
    assert _digest(plan.transformed_xml) == _digest(_fixture(transformed=True))
    assert _digest(original) == before
    assert plan.report["source_sha256"] == before
    assert plan.report["transformed_sha256"] == _digest(plan.transformed_xml)
    assert plan.report["transformed_task_count"] == 5
    assert plan.report["active_flag_site_count"] == 5
    assert plan.report["inactive_flag_site_count"] == 1
    assert plan.report["end_date_site_count"] == 5
    assert plan.report["replacement_site_count"] == 11
    assert plan.report["unchanged_object_count"] == 7
    assert plan.report["profile"] == "YN_9999_V1"
    assert plan.report["candidate_ikm_name"] == "IKM Oracle SCD2 YN9999 ODI68 V1"
    for key in (
        "deployment_authorized", "xml_identity_rewritten", "audit_semantics_rewritten",
        "connection_attempted", "write_attempted", "raw_xml_included", "raw_sql_included",
    ):
        assert plan.report[key] is False


def test_report_and_repr_expose_no_template_bodies(monkeypatch):
    plan = _synthetic_plan(monkeypatch, _fixture())
    for text in (json.dumps(plan.report), repr(plan)):
        assert "<![CDATA[" not in text
        assert "odiRef" not in text
        assert "synthetic-template-only" not in text
        assert "transformed_xml=" not in text
    assert list(inspect.signature(compat.plan_dim_product_scd2_ikm_compatibility).parameters) == ["vendor_xml"]


@pytest.mark.parametrize("data", [None, False, 1, "xml", bytearray(b"xml"), memoryview(b"xml"), {}])
def test_exact_bytes_type_is_required_without_parsing(monkeypatch, data):
    def forbidden(_data):
        raise AssertionError("parser must not be reached")

    monkeypatch.setattr(compat, "_transform", forbidden)
    _assert_refused(compat.plan_dim_product_scd2_ikm_compatibility(data), "blocked_input_type")


@pytest.mark.parametrize("data", [b"", b"not-xml", _fixture(), _fixture(transformed=True)])
def test_unknown_hash_never_reaches_parser(monkeypatch, data):
    def forbidden(_data):
        raise AssertionError("parser must not be reached")

    monkeypatch.setattr(compat, "_transform", forbidden)
    _assert_refused(compat.plan_dim_product_scd2_ikm_compatibility(data), "blocked_unknown_vendor_hash")


def test_reapplication_is_refused_by_public_hash_and_structural_contract(monkeypatch):
    plan = _synthetic_plan(monkeypatch, _fixture())
    again = compat.plan_dim_product_scd2_ikm_compatibility(plan.transformed_xml)
    _assert_refused(again, "blocked_unknown_vendor_hash")
    with pytest.raises(compat._ContractError):
        compat._transform(plan.transformed_xml)


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("mutation", ["missing_task", "duplicate_task", "missing_text", "duplicate_text"])
def test_missing_or_duplicate_required_objects_fail_closed(monkeypatch, index, mutation):
    source = _fixture()
    name, task_id, text_id, order, _call, _dates = _TASKS[index]
    fields = {"SqlName": name, "ITrt": "163", "ILineTrt": task_id, "DefITxt": text_id, "OrdTrt": order}
    task = _object("SnpLineTrt", "".join(_field(k, v) for k, v in fields.items())).encode()
    text = _object("SnpTxtHeader", _field("ITxt", text_id) + _field("Txt", _body(index))).encode()
    target = task if mutation.endswith("task") else text
    replacement = b"" if mutation.startswith("missing") else target * 2
    changed = source.replace(target, replacement, 1)
    _assert_refused(_synthetic_plan(monkeypatch, changed), "blocked_template_contract")


@pytest.mark.parametrize("field,value", [
    ("ITrt", "other"), ("TrtName", "Other IKM"), ("IntgType", "UPDATE"), ("TrtType", "X"),
    ("ILineTrt", "other"), ("DefITxt", "other"), ("OrdTrt", "0"),
])
def test_wrong_root_and_task_identity_are_refused(monkeypatch, field, value):
    originals = {
        "ITrt": "163", "TrtName": "IKM Oracle Slowly Changing Dimension",
        "IntgType": "SLOWLY_CHANGING_DIMENSION", "TrtType": "KI",
        "ILineTrt": "277", "DefITxt": "1301", "OrdTrt": "222",
    }
    changed = _fixture().replace(_field(field, originals[field]).encode(), _field(field, value).encode(), 1)
    _assert_refused(_synthetic_plan(monkeypatch, changed), "blocked_template_contract")


@pytest.mark.parametrize("field,old", [("ILineTrt", "279"), ("DefITxt", "1309")])
def test_duplicate_identity_under_different_task_name_is_refused(monkeypatch, field, old):
    # A different name cannot hide aliasing the first task's identity or text.
    fields = {"SqlName": "unrelated", "ITrt": "163", "ILineTrt": "other", "DefITxt": "other"}
    fields[field] = "277" if field == "ILineTrt" else "1301"
    extra = _object("SnpLineTrt", "".join(_field(k, v) for k, v in fields.items())).encode()
    changed = _fixture().replace(b"</SunopsisExport>", extra + b"</SunopsisExport>")
    _assert_refused(_synthetic_plan(monkeypatch, changed), "blocked_template_contract")


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "already_changed", "spacing_drift"])
def test_each_active_site_has_an_exact_literal_and_cardinality(monkeypatch, index, mutation):
    old = _TASKS[index][4]
    values = {"missing": "", "duplicate": old + old,
              "already_changed": old.replace("= 1", "= 'Y'"), "spacing_drift": old.replace("= 1", "=1")}
    original_body = _body(index).encode()
    changed_body = _body(index).replace(old, values[mutation]).encode()
    changed = _fixture().replace(original_body, changed_body, 1)
    _assert_refused(_synthetic_plan(monkeypatch, changed), "blocked_template_contract")


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_sentinel_site_cardinality_is_checked_per_task(monkeypatch, index, mutation):
    original = _body(index)
    replacement = original.replace(_OLD_END, "", 1) if mutation == "missing" else original + _OLD_END
    if index == 4 and mutation == "missing":
        replacement = original + _NEW_END  # this task must have no date sites
    changed = _fixture().replace(original.encode(), replacement.encode(), 1)
    _assert_refused(_synthetic_plan(monkeypatch, changed), "blocked_template_contract")


@pytest.mark.parametrize("replacement", ["", _INACTIVE * 2, _INACTIVE.replace('"0"', '"\'N\'"')])
def test_inactive_flag_site_is_required_exactly_once(monkeypatch, replacement):
    changed = _fixture().replace(_body(3).encode(), _body(3).replace(_INACTIVE, replacement).encode(), 1)
    _assert_refused(_synthetic_plan(monkeypatch, changed), "blocked_template_contract")


@pytest.mark.parametrize("mutation", [
    "duplicate_field", "split_cdata", "no_cdata", "text_outside_cdata",
    "duplicate_root", "wrong_root", "malformed", "doctype", "entity", "nested_object",
])
def test_xml_structure_is_fail_closed(monkeypatch, mutation):
    source = _fixture()
    field = _field("ITrt", "163").encode()
    body = _body(0).encode()
    changes = {
        "duplicate_field": source.replace(field, field * 2, 1),
        "split_cdata": source.replace(body, body + b"]]><![CDATA[", 1),
        "no_cdata": source.replace(b"<![CDATA[" + body + b"]]>", body, 1),
        "text_outside_cdata": source.replace(b"<![CDATA[" + body, b"extra<![CDATA[" + body, 1),
        "duplicate_root": source.replace(b"</SunopsisExport>", _object("SnpTrt", "").encode() + b"</SunopsisExport>"),
        "wrong_root": source.replace(b"SunopsisExport", b"OtherRoot"),
        "malformed": source[:-30],
        "doctype": source.replace(b"<SunopsisExport>", b"<!DOCTYPE SunopsisExport><SunopsisExport>"),
        "entity": source.replace(b"<SunopsisExport>", b'<!DOCTYPE SunopsisExport [<!ENTITY x "x">]><SunopsisExport>'),
        "nested_object": source.replace(field, field + _object("Other", "").encode(), 1),
    }
    _assert_refused(_synthetic_plan(monkeypatch, changes[mutation]), "blocked_template_contract")


def test_module_has_no_filesystem_runtime_or_writer_surface():
    tree = ast.parse(Path(compat.__file__).read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module)
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            assert name not in {"open", "read_bytes", "write_bytes", "write_text", "connect", "exec", "eval"}
    assert set(imports) <= {
        "__future__", "hashlib", "xml.etree.ElementTree", "dataclasses", "typing", "xml.parsers",
    }


@pytest.mark.skipif(not _VENDOR.is_file(), reason="Optional installed vendor XML is unavailable")
def test_installed_vendor_export_hash_and_transform_without_raw_output():
    source = _VENDOR.read_bytes()
    assert _digest(source) == compat.VENDOR_SHA256
    plan = compat.plan_dim_product_scd2_ikm_compatibility(source)
    assert plan.report["status"] == "blocked_pending_audit_contract"
    assert plan.report["replacement_site_count"] == 11
    assert plan.report["unchanged_object_count"] == 342
    assert plan.report["transformed_sha256"] == "27F81292541DD6E6280C20EC8768229C245DDAFF6F9DF57DC59BB85135953882"
    _assert_refused(compat.plan_dim_product_scd2_ikm_compatibility(plan.transformed_xml), "blocked_unknown_vendor_hash")
    assert _digest(_VENDOR.read_bytes()) == _digest(source)
