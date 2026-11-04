"""WP-E2E-A: update key, internal sources, control append and run-option contracts.

All tests use mocks. No database, no ODI repository and no Groovy process.
"""

from __future__ import annotations

import pandas as pd
import pytest

import odi_orchestrator
import target_table_service
from mapping_generator import MappingGenerator
from odi_orchestrator import OdiOrchestrator


BASE_PARAMS = {
    "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
    "src_model": "ORACLE_HR",
    "src_table": "HR.EMPLOYEES",
    "tgt_model": "ORACLE_ODS",
    "tgt_owner": "DWH",
    "tgt_table": "ODS_HR_EMPLOYEES",
    "update_key_columns": ["EMPLOYEE_ID"],
}


@pytest.fixture(autouse=True)
def _configured_source_schemas(monkeypatch):
    import mapping_generator

    monkeypatch.setattr(mapping_generator, "SOURCE_ORACLE_SCHEMAS", ["HR", "SH"])
    monkeypatch.setattr(
        mapping_generator, "ODI_IKM_PRIORITY", "oracle merge,oracle update,oracle insert"
    )


class _CompletedProcess:
    def __init__(self, *, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# Item 1: classpath and JMX noise tolerance
# ---------------------------------------------------------------------------

_JMX_NOISE = "\n".join(
    [
        "Nov 04, 2026 8:36:35 PM oracle.as.jmx.framework.PortableMBeanFactory <init>",
        "SEVERE: Error initializing the JMX FRamework SPI",
        "java.lang.NoClassDefFoundError: javax/management/j2ee/statistics/Stats",
        "\tat java.lang.Class.forName0(Native Method)",
        "\tat java.lang.Class.forName(Class.java:348)",
        "Caused by: java.lang.ClassNotFoundException: javax.management.j2ee.statistics.Stats",
        "\tat java.net.URLClassLoader.findClass(URLClassLoader.java:382)",
        "\t... 12 more",
    ]
)
_SUCCESS = '{"tool":"odi.generate_scenario_metadata","status":"passed","side_effects":true}'
_ERROR = '{"tool":"odi.generate_scenario_metadata","status":"error","error_type":"X"}'


def test_classpath_includes_j2ee_jar_when_present(monkeypatch, tmp_path):
    jar = tmp_path / "javax.management.j2ee.jar"
    jar.write_text("x")
    monkeypatch.setattr(odi_orchestrator, "_J2EE_JAR", str(jar))

    assert str(jar) in OdiOrchestrator()._build_classpath().split(";")


def test_classpath_skips_j2ee_jar_silently_when_absent(monkeypatch, tmp_path):
    missing = tmp_path / "javax.management.j2ee.jar"
    monkeypatch.setattr(odi_orchestrator, "_J2EE_JAR", str(missing))

    classpath = OdiOrchestrator()._build_classpath()

    assert str(missing) not in classpath
    assert "odi-core.jar" in classpath


def test_jmx_noise_with_success_envelope_is_not_fatal():
    assert not odi_orchestrator._groovy_output_failed(f"{_JMX_NOISE}\n{_SUCCESS}")


def test_jmx_noise_lines_that_look_like_fatal_markers_are_ignored():
    noisy = _JMX_NOISE.replace(
        "java.lang.NoClassDefFoundError:", "ERROR: java.lang.NoClassDefFoundError:"
    )

    assert not odi_orchestrator._groovy_output_failed(f"{noisy}\n{_SUCCESS}")


def test_jmx_noise_never_hides_a_real_structured_error():
    assert odi_orchestrator._groovy_output_failed(f"{_JMX_NOISE}\n{_ERROR}")


def test_jmx_noise_never_hides_other_fatal_markers():
    assert odi_orchestrator._groovy_output_failed(f"{_JMX_NOISE}\nFATAL ERROR in thread")
    assert odi_orchestrator._groovy_output_failed(f"{_JMX_NOISE}\nHata: something")
    assert odi_orchestrator._groovy_output_failed(f"{_JMX_NOISE}\nLKM VERIFY FAILED for X")


def test_run_groovy_returns_success_stdout_when_stderr_has_only_jmx_noise(monkeypatch):
    monkeypatch.setattr(odi_orchestrator, "_JAVA_BIN", r"C:\Java\bin\java.exe")
    monkeypatch.setattr(odi_orchestrator.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(
        odi_orchestrator, "_ensure_local_props_file", lambda: r"D:\safe\x.properties"
    )
    monkeypatch.setattr(
        odi_orchestrator.subprocess,
        "run",
        lambda *_a, **_k: _CompletedProcess(stdout=_SUCCESS, stderr=_JMX_NOISE),
    )

    with odi_orchestrator.groovy_approval_context(True):
        result = OdiOrchestrator().run_groovy(
            "generate_scenario_metadata.groovy", {"odi.mapping": "MAP_X"}
        )

    assert result == _SUCCESS


# ---------------------------------------------------------------------------
# Item 2 (G1): target key DDL
# ---------------------------------------------------------------------------


def test_key_constraint_names_fit_thirty_characters_and_are_stable():
    short = target_table_service.key_constraint_name("ODS_X", ["ID"], "unique")
    long_a = target_table_service.key_constraint_name(
        "ODS_HR_EMPLOYEES", ["EMPLOYEE_ID"], "unique"
    )
    long_b = target_table_service.key_constraint_name(
        "ODS_HR_EMPLOYEES", ["EMPLOYEE_ID", "DEPARTMENT_ID"], "unique"
    )

    assert short == "UK_ODS_X_ID"
    assert len(long_a) <= 30 and long_a.startswith("UK_")
    assert long_a == target_table_service.key_constraint_name(
        "ODS_HR_EMPLOYEES", ["EMPLOYEE_ID"], "unique"
    )
    assert long_a != long_b
    assert target_table_service.key_constraint_name("ODS_X", ["ID"], "primary") == "PK_ODS_X"


def test_key_constraint_sql_for_unique_and_primary():
    unique_sql = target_table_service.key_constraint_sql("ODS_X", ["ID", "SEQ"], "unique")
    primary_sql = target_table_service.key_constraint_sql("ODS_X", ["ID"], "primary")

    assert unique_sql.strip().startswith("CONSTRAINT UK_ODS_X_ID")
    assert 'UNIQUE ("ID", "SEQ")' in unique_sql
    assert primary_sql.strip() == 'CONSTRAINT PK_ODS_X PRIMARY KEY ("ID")'


class _Generator:
    def __init__(self):
        self.logs = []
        self._batch_id = "B1"

    def _add_log(self, *args):
        self.logs.append(args)


def _plan(**extra):
    plan = {
        "src_columns": [{"COLUMN_NAME": "ID"}],
        "col_defs": ['    "ID"  NUMBER NOT NULL', '    "NAME"  VARCHAR2(10)'],
        "desired_col_defs": [("ID", "NUMBER NOT NULL"), ("NAME", "VARCHAR2(10)")],
        "defaults_added": [],
    }
    plan.update(extra)
    return plan


def test_create_target_includes_unique_constraint_in_ddl(monkeypatch):
    executed = []
    monkeypatch.setattr(target_table_service, "_target_exists", lambda _c: False)
    monkeypatch.setattr(
        target_table_service,
        "_create_target",
        lambda ctx, defs: executed.append(list(defs)) or "DDL",
    )
    monkeypatch.setattr(target_table_service, "_apply_grants", lambda _c: None)
    monkeypatch.setattr(target_table_service, "_write_creation_audit", lambda *_a: None)

    result = target_table_service.execute_target_table_plan(
        _Generator(),
        {"tgt_owner": "DWH", "tgt_table": "ODS_X"},
        _plan(unique_key=["ID"]),
        "DWH",
    )

    assert result["status"] == "CREATED"
    assert any('UNIQUE ("ID")' in item for item in executed[0])
    assert result["key"]["status"] == "CREATED"
    assert result["key"]["columns"] == ["ID"]


def test_create_target_without_key_keeps_previous_ddl(monkeypatch):
    executed = []
    monkeypatch.setattr(target_table_service, "_target_exists", lambda _c: False)
    monkeypatch.setattr(
        target_table_service,
        "_create_target",
        lambda ctx, defs: executed.append(list(defs)) or "DDL",
    )
    monkeypatch.setattr(target_table_service, "_apply_grants", lambda _c: None)
    monkeypatch.setattr(target_table_service, "_write_creation_audit", lambda *_a: None)

    result = target_table_service.execute_target_table_plan(
        _Generator(), {"tgt_owner": "DWH", "tgt_table": "ODS_X"}, _plan(), "DWH"
    )

    assert executed[0] == _plan()["col_defs"]
    assert result["key"]["status"] == "NONE"


def test_existing_target_blocks_missing_key_without_ddl(monkeypatch):
    monkeypatch.setattr(target_table_service, "_target_exists", lambda _c: True)
    monkeypatch.setattr(
        target_table_service, "_target_columns", lambda _c: pytest.fail("key check must block first")
    )
    monkeypatch.setattr(target_table_service, "_existing_key_constraints", lambda _c: [])

    result = target_table_service.execute_target_table_plan(
        _Generator(),
        {"tgt_owner": "DWH", "tgt_table": "ODS_X"},
        _plan(unique_key=["ID"]),
        "DWH",
    )

    assert result["status"] == "BLOCKED"
    assert result["code"] == "update_key_missing_on_existing_target"
    assert result["ddl"] == ""


def test_existing_target_with_matching_key_is_left_alone(monkeypatch):
    monkeypatch.setattr(target_table_service, "_target_exists", lambda _c: True)
    monkeypatch.setattr(
        target_table_service, "_target_columns", lambda _c: {"ID", "NAME"}
    )
    monkeypatch.setattr(
        target_table_service, "_existing_key_constraints", lambda _c: [
            {"name": "UK_X", "columns": ["ID"], "type": "U", "enabled": True, "validated": True}
        ]
    )

    result = target_table_service.execute_target_table_plan(
        _Generator(),
        {"tgt_owner": "DWH", "tgt_table": "ODS_X"},
        _plan(unique_key=["ID"]),
        "DWH",
    )

    assert result["status"] == "ALREADY_IN_SYNC"
    assert result["ddl"] == ""
    assert result["key"]["status"] == "ALREADY_PRESENT"


# ---------------------------------------------------------------------------
# Item 2 (G1): request validation
# ---------------------------------------------------------------------------


def _validate(**extra):
    return MappingGenerator().validate_params({**BASE_PARAMS, **extra})


def test_update_key_columns_are_normalized_and_deduplicated():
    validated = _validate(update_key_columns=[" employee_id ", "EMPLOYEE_ID", "dept_id"])

    assert validated["update_key_columns"] == ["EMPLOYEE_ID", "DEPT_ID"]
    assert validated["update_key_kind"] == "unique"


def test_update_key_columns_accept_comma_string():
    assert _validate(update_key_columns="a,b")["update_key_columns"] == ["A", "B"]


@pytest.mark.parametrize("bad", [["bad name"], ["1ABC"], [""], "x;drop", [123], {"a": 1}])
def test_update_key_columns_reject_invalid_identifiers(bad):
    with pytest.raises(ValueError):
        _validate(update_key_columns=bad)


def test_update_key_kind_is_restricted():
    assert _validate(update_key_columns=["ID"], update_key_kind="PRIMARY")["update_key_kind"] == "primary"
    with pytest.raises(ValueError):
        _validate(update_key_columns=["ID"], update_key_kind="clustered")


def test_default_iu_resolves_exact_merge_with_requested_key():
    validated = _validate()

    assert validated["update_key_columns"] == ["EMPLOYEE_ID"]
    assert validated["integration_type"] == "IU"
    assert validated["ikm_name"] == "IKM Oracle Merge"


def test_merge_ikm_without_update_key_fails_closed():
    with pytest.raises(ValueError, match="update key"):
        _validate(ikm_name="IKM Oracle Merge", update_key_columns=[])


def test_merge_ikm_with_update_key_is_accepted():
    validated = _validate(ikm_name="IKM Oracle Merge", update_key_columns=["EMPLOYEE_ID"])

    assert validated["ikm_name"] == "IKM Oracle Merge"


# ---------------------------------------------------------------------------
# Item 4 (G4): integration type and IKM allowlist
# ---------------------------------------------------------------------------


def test_integration_type_is_restricted_to_two_values():
    assert _validate(integration_type="control_append", ikm_name="IKM Oracle Insert")[
        "integration_type"
    ] == "CONTROL_APPEND"
    with pytest.raises(ValueError):
        _validate(integration_type="SCD")


def test_ikm_name_must_match_configured_allowlist(monkeypatch):
    import mapping_generator

    monkeypatch.setattr(mapping_generator, "ODI_APPROVED_IKMS", "IKM Oracle Merge,IKM Oracle Insert")

    assert _validate(ikm_name="IKM Oracle Insert")["ikm_name"] == "IKM Oracle Insert"
    with pytest.raises(ValueError, match="ikm_name"):
        _validate(ikm_name="IKM Evil Custom")


@pytest.mark.parametrize("bad", ["IKM Oracle Insert; DROP", "IKM Oracle Insert\nX", "a" * 200])
def test_ikm_name_shape_is_checked_before_allowlist(bad):
    with pytest.raises(ValueError):
        _validate(ikm_name=bad)


def test_control_append_rejects_merge_or_update_ikm():
    with pytest.raises(ValueError, match="CONTROL_APPEND"):
        _validate(integration_type="CONTROL_APPEND", ikm_name="IKM Oracle Merge")


def test_control_append_defaults_to_insert_ikm_and_needs_no_key():
    validated = _validate(integration_type="CONTROL_APPEND", update_key_columns=[])

    assert validated["integration_type"] == "CONTROL_APPEND"
    assert "insert" in validated["ikm_name"].lower()


# ---------------------------------------------------------------------------
# Item 2 / 4: plan and ODI call wiring
# ---------------------------------------------------------------------------


def _source_columns(*names):
    return [
        {
            "COLUMN_NAME": name,
            "ORACLE_TYPE": "NUMBER",
            "DATA_TYPE": "NUMBER",
            "IS_NULLABLE": "NO",
        }
        for name in names
    ]


def test_plan_carries_unique_key_when_columns_exist(monkeypatch):
    generator = MappingGenerator()
    monkeypatch.setattr(
        generator, "_get_source_columns_oracle", lambda _t: _source_columns("EMPLOYEE_ID", "X")
    )
    validated = generator.validate_params({**BASE_PARAMS, "update_key_columns": ["employee_id"]})

    plan = generator._build_target_column_plan(validated)

    assert plan["unique_key"] == ["EMPLOYEE_ID"]
    assert plan["primary_key"] is None


def test_plan_carries_primary_key_when_requested(monkeypatch):
    generator = MappingGenerator()
    monkeypatch.setattr(
        generator, "_get_source_columns_oracle", lambda _t: _source_columns("EMPLOYEE_ID")
    )
    validated = generator.validate_params(
        {**BASE_PARAMS, "update_key_columns": ["EMPLOYEE_ID"], "update_key_kind": "primary"}
    )

    plan = generator._build_target_column_plan(validated)

    assert plan["primary_key"] == ["EMPLOYEE_ID"]
    assert plan["unique_key"] is None


def test_plan_rejects_key_column_missing_from_target(monkeypatch):
    generator = MappingGenerator()
    monkeypatch.setattr(
        generator, "_get_source_columns_oracle", lambda _t: _source_columns("EMPLOYEE_ID")
    )
    validated = generator.validate_params({**BASE_PARAMS, "update_key_columns": ["NOPE"]})

    with pytest.raises(ValueError, match="NOPE"):
        generator._build_target_column_plan(validated)


def test_plan_without_key_has_no_key_entries(monkeypatch):
    generator = MappingGenerator()
    monkeypatch.setattr(
        generator, "_get_source_columns_oracle", lambda _t: _source_columns("EMPLOYEE_ID")
    )

    plan = generator._build_target_column_plan(generator.validate_params(
        dict(BASE_PARAMS, integration_type="CONTROL_APPEND", update_key_columns=[])
    ))

    assert plan["unique_key"] is None and plan["primary_key"] is None


def _capture_generate_mapping(monkeypatch):
    captured = {}

    def fake(self, **kwargs):
        captured.update(kwargs)
        return "SUCCESS: mapping created"

    monkeypatch.setattr(OdiOrchestrator, "generate_mapping", fake)
    return captured


def _prepared_generator(monkeypatch, **extra):
    generator = MappingGenerator()
    validated = generator.validate_params({**BASE_PARAMS, **extra})
    validated.update(
        {
            "_odi_repository": {"mapping_exists": False},
            "_odi_src_model_code": "ORACLE_HR",
            "_odi_tgt_model_code": "ORACLE_ODS",
            "_odi_src_datastore_name": "EMPLOYEES",
            "_odi_tgt_datastore_name": "ODS_HR_EMPLOYEES",
        }
    )
    monkeypatch.setattr(generator, "_ensure_odi_repository_bindings", lambda p: p)
    return generator, validated


def test_create_mapping_passes_required_update_key_and_integration(monkeypatch):
    captured = _capture_generate_mapping(monkeypatch)
    generator, validated = _prepared_generator(
        monkeypatch, update_key_columns=["EMPLOYEE_ID"], ikm_name="IKM Oracle Merge"
    )

    generator._create_odi_mapping(validated)

    assert captured["required_update_key_columns"] == ["EMPLOYEE_ID"]
    assert captured["required_update_key_type"] == "ALTERNATE_KEY"
    assert captured["ikm_name"] == "IKM Oracle Merge"
    assert captured["integration_type"] == "IU"


def test_create_mapping_control_append_does_not_require_update_key(monkeypatch):
    captured = _capture_generate_mapping(monkeypatch)
    generator, validated = _prepared_generator(
        monkeypatch, integration_type="CONTROL_APPEND", update_key_columns=["EMPLOYEE_ID"]
    )

    generator._create_odi_mapping(validated)

    assert captured["integration_type"] == "CONTROL_APPEND"
    assert not captured.get("required_update_key_columns")
    assert "insert" in captured["ikm_name"].lower()


def test_create_mapping_transports_exact_default_ikm_and_key(monkeypatch):
    captured = _capture_generate_mapping(monkeypatch)
    generator, validated = _prepared_generator(monkeypatch)

    generator._create_odi_mapping(validated)

    assert captured["required_update_key_columns"] == ["EMPLOYEE_ID"]
    assert captured["ikm_name"] == "IKM Oracle Merge"
    assert captured["integration_type"] == "IU"


def test_orchestrator_transports_integration_type_and_ikm(monkeypatch):
    calls = []
    monkeypatch.setattr(
        OdiOrchestrator,
        "run_groovy",
        lambda _self, script, params: calls.append((script, params)) or "SUCCESS",
    )

    OdiOrchestrator().generate_mapping(
        "MAP_X",
        "ORACLE_SH",
        "SALES",
        "ORACLE_ODS",
        "ODS_SH_SALES",
        ikm_name="IKM Oracle Insert",
        integration_type="CONTROL_APPEND",
    )

    params = calls[0][1]
    assert params["odi.integration_type"] == "CONTROL_APPEND"
    assert params["odi.ikm_name"] == "IKM Oracle Insert"
    assert "odi.ikm_priority" not in params


def test_orchestrator_omits_integration_type_when_not_requested(monkeypatch):
    calls = []
    monkeypatch.setattr(
        OdiOrchestrator,
        "run_groovy",
        lambda _self, script, params: calls.append((script, params)) or "SUCCESS",
    )

    OdiOrchestrator().generate_mapping("MAP_X", "A", "B", "C", "D")

    assert "odi.integration_type" not in calls[0][1]


def test_register_datastore_transports_key_parameters(monkeypatch):
    calls = []
    monkeypatch.setattr(
        OdiOrchestrator,
        "run_groovy",
        lambda _self, script, params: calls.append((script, params)) or "SUCCESS",
    )

    OdiOrchestrator().register_datastore(
        "ORACLE_ODS",
        "ODS_X",
        [{"name": "ID", "type": "NUMBER"}],
        key_columns=["id"],
        key_name="UK_ODS_X_ID",
        key_type="ALTERNATE_KEY",
        key_only=True,
    )

    params = calls[0][1]
    assert params["odi.key_columns"] == "ID"
    assert params["odi.key_name"] == "UK_ODS_X_ID"
    assert params["odi.key_type"] == "ALTERNATE_KEY"
    assert params["odi.key_only"] == "true"


def test_register_datastore_without_key_sends_no_key_parameters(monkeypatch):
    calls = []
    monkeypatch.setattr(
        OdiOrchestrator,
        "run_groovy",
        lambda _self, script, params: calls.append((script, params)) or "SUCCESS",
    )

    OdiOrchestrator().register_datastore("ORACLE_ODS", "ODS_X", [{"name": "ID"}])

    assert not any(key.startswith("odi.key_") for key in calls[0][1])


def _register_generator(monkeypatch, *, tgt_exists, register_output):
    generator, validated = _prepared_generator(
        monkeypatch, update_key_columns=["EMPLOYEE_ID"]
    )
    validated["_odi_src_datastore_exists"] = True
    validated["_odi_tgt_datastore_exists"] = tgt_exists
    monkeypatch.setattr(
        generator, "_get_source_columns_oracle", lambda _t: _source_columns("EMPLOYEE_ID")
    )
    calls = []

    def fake_register(self, **kwargs):
        calls.append(kwargs)
        return register_output

    monkeypatch.setattr(OdiOrchestrator, "register_datastore", fake_register)
    return generator, validated, calls


_KEY_OK = '{"tool":"odi.register_datastore","status":"passed","key":{"created":true}}'


def test_new_target_datastore_is_registered_with_key(monkeypatch):
    generator, validated, calls = _register_generator(
        monkeypatch, tgt_exists=False, register_output=_KEY_OK
    )

    generator._register_odi_datastores(validated)

    assert len(calls) == 1
    assert calls[0]["key_columns"] == ["EMPLOYEE_ID"]
    assert calls[0]["key_type"] == "ALTERNATE_KEY"
    assert calls[0]["key_name"].startswith("UK_")
    assert not calls[0].get("key_only")


def test_existing_target_datastore_gets_key_only_registration(monkeypatch):
    generator, validated, calls = _register_generator(
        monkeypatch, tgt_exists=True, register_output=_KEY_OK
    )

    generator._register_odi_datastores(validated)

    assert len(calls) == 1
    assert calls[0]["key_only"] is True
    assert calls[0]["key_columns"] == ["EMPLOYEE_ID"]


def test_key_registration_failure_is_not_swallowed(monkeypatch):
    generator, validated, _calls = _register_generator(
        monkeypatch,
        tgt_exists=False,
        register_output='{"status":"error","action":"register_datastore"}',
    )

    with pytest.raises(RuntimeError, match="key"):
        generator._register_odi_datastores(validated)


def test_primary_kind_registers_primary_key_type(monkeypatch):
    generator, validated = _prepared_generator(
        monkeypatch, update_key_columns=["EMPLOYEE_ID"], update_key_kind="primary"
    )
    validated["_odi_src_datastore_exists"] = True
    validated["_odi_tgt_datastore_exists"] = False
    monkeypatch.setattr(
        generator, "_get_source_columns_oracle", lambda _t: _source_columns("EMPLOYEE_ID")
    )
    calls = []
    monkeypatch.setattr(
        OdiOrchestrator,
        "register_datastore",
        lambda self, **kw: calls.append(kw) or _KEY_OK,
    )

    generator._register_odi_datastores(validated)

    assert calls[0]["key_type"] == "PRIMARY_KEY"
    assert calls[0]["key_name"].startswith("PK_")


# ---------------------------------------------------------------------------
# Item 3 (G2): internal sources are read through the target connector
# ---------------------------------------------------------------------------


def _schema_frame():
    return pd.DataFrame(
        {
            "COLUMN_NAME": ["PROD_ID"],
            "DATA_TYPE": ["NUMBER"],
            "DATA_LENGTH": [22],
            "DATA_PRECISION": [6],
            "DATA_SCALE": [0],
            "NULLABLE": ["N"],
            "FULL_DATA_TYPE": ["NUMBER(6)"],
            "COMMENTS": [None],
        }
    )


def test_internal_source_reads_through_target_service_with_explicit_owner(monkeypatch):
    seen = []
    monkeypatch.setattr(
        target_table_service,
        "read_table_schema",
        lambda owner, table: seen.append((owner, table)) or _schema_frame(),
    )
    generator = MappingGenerator()
    monkeypatch.setattr(
        generator,
        "_get_source_columns_oracle",
        lambda _t: pytest.fail("source connector must not be used for internal sources"),
    )
    validated = generator.validate_params(
        {
            "mapping_name": "MAP_ODS_PRODUCTS_TO_DWH_PRODUCT_T1",
            "src_model": "ORACLE_ODS",
            "src_table": "ODS_SH_PRODUCTS",
            "tgt_model": "ORACLE_DWH",
            "tgt_owner": "DWH",
            "tgt_table": "DWH_PRODUCT_T1",
            "update_key_columns": ["PROD_ID"],
        }
    )

    columns = generator._source_columns_for(validated)

    assert seen == [("DWH", "ODS_SH_PRODUCTS")]
    assert columns[0]["COLUMN_NAME"] == "PROD_ID"
    assert columns[0]["ORACLE_TYPE"].startswith("NUMBER")


def test_internal_source_uses_qualified_owner_when_allowed(monkeypatch):
    seen = []
    monkeypatch.setattr(
        target_table_service,
        "read_table_schema",
        lambda owner, table: seen.append((owner, table)) or _schema_frame(),
    )
    generator = MappingGenerator()
    validated = generator.validate_params(
        {
            "mapping_name": "MAP_X",
            "src_model": "ORACLE_ODS",
            "src_table": "ods.ODS_SH_PRODUCTS",
            "tgt_model": "ORACLE_DWH",
            "tgt_owner": "DWH",
            "tgt_table": "DWH_X",
            "update_key_columns": ["PROD_ID"],
        }
    )

    generator._source_columns_for(validated)

    assert seen == [("ODS", "ODS_SH_PRODUCTS")]


def test_internal_source_rejects_owner_outside_allowlist():
    with pytest.raises(ValueError, match="src_owner"):
        MappingGenerator().validate_params(
            {
                "mapping_name": "MAP_X",
                "src_model": "ORACLE_ODS",
                "src_table": "HR.ODS_SH_PRODUCTS",
                "tgt_model": "ORACLE_DWH",
                "tgt_owner": "DWH",
                "tgt_table": "DWH_X",
            }
        )


def test_external_source_still_uses_source_connector(monkeypatch):
    generator = MappingGenerator()
    monkeypatch.setattr(
        target_table_service,
        "read_table_schema",
        lambda *_a: pytest.fail("target service must not be used for external sources"),
    )
    monkeypatch.setattr(
        generator, "_get_source_columns_oracle", lambda t: _source_columns("EMPLOYEE_ID")
    )
    validated = generator.validate_params(dict(BASE_PARAMS))

    assert generator._source_columns_for(validated)[0]["COLUMN_NAME"] == "EMPLOYEE_ID"


class _FakeCursor:
    def __init__(self):
        self.executed = []
        self.description = [(n,) for n in (
            "COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION",
            "DATA_SCALE", "NULLABLE", "COMMENTS",
        )]

    def execute(self, sql, binds=None):
        self.executed.append((sql, binds))

    def fetchall(self):
        return [("PROD_ID", "NUMBER", 22, 6, 0, "N", None)]

    def close(self):
        pass


class _FakeConnection:
    def __init__(self):
        self.cursor_obj = _FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def close(self):
        pass


def test_read_table_schema_binds_owner_and_matches_connector_contract(monkeypatch):
    connection = _FakeConnection()
    monkeypatch.setattr(target_table_service, "_connection", lambda: connection)

    frame = target_table_service.read_table_schema("dwh", "ods_sh_products")

    sql, binds = connection.cursor_obj.executed[0]
    assert "ALL_TAB_COLUMNS" in sql
    assert binds == {"o": "DWH", "t": "ODS_SH_PRODUCTS"}
    assert list(frame.columns) == [
        "COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION",
        "DATA_SCALE", "NULLABLE", "FULL_DATA_TYPE", "COMMENTS",
    ]
    assert frame.loc[0, "FULL_DATA_TYPE"] == "NUMBER(6)"


def test_read_table_schema_rejects_invalid_identifiers():
    with pytest.raises(ValueError):
        target_table_service.read_table_schema("DWH;DROP", "T")


# ---------------------------------------------------------------------------
# Item 4 (G4): run returns ikm_option_unsupported instead of a Groovy throw
# ---------------------------------------------------------------------------


class _FakeApp:
    def redact_sensitive_text(self, text, max_len=0):
        return str(text)[:max_len] if max_len else str(text)

    class MappingStateManager:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def upsert_state(self, *_a, **_k):
            return None

    class AlertManager:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def create_alert(self, **_k):
            return None

    class logger:
        @staticmethod
        def error(*_a, **_k):
            return None


_UNSUPPORTED = (
    '{"status":"ikm_option_unsupported","action":"update_mapping",'
    '"option":"TRUNCATE_TARGET_TABLE"}'
)


def test_run_reports_ikm_option_unsupported_with_conflict_status(monkeypatch):
    from mapping_run_service import MappingRunOptions, execute_mapping_run

    calls = []
    monkeypatch.setattr(
        OdiOrchestrator, "update_mapping", lambda self, **kw: _UNSUPPORTED
    )
    monkeypatch.setattr(
        OdiOrchestrator,
        "generate_scenario",
        lambda self, *a, **k: calls.append("generate") or "SUCCESS",
    )
    monkeypatch.setattr(
        OdiOrchestrator, "run_scenario", lambda self, *a, **k: calls.append("run") or "SUCCESS"
    )

    outcome = execute_mapping_run(
        _FakeApp(), MappingRunOptions("MAP_X", "001", "GLOBAL", True)
    )

    assert outcome.status_code == 409
    assert outcome.payload["status"] == "ikm_option_unsupported"
    assert outcome.payload["option"] == "TRUNCATE_TARGET_TABLE"
    assert calls == []


def test_run_continues_when_option_update_succeeds(monkeypatch):
    from mapping_run_service import MappingRunOptions, execute_mapping_run

    monkeypatch.setattr(OdiOrchestrator, "update_mapping", lambda self, **kw: "SUCCESS: ok")
    monkeypatch.setattr(OdiOrchestrator, "generate_scenario", lambda self, *a, **k: "SUCCESS")
    monkeypatch.setattr(OdiOrchestrator, "run_scenario", lambda self, *a, **k: "SUCCESS")

    outcome = execute_mapping_run(
        _FakeApp(), MappingRunOptions("MAP_X", "001", "GLOBAL", False)
    )

    assert outcome.status_code == 200
    assert outcome.payload["status"] == "SUCCESS"
