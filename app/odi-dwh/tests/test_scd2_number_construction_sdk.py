"""Real SDK construction fragments only: no application imports or OdiInstance."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "odi_automation"
FIXED = "dim_product_scd2_datastore_register.groovy"
GENERIC = "scd2_datastore_register.groovy"
CLASSIC = "register_datastore.groovy"
NUMBERS = {
    "PRODUCT_SK": (None, None),
    "PROD_ID": (6, None),
    "PROD_SUBCATEGORY_ID": (None, None),
    "PROD_CATEGORY_ID": (None, None),
    "PROD_WEIGHT_CLASS": (3, None),
    "SUPPLIER_ID": (6, None),
    "PROD_LIST_PRICE": (8, 2),
    "PROD_MIN_PRICE": (8, 2),
    "PROD_TOTAL_ID": (None, None),
    "ETL_BATCH_ID": (None, None),
}
GENERIC_CASES = [
    ("NUMBER", None, None),
    ("NUMBER_1", 1, None),
    ("NUMBER_6", 6, None),
    ("NUMBER_38", 38, None),
    ("NUMBER_8_2", 8, 2),
    ("NUMBER_38_0", 38, 0),
    ("NUMBER_38_38", 38, 38),
    ("NUMBER_4_5", 4, 5),
]
CLASSIC_CASES = [
    ("BARE:NUMBER", None, None),
    ("LOWER:number", None, None),
    ("ONE:NUMBER:1", 1, None),
    ("SIX:NUMBER:6", 6, None),
    ("MAXIMUM:NUMBER:38", 38, None),
    ("DECIMAL:NUMBER:8:2", 8, 2),
    # Classic intentionally keeps its existing >0-only scale setter behavior.
    ("ZERO_SCALE:NUMBER:38:0", 38, None),
    ("MAX_SCALE:NUMBER:38:38", 38, 38),
    ("LARGE_SCALE:NUMBER:4:5", 4, 5),
    ("NEGATIVE_SCALE:NUMBER:8:-2", 8, None),
    # Explicit zero length is not a bare declaration and is outside this fix.
    ("ZERO_LENGTH:NUMBER:0", 50, None),
    ("TEXT:VARCHAR2:20", 20, None),
    ("DEFAULT_TEXT:VARCHAR2", 50, None),
]


def fragment(source, start, end):
    assert source.count(start) == source.count(end) == 1
    result = source.split(start, 1)[1].split(end, 1)[0]
    result = start + result
    for forbidden in ("OdiInstance", "FileInputStream", "txnMgr", "em.", "System.getProperty"):
        assert forbidden not in result
    return result


@pytest.fixture(scope="module")
def sdk():
    home = Path(os.getenv("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home"))
    java = shutil.which("java")
    jars = [
        home / "odi/sdk/lib/odi-core.jar",
        home / "oracle_common/modules/thirdparty/groovy-all-2.4.17.jar",
        home / "oracle_common/modules/oracle.toplink/eclipselink.jar",
    ]
    if java is None or not all(path.is_file() for path in jars):
        pytest.skip("Local Java/ODI SDK/Groovy unavailable; nothing is downloaded")
    return java, os.pathsep.join([str(home / "odi/sdk/lib/*"), *map(str, jars[1:])])


HARNESS = r'''
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import oracle.odi.domain.model.*
import oracle.odi.domain.topology.*
import com.sunopsis.tools.core.SnpsStringTools

class OfflineOracle extends OdiTechnology {
    OfflineOracle() { super('Oracle', 'ORACLE') }
}
def spec = new JsonSlurper().parse(System.in)
def technology = new OfflineOracle()
// NUMBER patterns match the installed Oracle technology definition.
[NUMBER:'NUMBER(%L,%P)', VARCHAR2:'VARCHAR2(%L)', DATE:'DATE',
 TIMESTAMP:'TIMESTAMP(%L)'].each { code, pattern ->
    def type = new OdiDataType(technology, code)
    type.setCode(code)
    type.setIconLetter(code == 'NUMBER' ? 'N' : code == 'VARCHAR2' ? 'A' : 'D')
    type.setDdlPattern(pattern)
    type.setWritablePattern(pattern)
}
def model = new OdiModel(new OdiLogicalSchema(technology, 'OFFLINE'), 'Offline', 'OFFLINE')
def target = new OdiDataStore(model, 'OFFLINE')
def binding = new Binding([target:target, ds:target,
    colDefs:(spec.classic_columns ?: []) as String[], types:spec.types,
    nullable:spec.types.collectEntries { name, type -> [(name):'N'] },
    roleOf:{ String name -> null }])
String imports = 'import oracle.odi.domain.model.OdiColumn\n'
new GroovyShell(this.class.classLoader, binding).evaluate(imports + spec.fragment)
def rows = target.getColumns().collectEntries { column ->
    [(column.getName()):[
        length:column.getLength(), scale:column.getScale(),
        raw_length:column.getRawLength(), raw_scale:column.getRawScale(),
        data_format:column.getDataFormat(), ddl:column.getDdlDataType(),
        writable:SnpsStringTools.getDdlDataType(column.getDataType().getWritablePattern(),
            column.getLength(), column.getScale(), true),
        mandatory:column.isMandatory(), role:column.getScdType()?.name(),
        position:column.getPosition(),
    ]]
}
def fresh = new OdiColumn(target, 'SDK_DEFAULT')
fresh.setDataTypeCode('NUMBER')
fresh.setDataFormat('NUMBER')
println JsonOutput.toJson([rows:rows, constructor_ddl:fresh.getDdlDataType()])
'''


@pytest.fixture(scope="module", params=[FIXED, GENERIC])
def constructed(request, sdk):
    source = (ROOT / request.param).read_text(encoding="utf-8")
    types = {f"N{index}": case[0] for index, case in enumerate(GENERIC_CASES)}
    types.update(TEXT_BYTE="VARCHAR2_20_BYTE", TEXT_CHAR="VARCHAR2_20_CHAR")
    if request.param == FIXED:
        code = fragment(source, "final List<Map<String, Object>> COLUMN_CONTRACT = [", "def odi = null")
        code += fragment(source, "Map<String, OdiColumn> columnsByName = [:]", "OdiKey primaryKey =")
    else:
        code = fragment(source, "final java.util.regex.Pattern TYPE =", "final Map<String, String> FIXED_ROLES =")
        code += fragment(source, "Map<String, OdiColumn> columnsByName = [:]", '([skName, "EFFECTIVE_FROM"')
    java, classpath = sdk
    completed = subprocess.run(
        [java, "-cp", classpath, "groovy.ui.GroovyMain", "-e", HARNESS],
        input=json.dumps({"fragment": code, "types": types}),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return request.param, json.loads(completed.stdout)


def assert_number(row, length, scale, *, check_format=True):
    expected = "NUMBER"
    if length is not None:
        expected += f"({length}" + (f",{scale}" if scale is not None else "") + ")"
    assert row["ddl"] == expected
    assert row["writable"] == expected
    if check_format:
        assert row["data_format"] == expected
    assert (row["length"], row["scale"]) == (length, scale)
    assert (row["raw_length"], row["raw_scale"]) == (
        (0, -127) if length is None else (length, scale)
    )


def test_sdk_constructor_default_is_not_changed_by_data_format(constructed):
    _, result = constructed
    assert result["constructor_ddl"] == "NUMBER(50)"


def test_unconstrained_number_construction(constructed):
    name, result = constructed
    names = [key for key, value in NUMBERS.items() if value == (None, None)] if name == FIXED else ["N0"]
    assert len(names) == (5 if name == FIXED else 1)
    for column in names:
        assert_number(result["rows"][column], None, None)


def test_bounded_number_precision_and_scale_are_preserved(constructed):
    name, result = constructed
    expected = NUMBERS if name == FIXED else {
        f"N{index}": case[1:] for index, case in enumerate(GENERIC_CASES)
    }
    for column, (length, scale) in expected.items():
        if length is not None:
            assert_number(result["rows"][column], length, scale)


def test_other_column_metadata_is_preserved(constructed):
    name, result = constructed
    rows = result["rows"]
    assert sorted(row["position"] for row in rows.values()) == list(range(1, len(rows) + 1))
    if name == FIXED:
        assert len(rows) == 26
        assert rows["PRODUCT_SK"]["role"] == "SURROGATE_KEY"
        assert rows["PROD_ID"]["role"] == "NATURAL_KEY"
        assert rows["PROD_CATEGORY_ID"]["role"] == "ADD_ROW_ON_CHANGE"
        assert rows["ETL_BATCH_ID"]["role"] is None
        assert rows["PRODUCT_SK"]["mandatory"] is True
        assert rows["UPDATE_DATE"]["mandatory"] is False
        assert rows["PROD_NAME"]["length"] == 50
        assert rows["EFFECTIVE_FROM"]["ddl"] == "DATE"
    else:
        for suffix in ("BYTE", "CHAR"):
            row = rows[f"TEXT_{suffix}"]
            assert row["length"] == 20
            assert row["data_format"] == f"VARCHAR2(20 {suffix})"


@pytest.fixture(scope="module")
def classic_constructed(sdk):
    source = (ROOT / CLASSIC).read_text(encoding="utf-8")
    code = fragment(source, "int pos = 1", 'println "STEP 4: ${colDefs.length}')
    java, classpath = sdk
    completed = subprocess.run(
        [java, "-cp", classpath, "groovy.ui.GroovyMain", "-e", HARNESS],
        input=json.dumps({"fragment": code, "types": {},
                          "classic_columns": [case[0] for case in CLASSIC_CASES]}),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout)["rows"]


@pytest.mark.parametrize("declaration,length,scale", CLASSIC_CASES)
def test_classic_number_construction_and_unchanged_bounded_metadata(
    classic_constructed, declaration, length, scale,
):
    name, type_code, *_ = declaration.split(":")
    row = classic_constructed[name]
    if type_code.upper() == "NUMBER":
        assert_number(row, length, scale, check_format=False)
    else:
        assert (row["length"], row["scale"]) == (length, scale)
        assert row["ddl"] == row["writable"] == f"VARCHAR2({length})"
    assert row["data_format"] is None  # Classic does not set a separate data format.
    assert row["position"] == [case[0] for case in CLASSIC_CASES].index(declaration) + 1


@pytest.mark.parametrize("name", [FIXED, GENERIC, CLASSIC])
def test_complete_registrar_compiles_without_running_body(sdk, tmp_path, name):
    java, classpath = sdk
    completed = subprocess.run(
        [java, "-cp", classpath, "org.codehaus.groovy.tools.FileSystemCompiler",
         "-d", str(tmp_path), str(ROOT / name)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]


READBACK_TYPES = {
    "N_BARE": "NUMBER", "N_6": "NUMBER_6", "D": "DATE",
    "TS0": "TIMESTAMP_0", "TS3": "TIMESTAMP_3", "TS6": "TIMESTAMP_6", "TS_DEFAULT": "TIMESTAMP",
}
READBACK_HARNESS = HARNESS.replace(
    "new GroovyShell(this.class.classLoader, binding).evaluate(imports + spec.fragment)",
    """def failure = null
try {
    new GroovyShell(this.class.classLoader, binding).evaluate(imports + spec.fragment)
} catch (IllegalStateException e) {
    failure = e.getMessage()
}""",
).replace(
    "println JsonOutput.toJson([rows:rows, constructor_ddl:fresh.getDdlDataType()])",
    "println JsonOutput.toJson([failure:failure, rows:rows])",
)


def run_readback(sdk, mutate):
    """Execute the registrar's real registration loop and read-back block in memory."""
    source = (ROOT / GENERIC).read_text(encoding="utf-8")
    code = fragment(source, "final java.util.regex.Pattern TYPE =", "final Map<String, String> FIXED_ROLES =")
    code += fragment(source, "Map<String, OdiColumn> columnsByName = [:]", '([skName, "EFFECTIVE_FROM"')
    code += "".join(f'columnsByName["{n}"].setLength({v})\n' for n, v in mutate.items())
    code += "List<OdiColumn> ordered = target.getColumns().toList().sort { l, r -> l.getPosition() <=> r.getPosition() }\n"
    code += fragment(source, "ordered.each { column ->", "if (!primaryKey.isPK()")
    java, classpath = sdk
    completed = subprocess.run(
        [java, "-cp", classpath, "groovy.ui.GroovyMain", "-e", READBACK_HARNESS],
        input=json.dumps({"fragment": code, "types": READBACK_TYPES}),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout)


def test_registrar_readback_passes_for_correct_lengths(sdk):
    result = run_readback(sdk, {})
    assert result["failure"] is None
    rows = result["rows"]
    assert [rows[n]["length"] for n in ("TS0", "TS3", "TS6", "TS_DEFAULT")] == [0, 3, 6, 6]
    assert rows["N_BARE"]["length"] in (None, 0)


@pytest.mark.parametrize(
    "mutate",
    [{"TS6": 50}, {"TS3": 50}, {"TS0": 10}, {"TS_DEFAULT": 3}, {"N_6": 39}, {"N_BARE": 5}],
)
def test_registrar_readback_fails_structurally_for_wrong_lengths(sdk, mutate):
    assert run_readback(sdk, mutate)["failure"] == "ColumnMetadataMismatch"
