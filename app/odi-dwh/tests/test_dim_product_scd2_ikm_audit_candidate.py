"""Offline candidate contracts. Synthetic fixtures contain no vendor task bodies."""

import ast
import hashlib
import inspect
import json
import re
import subprocess
from pathlib import Path

import pytest

import dim_product_scd2_ikm_audit_candidate as audit
import dim_product_scd2_ikm_compatibility as compat
from tests import test_dim_product_scd2_ikm_compatibility as base


_COLUMNS = (
    "PRODUCT_SK", "PROD_ID", "PROD_NAME", "PROD_DESC", "PROD_SUBCATEGORY",
    "PROD_SUBCATEGORY_ID", "PROD_SUBCATEGORY_DESC", "PROD_CATEGORY",
    "PROD_CATEGORY_ID", "PROD_CATEGORY_DESC", "PROD_WEIGHT_CLASS",
    "PROD_UNIT_OF_MEASURE", "PROD_PACK_SIZE", "SUPPLIER_ID", "PROD_STATUS",
    "PROD_LIST_PRICE", "PROD_MIN_PRICE", "PROD_TOTAL", "PROD_TOTAL_ID",
    "EFFECTIVE_FROM", "EFFECTIVE_TO", "CURRENT_FLAG", "INSERT_DATE",
    "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID",
)
_LOCKS = (
    ("CREATE_TARG_TABLE", "0"), ("TRUNCATE", "0"), ("DELETE_ALL", "0"),
    ("RECYCLE_ERRORS", "0"), ("FLOW_CONTROL", "1"), ("COMMIT", "1"),
    ("STATIC_CONTROL", "0"), ("CONSTRAINTS", "None"), ("INDEXES", "None"),
)
_EQUALITY = "(A.VALUE = B.VALUE OR (A.VALUE IS NULL AND B.VALUE IS NULL))"
_EXPIRY = (
    'update TARGET T\nset ("CURRENT_FLAG", "EFFECTIVE_TO") = (\n'
    'select 0, I."EFFECTIVE_FROM"\nfrom FLOW I\n'
    "where I.PROD_ID = T.PROD_ID and I.IND_UPDATE = 'I'\n)\n"
    "where exists (select 1 from FLOW I where I.PROD_ID = T.PROD_ID and I.IND_UPDATE = 'I')\n"
    "/* " + base._body(3) + " */"
)
_INSERT = (
    'insert into TARGET (\n<%=odiRef.getTargetColList("", "[COL_NAME]", ",", "", "")%>\n)\n'
    'select\n<%=odiRef.getTargetColList("", "[COL_NAME]", ",", "", "")%>\n'
    "from FLOW\nwhere IND_UPDATE = 'I'"
)
_FLOW = (
    'create table FLOW (<%=odiRef.getTargetColList("", "[COL_NAME] [DEST_WRI_DT] null", ",", ",", "")%>'
    ' IND_UPDATE CHAR(1))'
)
_OPTIONS = "/* synthetic option-validation body; preserve exactly */"
_JAVA = Path(r"C:\Program Files\Java\jdk1.8.0_211\bin\java.exe")
_GROOVY = Path(r"C:\Oracle\Middleware\Oracle_Home\odi\oracle_common\modules\thirdparty\groovy-all-2.4.15.jar")
_CORE = Path(r"C:\Oracle\Middleware\Oracle_Home\odi\sdk\lib\odi-core.jar")


def _digest(value):
    return hashlib.sha256(value).hexdigest().upper()


def _task(name, task_id, text_id, order, body):
    fields = {
        "ITrt": "163", "ILineTrt": task_id, "DefITxt": text_id,
        "OrdTrt": order, "SqlName": name,
    }
    return (
        base._object("SnpLineTrt", "".join(base._field(k, v) for k, v in fields.items()))
        + base._object("SnpTxtHeader", base._field("ITxt", text_id) + base._field("Txt", body))
    ).encode()


def _fixture():
    xml = base._fixture().replace(base._body(3).encode(), _EXPIRY.encode(), 1)
    for index in (0, 1):
        old = base._body(index).encode()
        xml = xml.replace(old, old + b"\n/* " + _EQUALITY.encode() + b" */", 1)
    extra = b"".join((
        _task("Create flow table (I$)", "273", "1297", "162", _FLOW),
        _task("Insert changing and new dimensions", "283", "1307", "352", _INSERT),
        _task("Validate KM options", "295", "1319", "72", _OPTIONS),
    ))
    for name, value in _LOCKS:
        fields = {"ITrt": "163", "UeName": name, "ShortValue": value}
        extra += base._object("SnpUserExit", "".join(base._field(k, v) for k, v in fields.items())).encode()
    return xml.replace(b"</SunopsisExport>", extra + b"</SunopsisExport>")


def _prepare(monkeypatch, xml=None):
    xml = _fixture() if xml is None else xml
    monkeypatch.setattr(compat, "VENDOR_SHA256", _digest(xml))
    return audit.prepare_dim_product_scd2_ikm_audit_candidate(xml)


def _texts(xml):
    return {f["ITxt"]: f["Txt"] for c, f in compat._records(xml) if c.endswith(".SnpTxtHeader")}


def _refused(result, status):
    assert result.report["status"] == status
    assert result.candidate is None
    assert result.report["deployment_authorized"] is False
    assert result.report["transformed_in_memory"] is False


def test_candidate_has_real_body_edits_and_stays_blocked(monkeypatch):
    original = _fixture()
    result = _prepare(monkeypatch, original)
    assert result.report["status"] == "blocked_pending_bindings_and_runtime_verification"
    assert result.report["compatibility_replacement_site_count"] == 11
    assert result.report["changed_existing_task_count"] == 7
    assert result.report["new_task_count"] == 1
    assert result.report["fixed_projection_column_count"] == 26
    assert result.report["expected_flow_column_count"] == 27
    assert result.report["option_lock_count"] == 9
    assert result.report["unresolved_binding_count"] == 2
    candidate = result.candidate
    assert candidate.new_task.name == "Populate ODI68 flow audit"
    assert candidate.new_task.order == 253
    assert candidate.new_task.repository_id is None
    assert candidate.new_task.connection_id == "1"
    assert candidate.new_task.commit == "0"
    assert candidate.new_task.technology == "ORACLE"
    assert candidate.option_locks == _LOCKS
    assert candidate.target_columns == _COLUMNS
    assert _digest(original) == _digest(_fixture())
    for key in ("deployment_authorized", "exportable_xml", "identity_rewritten", "batch_binding_verified",
                "source_binding_verified", "rendered_metadata_verified", "connection_attempted", "write_attempted"):
        assert result.report[key] is False


def test_expiry_adds_only_one_assignment_and_one_value(monkeypatch):
    result = _prepare(monkeypatch)
    body = _texts(result.candidate.existing_objects_xml)["1318"]
    expected = _EXPIRY.replace('"EFFECTIVE_TO")', '"EFFECTIVE_TO",\n\t"UPDATE_DATE")', 1)
    expected = expected.replace('\nfrom FLOW I', '\n,\n\tSYSTIMESTAMP\nfrom FLOW I', 1)
    expected = expected.replace(base._body(3), base._body(3, transformed=True))
    assert _digest(body.encode()) == _digest(expected.replace("\r\n", "\n").encode())
    assert body.count('"UPDATE_DATE"') == 1
    assert body.count("SYSTIMESTAMP") == 1
    for forbidden in ('"INSERT_DATE"', '"SOURCE_SYSTEM"', '"ETL_BATCH_ID"', "SYSDATE"):
        assert forbidden not in body


def test_fixed_final_projection_is_positional_and_uses_flow_clock_and_batch(monkeypatch):
    result = _prepare(monkeypatch)
    expressions = [
        "SYSTIMESTAMP" if name == "INSERT_DATE" else
        "CAST(NULL AS TIMESTAMP)" if name == "UPDATE_DATE" else f'"{name}"'
        for name in _COLUMNS
    ]
    expected = _INSERT[:_INSERT.index("select") + 6] + "\n\t" + ",\n\t".join(expressions) + "\n"
    expected += _INSERT[_INSERT.index("from FLOW"):]
    body = _texts(result.candidate.existing_objects_xml)["1307"]
    assert _digest(body.encode()) == _digest(expected.encode())
    assert body.count("SYSTIMESTAMP") == 1
    assert "SYSDATE" not in body and "CURRENT_DATE" not in body and "SESS_NO" not in body


def test_population_is_one_candidate_only_update_with_one_effective_clock(monkeypatch):
    task = _prepare(monkeypatch).candidate.new_task
    expected = (
        'update <%=odiRef.getTable("L", "INT_NAME", "A")%>\n'
        'set "EFFECTIVE_FROM" = SYSDATE,\n'
        '    "INSERT_DATE" = SYSTIMESTAMP,\n'
        '    "UPDATE_DATE" = CAST(NULL AS TIMESTAMP),\n'
        '    "SOURCE_SYSTEM" = __ODI68_UNVERIFIED_SOURCE_SYSTEM__,\n'
        '    "ETL_BATCH_ID" = __ODI68_UNVERIFIED_CALLER_BATCH__\n'
        "where IND_UPDATE = 'I'"
    )
    assert _digest(task.body) == _digest(expected.encode())
    assert task.body.count(b"SYSDATE") == 1
    assert b"TARG_NAME" not in task.body and b"SESS_NO" not in task.body


def test_option_guards_are_exact_and_prepend_without_changing_stock_validation(monkeypatch):
    body = _texts(_prepare(monkeypatch).candidate.existing_objects_xml)["1319"]
    checks = [
        f'if (!"{value}".equals(odiRef.getOption("{name}"))) {{\n'
        '    throw new IllegalStateException("ODI68OptionContract");\n}'
        for name, value in _LOCKS
    ]
    expected = "<%\n" + "\n".join(checks) + "\n%>\n" + _OPTIONS
    assert _digest(body.encode()) == _digest(expected.encode())


def test_only_seven_existing_text_objects_change_null_predicates_and_other_bytes_survive(monkeypatch):
    original = _fixture()
    result = _prepare(monkeypatch, original)
    before = compat._records(original)
    after = compat._records(result.candidate.existing_objects_xml)
    changed = [a[1].get("ITxt") for a, b in zip(before, after) if a != b]
    assert set(changed) == {"1301", "1309", "1317", "1318", "1321", "1307", "1319"}
    assert len(after) == len(before)
    old_texts, new_texts = _texts(original), _texts(result.candidate.existing_objects_xml)
    for key in ("1301", "1309"):
        assert old_texts[key].count(_EQUALITY) == new_texts[key].count(_EQUALITY) == 1
    assert _digest(old_texts["1297"].encode()) == _digest(new_texts["1297"].encode())
    # Replacing only the seven CDATA spans must reconstruct the whole output.
    # Mixed-newline fixtures require the parser's exact original byte spans.
    spans = compat._text_spans(original)
    expected = original
    for index in reversed(range(len(before))):
        key = before[index][1].get("ITxt")
        if key in changed:
            begin, end = spans[index][0]
            replacement = result.candidate.existing_objects_xml
            after_spans = compat._text_spans(replacement)
            a, b = after_spans[index][0]
            expected = expected[:begin] + replacement[a:b] + expected[end:]
    assert _digest(expected) == _digest(result.candidate.existing_objects_xml)


@pytest.mark.parametrize("value", [None, False, 1, "xml", bytearray(b"xml"), memoryview(b"xml")])
def test_non_bytes_refused_before_parser(monkeypatch, value):
    monkeypatch.setattr(compat, "_records", lambda _x: pytest.fail("parser called"))
    _refused(audit.prepare_dim_product_scd2_ikm_audit_candidate(value), "blocked_input_type")


def test_hash_gate_and_reapply_precede_parsing(monkeypatch):
    result = _prepare(monkeypatch)
    monkeypatch.setattr(compat, "_records", lambda _x: pytest.fail("parser called"))
    for xml in (b"not XML", _fixture() + b" ", result.candidate.existing_objects_xml):
        _refused(audit.prepare_dim_product_scd2_ikm_audit_candidate(xml), "blocked_unknown_vendor_hash")


@pytest.mark.parametrize("name,value", _LOCKS)
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "drift"])
def test_locked_options_missing_duplicate_drift_fail_closed(monkeypatch, name, value, mutation):
    fields = {"ITrt": "163", "UeName": name, "ShortValue": value}
    obj = base._object("SnpUserExit", "".join(base._field(k, v) for k, v in fields.items())).encode()
    new = b"" if mutation == "missing" else obj * 2 if mutation == "duplicate" else obj.replace(
        base._field("ShortValue", value).encode(), base._field("ShortValue", "untrusted").encode())
    xml = _fixture().replace(obj, new, 1)
    _refused(_prepare(monkeypatch, xml), "blocked_audit_template_contract")


def test_occupied_new_task_order_refused(monkeypatch):
    extra = _task("Other task", "other", "other", "253", "synthetic")
    xml = _fixture().replace(b"</SunopsisExport>", extra + b"</SunopsisExport>")
    _refused(_prepare(monkeypatch, xml), "blocked_audit_template_contract")


@pytest.mark.parametrize("name,task_id,text_id,order", [
    ("Create flow table (I$)", "273", "1297", "162"),
    ("Insert changing and new dimensions", "283", "1307", "352"),
    ("Validate KM options", "295", "1319", "72"),
])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_link", "wrong_order"])
def test_added_task_identity_checks_are_fail_closed(monkeypatch, name, task_id, text_id, order, mutation):
    fields = {"ITrt": "163", "ILineTrt": task_id, "DefITxt": text_id, "OrdTrt": order, "SqlName": name}
    obj = base._object("SnpLineTrt", "".join(base._field(k, v) for k, v in fields.items())).encode()
    if mutation == "missing":
        replacement = b""
    elif mutation == "duplicate":
        replacement = obj * 2
    else:
        key, old = ("DefITxt", text_id) if mutation == "wrong_link" else ("OrdTrt", order)
        replacement = obj.replace(base._field(key, old).encode(), base._field(key, "other").encode())
    _refused(_prepare(monkeypatch, _fixture().replace(obj, replacement, 1)), "blocked_audit_template_contract")


@pytest.mark.parametrize("text_id", ["1297", "1307", "1319"])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "split_cdata"])
def test_additional_text_objects_require_one_exact_cdata_span(monkeypatch, text_id, mutation):
    body = {"1297": _FLOW, "1307": _INSERT, "1319": _OPTIONS}[text_id]
    obj = base._object("SnpTxtHeader", base._field("ITxt", text_id) + base._field("Txt", body)).encode()
    if mutation == "missing":
        replacement = b""
    elif mutation == "duplicate":
        replacement = obj * 2
    else:
        replacement = obj.replace(body.encode(), body.encode() + b"]]><![CDATA[", 1)
    _refused(_prepare(monkeypatch, _fixture().replace(obj, replacement, 1)), "blocked_audit_template_contract")


@pytest.mark.parametrize("old,new", [
    (_INSERT, _INSERT + "\nselect 1 from OTHER"),
    (_INSERT, _INSERT.replace("select", "deleted", 1)),
    (_EXPIRY, _EXPIRY.replace("set (", "set ", 1)),
    (_EXPIRY, _EXPIRY.replace('"EFFECTIVE_TO"', '"UPDATE_DATE"', 1)),
    (_FLOW, _FLOW.replace("[DEST_WRI_DT]", "VARCHAR2(50)")),
    (_FLOW, _FLOW.replace(" null", " not null")),
    (_FLOW, _FLOW.replace(" null", "")),
    (_INSERT, _INSERT.replace('"", "")%>\nfrom', '"", "SCD_INS")%>\nfrom')),
])
def test_structural_anchor_drift_refused_without_partial_candidate(monkeypatch, old, new):
    xml = _fixture().replace(old.encode(), new.encode(), 1)
    _refused(_prepare(monkeypatch, xml), "blocked_audit_template_contract")


def test_safe_reports_and_repr_never_contain_bodies(monkeypatch):
    result = _prepare(monkeypatch)
    for text in (repr(result), repr(result.candidate), repr(result.candidate.new_task), json.dumps(result.report)):
        for forbidden in ("<![CDATA[", "odiRef", "SYSTIMESTAMP", "select", "synthetic", "__ODI68_UNVERIFIED"):
            assert forbidden not in text
    assert list(inspect.signature(audit.prepare_dim_product_scd2_ikm_audit_candidate).parameters) == ["vendor_xml"]


def test_no_io_import_export_or_runtime_surface():
    tree = ast.parse(Path(audit.__file__).read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module)
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            assert name not in {"open", "read_bytes", "write_bytes", "write_text", "connect", "exec", "eval",
                                "print", "importObjectFromXml"}
    assert set(imports) <= {"__future__", "hashlib", "re", "dataclasses", "typing",
                           "xml.etree.ElementTree", "xml.parsers", "dim_product_scd2_ikm_compatibility"}


def test_fixed_column_order_matches_local_frozen_plan_without_application_imports():
    from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan

    metadata = [{"name": n, "data_type": t, "nullable": v} for n, t, v in PRODUCTS_SOURCE_METADATA]
    plan = build_dim_product_scd2_plan(metadata)
    assert tuple(c["name"] for c in plan["target_columns"]) == _COLUMNS


@pytest.mark.skipif(not base._VENDOR.is_file(), reason="Optional installed vendor XML unavailable")
def test_stock_null_comparison_remains_exact_and_has_null_safe_truth_table():
    original = base._VENDOR.read_bytes()
    candidate = audit.prepare_dim_product_scd2_ikm_audit_candidate(original).candidate
    before, after = _texts(original), _texts(candidate.existing_objects_xml)
    selector = "((SCD_UPD OR SCD_INS) AND REW) AND !TRG AND !SCD_NK"
    calls = re.findall(r'odiRef\.getColList\(([^\r\n]*)\)', before["1301"])
    selected = [call for call in calls if selector in call]
    selected_count = len(selected)
    assert selected_count == 1
    preserved_count = after["1301"].count(selected[0])
    assert preserved_count == 1
    args = re.findall(r'"((?:\\.|[^"\\])*)"', selected[0])
    expression = args[1]
    refs = list(dict.fromkeys(re.findall(r'\w+\.\[COL_NAME\]', expression)))
    assert len(refs) == 2
    for ref, token in zip(refs, ("a", "b")):
        expression = expression.replace(ref, token)
    normalized = re.sub(r"\s+", "", expression.lower())
    assert normalized in {"(a=bor(aisnullandbisnull))", "((a=b)or(aisnullandbisnull))"}
    assert "and" in args[2].lower()
    # Oracle three-valued equality inside EXISTS, plus the explicit both-NULL arm.
    for attribute in _COLUMNS[2:19]:
        assert attribute not in {"INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"}
        for left in (None, 1, 2):
            for right in (None, 1, 2):
                equality = None if left is None or right is None else left == right
                matches = equality is True or (left is None and right is None)
                assert matches == (left == right)


_RENDER_CHECK = r'''
import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import groovy.text.SimpleTemplateEngine
class OfflineRef {
    List columns
    Map options
    String getTable(String scope, String name, String location) {
        assert scope == 'L' && location == 'A'
        assert name in ['INT_NAME', 'TARG_NAME']
        name == 'INT_NAME' ? 'FLOW' : 'TARGET'
    }
    String getOption(String key) {
        assert options.containsKey(key)
        options[key]
    }
    String getTargetColList(String prefix, String format, String separator, String suffix, String selector) {
        def names
        if (!selector) names = columns
        else {
            def matches = [SCD_FLAG: 'CURRENT_FLAG', SCD_END: 'EFFECTIVE_TO',
                           SCD_START: 'EFFECTIVE_FROM', SCD_NK: 'PROD_ID'].findAll { key, value -> selector.contains(key) }
            assert matches.size() == 1
            names = matches.values().toList()
        }
        prefix + names.collect { format.replace('[COL_NAME]', it) }.join(separator) + suffix
    }
    String getColList(String prefix, String format, String separator, String suffix, String selector) {
        getTargetColList(prefix, format, separator, suffix, selector)
    }
}
String stage = 'read_input'
try {
    def input = new JsonSlurper().parse(System.in)
    def engine = new SimpleTemplateEngine()
    def render = { body, options ->
        engine.createTemplate(body).make([odiRef: new OfflineRef(columns: input.columns, options: options)]).toString()
    }
    stage = 'render_expiry'
    def expiry = render(input.expiry, input.options)
    stage = 'inspect_expiry'
    def matcher = expiry =~ /(?is)\bset\s*\((.*?)\)\s*=\s*\(\s*select(.*?)\bfrom\b/
    assert matcher.find()
    def assignments = matcher.group(1).replace('"', '').split(',').collect {
        it.trim().replaceFirst(/^[A-Za-z][A-Za-z0-9_]*\./, '')
    }
    def values = matcher.group(2).split(',').collect { it.trim() }
    stage = 'expiry_assignments'
    assert assignments == ['CURRENT_FLAG', 'EFFECTIVE_TO', 'UPDATE_DATE']
    stage = 'expiry_values'
    assert values.size() == 3 && values[-1] == 'SYSTIMESTAMP'
    stage = 'expiry_effective_reference'
    assert values[1].endsWith('EFFECTIVE_FROM')
    stage = 'expiry_no_clock'
    assert !expiry.contains('SYSDATE')
    stage = 'render_insert'
    def insert = render(input.insert, input.options)
    stage = 'inspect_insert'
    def projectionMatch = insert =~ /(?is)\bselect\b(.*?)\bfrom\b/
    assert projectionMatch.find()
    def projection = projectionMatch.group(1).split(',').collect { it.trim() }
    assert projection == input.expectedProjection
    assert !insert.contains('SYSDATE') && !insert.contains('SESS_NO')
    stage = 'render_population'
    def population = render(input.population, input.options)
    assert population.count('SYSDATE') == 1 && population.count('update ') == 1
    assert population.contains('"EFFECTIVE_FROM" = SYSDATE')
    assert population.contains("where IND_UPDATE = 'I'")
    assert population.count('__ODI68_UNVERIFIED_') == 2
    stage = 'render_options'
    render(input.optionsBody, input.options)
    int rejected = 0
    input.lockedNames.each { key ->
        def changed = new LinkedHashMap(input.options)
        changed[key] = 'untrusted'
        try { render(input.optionsBody, changed) }
        catch (Exception ignored) { rejected++ }
    }
    assert rejected == 9
    println JsonOutput.toJson([passed: true, rendered: 4, option_rejections: rejected,
                              expiry_columns: assignments.size(), projection_columns: projection.size()])
} catch (Throwable ignored) {
    println JsonOutput.toJson([passed: false, stage: stage])
}
'''


@pytest.mark.skipif(not (base._VENDOR.is_file() and _JAVA.is_file() and _GROOVY.is_file()),
                    reason="Optional offline template renderer unavailable")
def test_installed_new_templates_render_with_fake_reference_no_sdk_or_connection():
    candidate = audit.prepare_dim_product_scd2_ikm_audit_candidate(base._VENDOR.read_bytes()).candidate
    bodies = _texts(candidate.existing_objects_xml)
    payload = {
        "expiry": bodies["1318"], "insert": bodies["1307"], "optionsBody": bodies["1319"],
        "population": candidate.new_task.body.decode(), "columns": _COLUMNS,
        "options": {**dict(_LOCKS), "COMPATIBLE": "9"}, "lockedNames": [name for name, _ in _LOCKS],
        "expectedProjection": ["SYSTIMESTAMP" if c == "INSERT_DATE" else
                               "CAST(NULL AS TIMESTAMP)" if c == "UPDATE_DATE" else f'"{c}"' for c in _COLUMNS],
    }
    # Vendor text travels only through stdin memory; the child emits fixed metadata only.
    # Classpath deliberately contains Groovy, NOT odi-core or any repository/config helper.
    result = subprocess.run(
        [str(_JAVA), "-cp", str(_GROOVY), "groovy.ui.GroovyMain", "-e", _RENDER_CHECK],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60,
        env={"SYSTEMROOT": r"C:\Windows", "JAVA_TOOL_OPTIONS": ""},
    )
    exit_code = result.returncode
    assert exit_code == 0
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail("Offline renderer returned an invalid metadata envelope", pytrace=False)
    safe_shape = type(summary) is dict and set(summary) <= {
        "passed", "rendered", "option_rejections", "expiry_columns", "projection_columns", "stage",
    } and all(type(value) in (bool, int) or (type(value) is str and value in {
        "read_input", "render_expiry", "inspect_expiry", "expiry_assignments", "expiry_values",
        "expiry_effective_reference", "expiry_no_clock", "render_insert", "inspect_insert",
        "render_population", "render_options",
    }) for value in summary.values())
    if not safe_shape:
        pytest.fail("Offline renderer returned unexpected metadata", pytrace=False)
    assert summary == {"passed": True, "rendered": 4, "option_rejections": 9,
                       "expiry_columns": 3, "projection_columns": 26}


@pytest.mark.skipif(not (_JAVA.with_name("javap.exe").is_file() and _CORE.is_file()),
                    reason="Optional SDK bytecode inspection unavailable")
def test_sdk_duplication_has_mode_two_and_identity_remapping_not_custom_allocator():
    result = subprocess.run(
        [str(_JAVA.with_name("javap.exe")), "-classpath", str(_CORE), "-constants", "-c", "-p",
         "oracle.odi.impexp.IImportService", "com.sunopsis.dwg.DwgDuplicationTools",
         "com.sunopsis.dwg.DwgDuplicationMappingTable", "com.sunopsis.dwg.DwgObject"],
        capture_output=True, text=True, timeout=60,
        env={"SYSTEMROOT": r"C:\Windows", "JAVA_TOOL_OPTIONS": ""},
    )
    exit_code = result.returncode
    assert exit_code == 0
    for required in ("IMPORT_MODE_DUPLICATION = 2", "duplicationComputeNewPrimaryKey", "addPrimaryKeyMapping",
                     "setForeignKeyValue", "UUID.randomUUID", "internalSetGUID"):
        present = required in result.stdout
        assert present
    methods = re.split(r"\n(?=  (?:public|private|protected) )", result.stdout)
    import_method = next(m for m in methods if " importFromObjectLst(" in m.splitlines()[0])
    calls = re.findall(r"(\d+): invokevirtual[^\n]*DwgDuplicationTools.batchDuplication:", import_method)
    call_count = len(calls)
    assert call_count == 1
    call_offset = int(calls[0])
    mode_two_targets = re.findall(
        r"\d+: iload_1\s*\n\s*\d+: iconst_2\s*\n\s*\d+: if_icmpeq\s+(\d+)", import_method,
    )
    mode_two_entry = max(int(target) for target in mode_two_targets if int(target) < call_offset)
    checks = re.findall(
        r"(\d+): iload_1\s*\n\s*\d+: (iconst_\d|bipush\s+\d+)\s*\n"
        r"\s*\d+: if_icmpne\s+(\d+)", import_method,
    )
    chain = [(int(start), int(opcode[-1]) if opcode.startswith("iconst_") else int(opcode.split()[1]), int(end))
             for start, opcode, end in checks if mode_two_entry <= int(start) and int(end) < call_offset]
    # Mode 2 takes every != edge past the five other modes to batchDuplication.
    assert [mode for _, mode, _ in chain] == [1, 4, 3, 5, 6]
    assert all(left[2] == right[0] for left, right in zip(chain, chain[1:]))
    assert chain[-1][2] < call_offset


@pytest.mark.skipif(not base._VENDOR.is_file(), reason="Optional installed vendor XML unavailable")
def test_installed_candidate_safe_aggregate_assertions():
    source = base._VENDOR.read_bytes()
    result = audit.prepare_dim_product_scd2_ikm_audit_candidate(source)
    assert result.candidate is not None
    assert result.report["status"] == "blocked_pending_bindings_and_runtime_verification"
    assert result.report["existing_task_count"] == 31
    assert result.report["candidate_task_count"] == 32
    assert result.report["changed_existing_task_count"] == 7
    assert result.report["unchanged_object_count"] == 340
    assert result.report["existing_objects_sha256"] == "135C59DD377D47AF215E1EAFB2E343695AB45BEDCB9C9F3200F90376F1E59B74"
    assert result.report["new_task_sha256"] == "4EA591211060B89827325AC9F2638AA0CE26A209F2B6EF18E3651BF36477AEE9"
    assert _digest(base._VENDOR.read_bytes()) == _digest(source)
