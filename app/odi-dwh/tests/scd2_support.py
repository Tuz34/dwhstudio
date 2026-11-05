"""Shared fakes for the SCD2 tests (no live Oracle or ODI)."""

from __future__ import annotations

import json
import re

import oracledb

import execution_identity
import scd2_builder
from scd2_service import Scd2Services

SOURCE_COLUMNS = [
    {"name": "CUST_ID", "data_type": "NUMBER(10)", "nullable": False},
    {"name": "CUST_NAME", "data_type": "VARCHAR2(50)", "nullable": False},
    {"name": "CUST_CITY", "data_type": "VARCHAR2(30)", "nullable": True},
    {"name": "CREDIT_LIMIT", "data_type": "NUMBER(8,2)", "nullable": True},
]

REQUEST = {
    "source_owner": "DWH",
    "source_table": "ODS_CUSTOMERS",
    "natural_key": ["CUST_ID"],
    "tracked_columns": ["CUST_NAME", "CUST_CITY"],
    "target_owner": "DM",
    "target_table": "DIM_CUSTOMER",
    "mapping_name": "MAP_ODS_CUST_TO_DIM_CUST",
}


def catalog_row(name: str, data_type: str, nullable: bool):
    match = re.fullmatch(r"([A-Z0-9]+)(?:\((\d+)(?:,(\d+)| (CHAR|BYTE))?\))?", data_type)
    base, first, second, semantics = match.groups()
    flag = "Y" if nullable else "N"
    if base in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"}:
        used = "C" if semantics == "CHAR" or base in {"NVARCHAR2", "NCHAR"} else "B"
        return (name, base, int(first), None, None, flag, int(first), used)
    if base == "NUMBER":
        scale = int(second) if second else (0 if first else None)
        return (name, base, 22, int(first) if first else None, scale, flag, 0, None)
    if base == "TIMESTAMP":
        return (name, "TIMESTAMP(6)", 11, None, None, flag, 0, None)
    return (name, base, 7, None, None, flag, 0, None)


def plan_catalog_rows(plan):
    return [catalog_row(n, t, nullable) for n, t, nullable in scd2_builder.expected_target_metadata(plan)]


def source_catalog_rows():
    return [catalog_row(c["name"], c["data_type"], c["nullable"]) for c in SOURCE_COLUMNS]


class FakeCursor:
    """Records SQL and answers from a handler ``(sql, params) -> rows``."""

    def __init__(self, log, handler):
        self.log = log
        self.handler = handler
        self._rows = []
        self.closed = False

    def execute(self, sql, params=None):
        self.log.append(" ".join(str(sql).split()))
        result = self.handler(self.log[-1], params or {})
        if isinstance(result, Exception):
            raise result
        self._rows = result or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, log, handler):
        self.log = log
        self.handler = handler
        self.closed = False
        self.autocommit = False
        self.rollback_count = 0

    def cursor(self):
        return FakeCursor(self.log, self.handler)

    def close(self):
        self.closed = True

    def rollback(self):
        self.rollback_count += 1


def connection_factory(log, handler):
    return lambda: FakeConnection(log, handler)


def oracle_error(text):
    return oracledb.DatabaseError(text)


def _envelope(tool, mode, side_effects, result):
    return {
        "tool": tool,
        "status": "passed",
        "mode": mode,
        "side_effects": side_effects,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "result": result,
    }


def _default_plan():
    return scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)


def _probe_column(column):
    data_format = scd2_builder.oracle_ddl_type(column["data_type"])
    parts = re.fullmatch(r"([A-Z][A-Z0-9]*)(?:\((\d+)(?:,(\d+)| (CHAR|BYTE))?\))?", data_format)
    base, length, scale, semantics = parts.groups()
    return {
        "name": column["name"], "role": column["scd_role"],
        "mandatory": not column["nullable"], "data_format": data_format,
        "type_code": base, "length": int(length) if length else None,
        "scale": int(scale) if scale else None, "length_semantics": semantics,
    }


def probe_payload(plan=None, **overrides):
    plan = plan or _default_plan()
    result = {
        "source_model_exists": True,
        "source_datastore_exists": True,
        "source_column_count": 4,
        "target_model_exists": True,
        "target_model_oracle": True,
        "target_datastore_exists": False,
        "project_folder_exists": True,
        "ikm_present": True,
        "ikm_scd_type": True,
        "lkm_present": True,
        "mapping_exists": False,
        "scenario_exists": False,
        "scenario_name_collision": False,
        "scenario_versions": [],
        "target_columns": [],
        "target_primary_key": [],
        "target_alternate_key": [],
        "mapping": None,
    }
    if overrides.get("target_datastore_exists"):
        result["target_columns"] = [_probe_column(c) for c in plan["target_columns"]]
        result["target_primary_key"] = plan["primary_key"]["columns"]
        result["target_alternate_key"] = plan["natural_key"]
    if overrides.get("mapping_exists"):
        count = len(plan["target_columns"])
        result["mapping"] = {
            "target_count": 1,
            "target_table": plan["target"]["table"],
            "source_tables": [plan["source"]["table"]],
            "target_component": {"model": plan["models"]["target"], "table": plan["target"]["table"]},
            "source_components": [{"model": plan["models"]["source"], "table": plan["source"]["table"]}],
            "integration_type": "SCD",
            "target_ikm": "IKM Oracle Slowly Changing Dimension",
            "lkm_names": ["LKM SQL to Oracle (Built-In)"],
            "access_point_count": 1,
            "target_attribute_count": count,
            "mapped_attribute_count": count,
        }
    if overrides.get("scenario_exists"):
        result["scenario_versions"] = ["001"]
    result.update(overrides)
    return json.dumps(_envelope("odi.scd2_probe", "generic_scd2_readiness_probe", False, result))


def error_payload(tool, mode, error_type, side_effects=False):
    outcome = {}
    if tool in {"odi.scd2_mapping_create", "odi.scd2_datastore_register"} and side_effects is False:
        outcome = dict(repository_outcome="rolled_back", commit_attempted=False,
                       commit_acknowledged=False, rollback_attempted=True, rollback_succeeded=True,
                       manual_recovery_review_required=False)
    return json.dumps(
        {
            "tool": tool,
            "status": "error",
            "mode": mode,
            "side_effects": side_effects,
            "repository_connection_attempted": True,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "error_type": error_type,
            **outcome,
        }
    )


def register_payload(plan):
    return json.dumps(
        _envelope(
            "odi.scd2_datastore_register",
            "generic_absent_only_scd2_datastore_registration",
            True,
            {
                "status": "passed",
                "target_column_count": len(plan["target_columns"]),
                "primary_key_column_count": 1,
                "natural_key_column_count": len(plan["natural_key"]),
                "scd_role_column_counts": {
                    "surrogate_key": 1,
                    "natural_key": len(plan["natural_key"]),
                    "start_date": 1,
                    "end_date": 1,
                    "current_flag": 1,
                    "tracked_attributes": len(plan["tracked_attributes"]),
                    "overwrite_attributes": len(plan["overwrite_attributes"]) + 4,
                },
                "commit_completed": True,
            },
        )
    )


def mapping_payload(plan):
    count = len(plan["target_columns"])
    return json.dumps(
        _envelope(
            "odi.scd2_mapping_create",
            "generic_absent_only_scd2_mapping_creation",
            True,
            {
                "status": "passed",
                "source_component_count": 1,
                "target_component_count": 1,
                "target_attribute_count": count,
                "mapped_attribute_count": count,
                "integration_type": "SCD",
                "target_ikm": "IKM Oracle Slowly Changing Dimension",
                "access_point_count": 2,
                "lkm_assigned_count": 2,
                "commit_completed": True,
            },
        )
    )


def identity_payload(plan, username="ODI_EXEC", **overrides):
    """Groovy success envelope for the plan's target topology. Tests may override it."""
    payload = {
        "tool": execution_identity.TOOL,
        "status": "passed",
        "mode": execution_identity.MODE,
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "model_count": 1,
        "context_count": 1,
        "mapping_count": 1,
        "data_server_count": 1,
        "model_code": plan["models"]["target"],
        "context_code": execution_identity.EXECUTION_CONTEXT,
        "schema_name": plan["target"]["owner"],
        "technology": execution_identity.ORACLE_TECHNOLOGY,
        "data_server_technology": execution_identity.ORACLE_TECHNOLOGY,
        "username": username,
    }
    payload.update(overrides)
    return json.dumps(payload)


class FakeOrchestrator:
    """Stands in for OdiOrchestrator; scripts answer from ``responses``."""

    odi_home = "ODI_HOME"

    def __init__(self, plan, responses=None, scenario_result=None, run_result=None):
        self.plan = plan
        self.calls = []
        self.responses = {
            "scd2_probe.groovy": probe_payload(),
            "scd2_datastore_register.groovy": register_payload(plan),
            "scd2_mapping_create.groovy": mapping_payload(plan),
            "execution_identity_probe.groovy": identity_payload(plan),
        }
        self.responses.update(responses or {})
        self.scenario_result = scenario_result or {"status": "passed"}
        self.run_result = run_result or {
            "status": "passed",
            "side_effects": True,
            "session": {"id": "1", "status": "D"},
        }

    def run_groovy(self, script, params):
        from odi_orchestrator import _GROOVY_APPROVED

        self.calls.append(("groovy", script, _GROOVY_APPROVED.get(), dict(params)))
        value = self.responses[script]
        return value(params) if callable(value) else value

    def prove_execution_identity(self, *, model_code, context_code, schema_name):
        from odi_orchestrator import _GROOVY_APPROVED

        self.calls.append(
            (
                "groovy",
                "execution_identity_probe.groovy",
                _GROOVY_APPROVED.get(),
                {
                    "odi.execution.model": model_code,
                    "odi.execution.context": context_code,
                    "odi.execution.schema": schema_name,
                },
            )
        )
        value = self.responses["execution_identity_probe.groovy"]
        output = value(model_code) if callable(value) else value
        return execution_identity.interpret_execution_identity(
            output, model_code=model_code, context_code=context_code, schema_name=schema_name
        )

    def generate_scenario_metadata(self, **kwargs):
        self.calls.append(("scenario_generate", kwargs))
        return self.scenario_result

    def run_scenario_safe(self, **kwargs):
        self.calls.append(("scenario_run", kwargs))
        return self.run_result

    @property
    def order(self):
        return [c[1] if c[0] == "groovy" else c[0] for c in self.calls]


def default_catalog_handler(sql, params):
    if "ALL_TAB_COLUMNS" in sql:
        return source_catalog_rows()
    return [(0,)]


def default_ddl_handler(sql, params):
    return [(1,)] if "FROM ALL_USERS" in sql else [(0,)]


def make_services(orchestrator, ddl_log=None, ddl_handler=None, catalog_log=None, catalog_handler=None):
    ddl_log = [] if ddl_log is None else ddl_log
    catalog_log = [] if catalog_log is None else catalog_log
    return Scd2Services(
        catalog_connection=connection_factory(catalog_log, catalog_handler or default_catalog_handler),
        ddl_connection=connection_factory(ddl_log, ddl_handler or default_ddl_handler),
        ddl_user="DWH_DDL",
        orchestrator_factory=lambda: orchestrator,
    )
