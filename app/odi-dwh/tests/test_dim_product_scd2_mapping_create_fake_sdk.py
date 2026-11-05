"""Run the real fixed mapping script against an isolated fake SDK.

Synthetic evidence only. No ODI repository, database or live object is touched.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from dim_product_odi_scd2_mapping_builder import normalize_dim_product_odi_scd2_mapping
from tests import dim_product_mapping_fake_sdk as fake

MAPPING = "oracle.odi.domain.mapping.Mapping"
NODE = "oracle.odi.domain.mapping.physical.MapPhysicalNode"
DESIGN = "oracle.odi.domain.mapping.physical.MapPhysicalDesign"
OPTION = "oracle.odi.domain.adapter.project.IOptionValue"
KM = "oracle.odi.domain.project.OdiKM"
ISSUE = "oracle.odi.domain.root.RootIssue"
KM_ARG = "oracle.odi.domain.adapter.project.IKnowledgeModule"

STOCK_FINAL_OPTIONS = {
    "FLOW_CONTROL": False,
    "STATIC_CONTROL": False,
    "RECYCLE_ERRORS": False,
    "TRUNCATE": False,
    "DELETE_ALL": False,
    "CREATE_TARG_TABLE": False,
    "COMMIT": True,
}

SCENARIOS: dict[str, dict] = {
    "clean_one_ap": {},
    "clean_zero_ap": {"ap_count": 0},
    "clean_two_ap": {"ap_count": 2},
    "warning_only": {"final_validate": {"levels": ["WARNING", "INFO"]}},
    "validate_true_empty": {"final_validate": {"flag": True}},
    "validate_true_error": {"final_validate": {"flag": True, "levels": ["ERROR"]}},
    "validate_false_error": {"final_validate": {"levels": ["ERROR"]}},
    "validate_false_fatal": {"final_validate": {"levels": ["FATAL"]}},
    "validate_raises": {"final_validate": {"raises": True}},
    "first_validate_error_is_repairable": {"first_validate": {"flag": True, "levels": ["ERROR"]}},
    "first_validate_raises": {"first_validate": {"raises": True}},
    "ap_materialized_on_first_validate": {"materialize_ap_on_validate": True},
    "ap_materialized_on_final_validate": {"materialize_ap_on_final_validate": True},
    "lkm_set_ignored": {"lkm_set_ignored": True},
    "ikm_name_mismatch": {"ikm_name_mismatch": True},
    "options_reset_on_first_validate": {"options_reset_on_first_validate": True},
    "options_reset_on_final_validate": {"options_reset_on_final_validate": True},
    "flow_option_missing": {"missing_options": ["FLOW_CONTROL"]},
    "static_option_missing": {"missing_options": ["STATIC_CONTROL"]},
    "commit_option_missing": {"missing_options": ["COMMIT"]},
    "flow_option_sticky": {"sticky_options": ["FLOW_CONTROL"]},
    "truncate_default_on": {"option_defaults": {"TRUNCATE": True}},
    "delete_all_default_on": {"option_defaults": {"DELETE_ALL": True}},
    "create_target_default_on": {"option_defaults": {"CREATE_TARG_TABLE": True}},
    "recycle_default_on": {"option_defaults": {"RECYCLE_ERRORS": True}},
    "commit_default_off": {"option_defaults": {"COMMIT": False}},
    "two_lkm_candidates": {
        "lkms": [{"name": fake.LKM_NAME, "project": None}, {"name": fake.LKM_NAME, "project": "PRJ"}],
    },
    "two_ikm_candidates": {
        "ikms": [{"name": fake.IKM_NAME, "project": None}, {"name": fake.IKM_NAME, "project": "PRJ"}],
    },
    "other_project_ikm_only": {"ikms": [{"name": fake.IKM_NAME, "project": "OTHER"}]},
    "other_project_copy_ignored": {
        "ikms": [{"name": fake.IKM_NAME, "project": "OTHER"}, {"name": fake.IKM_NAME, "project": "PRJ"}],
    },
    "similar_lkm_name_only": {"lkms": [{"name": fake.LKM_NAME.lower(), "project": None}]},
    "lkm_absent": {"lkms": [{"name": "LKM Another", "project": None}]},
    "wrong_ikm_type": {
        "ikms": [{"name": fake.IKM_NAME, "project": "PRJ", "integration_type": "CONTROL_APPEND"}],
    },
    "mapping_exists": {"mapping_exists": True},
    "scenario_exists": {"scenario_exists": True},
    "begin_raises": {"begin_raises": True},
    "commit_raises": {"commit_raises": True},
    "commit_applied_then_raises": {"commit_applied_then_raises": True},
    "commit_applied_then_error": {"commit_applied_then_error": True},
    "commit_raises_rollback_raises": {"commit_raises": True, "rollback_raises": True},
    "commit_applied_rollback_raises": {"commit_applied_then_raises": True, "rollback_raises": True},
    "rollback_raises_before_commit": {"final_validate": {"levels": ["ERROR"]}, "rollback_raises": True},
    "rollback_error_before_commit": {"final_validate": {"levels": ["ERROR"]}, "rollback_error": True},
    "success_report_print_fails": {"print_failures": 1},
    "standalone_clean": {"standalone": True},
    "standalone_close_raises_after_success": {"standalone": True, "close_raises": True},
    "standalone_close_raises_after_error": {
        "standalone": True, "close_raises": True, "final_validate": {"levels": ["ERROR"]},
    },
    "standalone_close_raises_after_unknown": {
        "standalone": True, "close_raises": True, "commit_applied_then_raises": True,
    },
}

# Fixed report for each repository outcome. An uncertain commit stays unknown even when rollback succeeds.
COMMIT_UNKNOWN = {
    "status": "outcome_unknown",
    "repository_outcome": "commit_outcome_unknown",
    "side_effects": None,
    "commit_attempted": True,
    "commit_acknowledged": False,
    "rollback_attempted": True,
    "manual_recovery_review_required": True,
}
ROLLBACK_FAILED = {
    "status": "outcome_unknown",
    "repository_outcome": "rollback_failed",
    "side_effects": None,
    "commit_attempted": False,
    "commit_acknowledged": False,
    "rollback_attempted": True,
    "rollback_succeeded": False,
    "manual_recovery_review_required": True,
}
UNCERTAIN_OUTCOMES = {
    "commit_raises": {**COMMIT_UNKNOWN, "rollback_succeeded": True},
    "commit_applied_then_raises": {**COMMIT_UNKNOWN, "rollback_succeeded": True},
    "commit_applied_then_error": {**COMMIT_UNKNOWN, "rollback_succeeded": True},
    "commit_raises_rollback_raises": {**COMMIT_UNKNOWN, "rollback_succeeded": False},
    "commit_applied_rollback_raises": {**COMMIT_UNKNOWN, "rollback_succeeded": False},
    "standalone_close_raises_after_unknown": {**COMMIT_UNKNOWN, "rollback_succeeded": True},
    "rollback_raises_before_commit": ROLLBACK_FAILED,
    "rollback_error_before_commit": ROLLBACK_FAILED,
}
# The repository state a test harness can see, which the report must not contradict.
APPLIED_IN_THE_MODEL = {
    "commit_raises": False,
    "commit_applied_then_raises": True,
    "commit_applied_then_error": True,
    "commit_raises_rollback_raises": False,
    "commit_applied_rollback_raises": True,
    "standalone_close_raises_after_unknown": True,
    "rollback_raises_before_commit": False,
    "rollback_error_before_commit": False,
}
OTHER_REPORTS = {
    "begin_raises",
    "success_report_print_fails",
    "standalone_clean",
    "standalone_close_raises_after_success",
    "standalone_close_raises_after_error",
}

BLOCKED_BEFORE_COMMIT = [
    "validate_true_empty",
    "validate_true_error",
    "validate_false_error",
    "validate_false_fatal",
    "validate_raises",
    "first_validate_raises",
    "ap_materialized_on_final_validate",
    "lkm_set_ignored",
    "ikm_name_mismatch",
    "options_reset_on_final_validate",
    "flow_option_missing",
    "static_option_missing",
    "commit_option_missing",
    "flow_option_sticky",
    "truncate_default_on",
    "delete_all_default_on",
    "create_target_default_on",
    "recycle_default_on",
    "commit_default_off",
]
BLOCKED_BEFORE_CREATE = [
    "two_lkm_candidates",
    "two_ikm_candidates",
    "other_project_ikm_only",
    "similar_lkm_name_only",
    "lkm_absent",
    "wrong_ikm_type",
    "mapping_exists",
    "scenario_exists",
]
PASSING = [
    "clean_one_ap",
    "clean_zero_ap",
    "clean_two_ap",
    "warning_only",
    "first_validate_error_is_repairable",
    "ap_materialized_on_first_validate",
    "options_reset_on_first_validate",
    "other_project_copy_ignored",
]


@pytest.fixture(scope="module")
def java():
    binary = fake.java_binary()
    if binary is None or not fake.GROOVY_JAR.is_file():
        pytest.skip("Local Java/Groovy runtime not available; no runtime is downloaded")
    return binary


@pytest.fixture(scope="module")
def runs(java, tmp_path_factory):
    return fake.run_scenarios(java, tmp_path_factory.mktemp("fake_sdk_run"), SCENARIOS)


def payload_of(run: dict) -> dict:
    lines = [line for line in run["output"].splitlines() if line.strip()]
    assert len(lines) == 1, "The script must emit exactly one structured line"
    return json.loads(lines[0])


def test_every_scenario_has_an_expected_outcome():
    assert set(SCENARIOS) == (
        set(BLOCKED_BEFORE_COMMIT) | set(BLOCKED_BEFORE_CREATE) | set(PASSING) | set(UNCERTAIN_OUTCOMES) | OTHER_REPORTS
    )


@pytest.mark.parametrize("name", PASSING)
def test_clean_designs_commit_with_final_validation_before_commit(runs, name):
    run = runs[name]
    payload = payload_of(run)
    trace = run["trace"]

    assert payload["status"] == "passed"
    assert run["committed"] is True
    assert "rollback" not in trace
    assert trace.index("commit") == len(trace) - 1
    assert trace[-2] == "validate"
    normalized = normalize_dim_product_odi_scd2_mapping(payload)
    assert normalized["status"] == "passed"


@pytest.mark.parametrize("name", BLOCKED_BEFORE_COMMIT)
def test_failures_after_create_roll_back_without_commit(runs, name):
    run = runs[name]
    payload = payload_of(run)

    assert payload["status"] == "error"
    assert payload["side_effects"] is False
    assert payload["repository_outcome"] == "rolled_back"
    assert payload["rollback_succeeded"] is True
    assert payload["manual_recovery_review_required"] is False
    assert run["committed"] is False
    assert "commit" not in run["trace"]
    assert run["trace"][-1] == "rollback"
    normalized = normalize_dim_product_odi_scd2_mapping(payload)
    assert normalized["status"] == "error"
    assert normalized["side_effects"] is False
    assert normalized["repository_outcome"] == "rolled_back"


@pytest.mark.parametrize("name", BLOCKED_BEFORE_CREATE)
def test_identity_and_collision_failures_block_before_creating_anything(runs, name):
    run = runs[name]

    assert payload_of(run)["status"] == "error"
    assert run["committed"] is False
    assert "persist" not in run["trace"]
    assert "commit" not in run["trace"]


@pytest.mark.parametrize("name", list(UNCERTAIN_OUTCOMES))
def test_uncertain_repository_state_is_never_reported_as_clean(runs, name):
    run = runs[name]
    payload = payload_of(run)
    normalized = normalize_dim_product_odi_scd2_mapping(payload)

    assert run["committed"] is APPLIED_IN_THE_MODEL[name]
    assert run["script_raised"] is None
    for field, expected in UNCERTAIN_OUTCOMES[name].items():
        assert payload[field] == expected, field
        assert normalized[field] == expected, field
    assert "result" not in normalized


def test_applied_and_unapplied_commit_failures_report_the_same_unknown(runs):
    unapplied = payload_of(runs["commit_raises"])
    applied = payload_of(runs["commit_applied_then_raises"])

    assert runs["commit_raises"]["committed"] is False
    assert runs["commit_applied_then_raises"]["committed"] is True
    assert unapplied == applied
    assert applied["error_type"] == "RuntimeException"


def test_failure_before_the_transaction_starts_is_a_clean_error(runs):
    run = runs["begin_raises"]
    payload = payload_of(run)
    normalized = normalize_dim_product_odi_scd2_mapping(payload)

    assert payload["repository_outcome"] == "no_transaction"
    assert payload["rollback_attempted"] is False
    assert run["trace"] == []
    assert normalized["status"] == "error"
    assert normalized["side_effects"] is False
    assert normalized["manual_recovery_review_required"] is False


def test_acknowledged_commit_with_a_failed_report_keeps_the_commit(runs):
    run = runs["success_report_print_fails"]
    payload = payload_of(run)
    normalized = normalize_dim_product_odi_scd2_mapping(payload)

    assert run["committed"] is True
    assert "rollback" not in run["trace"]
    for report in (payload, normalized):
        assert report["status"] == "committed_report_incomplete"
        assert report["repository_outcome"] == "committed"
        assert report["side_effects"] is True
        assert report["commit_acknowledged"] is True
        assert report["rollback_attempted"] is False
        assert report["manual_recovery_review_required"] is True
    assert "result" not in normalized


@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("standalone_clean", "passed"),
        ("standalone_close_raises_after_success", "passed"),
        ("standalone_close_raises_after_error", "error"),
        ("standalone_close_raises_after_unknown", "outcome_unknown"),
    ],
)
def test_instance_close_failure_cannot_replace_the_report(runs, name, status):
    run = runs[name]

    assert run["script_raised"] is None
    assert payload_of(run)["status"] == status
    assert run["trace"][-1] == "odi_close"


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_output_never_leaks_issue_or_exception_text(runs, name):
    output = runs[name]["output"]

    assert fake.ISSUE_TOKEN not in output
    assert fake.RAISE_TOKEN not in output
    assert "jdbc" not in output.lower()


@pytest.mark.parametrize(("name", "expected"), [("clean_zero_ap", 0), ("clean_one_ap", 1), ("clean_two_ap", 2)])
def test_every_access_point_gets_the_lkm_through_the_lkm_api(runs, name, expected):
    run = runs[name]
    result = payload_of(run)["result"]

    assert run["state"]["lkm_names"] == [fake.LKM_NAME] * expected
    assert run["state"]["xkm_names"] == [""] * expected
    assert "setXKM" not in run["trace"]
    assert result["access_point_count"] == expected
    assert result["lkm_assigned_count"] == expected


def test_access_point_materialized_by_first_validation_is_also_assigned(runs):
    run = runs["ap_materialized_on_first_validate"]

    assert run["state"]["lkm_names"] == [fake.LKM_NAME, fake.LKM_NAME]
    assert payload_of(run)["result"]["lkm_assigned_count"] == 2


@pytest.mark.parametrize("name", ["clean_one_ap", "options_reset_on_first_validate", "clean_zero_ap"])
def test_committed_design_has_the_exact_stock_options(runs, name):
    assert runs[name]["state"]["options"] == STOCK_FINAL_OPTIONS


def test_only_flow_and_static_control_are_written(runs):
    written = {event.split(":")[1] for event in runs["clean_one_ap"]["trace"] if event.startswith("option:")}

    assert written == {"FLOW_CONTROL=false", "STATIC_CONTROL=false"}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_existing_datastore_metadata_is_never_mutated(runs, name):
    assert "datastore_mutation" not in runs[name]["trace"]


def test_final_validation_runs_after_the_km_and_option_work(runs):
    trace = runs["clean_one_ap"]["trace"]

    assert trace.index("resetSpec") < trace.index("setTargetComponentKM")
    assert trace.index("setTargetComponentKM") < trace.index("option:FLOW_CONTROL=false")
    assert trace.index("option:STATIC_CONTROL=false") < trace.index("setLKM")
    assert trace.index("setLKM") < len(trace) - 2
    assert trace.count("validate") == 2


FINAL_GATE = "if (validationFlagged || RootIssue.isErrorOrFatal(finalIssues)) {"
POST_VALIDATE_CHECK = "if (verifyPhysicalContract().size() != accessPoints.size()) {"
SETTLE_PASS = "mapping.validate(new ArrayList<RootIssue>(), false)"
OPTION_READ_BACK = "!String.valueOf(expected).equalsIgnoreCase(String.valueOf(optionValue.getOptionValue()))"
LKM_READ_BACK = "if (nodes.any { LKM_NAME != it.getLKMName() }) {"
KM_CARDINALITY = "if (candidates.size() != 1) {"
SECOND_LKM_PASS = """    writeIkmOptions(targetNode)
    assignAccessPointLkm()
    List<MapPhysicalNode> accessPoints = verifyPhysicalContract()"""


@pytest.mark.parametrize(
    ("mutation", "needle", "replacement", "scenario"),
    [
        ("ignores_boolean", FINAL_GATE, "if (RootIssue.isErrorOrFatal(finalIssues)) {", "validate_true_empty"),
        ("ignores_issues", FINAL_GATE, "if (validationFlagged) {", "validate_false_fatal"),
        ("no_post_validate_check", POST_VALIDATE_CHECK, "if (false) {", "ap_materialized_on_final_validate"),
        ("no_option_read_back", OPTION_READ_BACK, "false", "flow_option_sticky"),
        ("no_option_read_back", OPTION_READ_BACK, "false", "truncate_default_on"),
        ("no_lkm_read_back", LKM_READ_BACK, "if (false) {", "lkm_set_ignored"),
    ],
)
def test_removing_a_commit_gate_is_detected(java, tmp_path, monkeypatch, mutation, needle, replacement, scenario):
    """Each mutation is written to a temporary copy, and the committed script stays intact."""
    original = fake.SCRIPT.read_text(encoding="utf-8")
    assert needle in original
    mutated = tmp_path / f"{mutation}.groovy"
    mutated.write_text(original.replace(needle, replacement), encoding="utf-8")
    monkeypatch.setattr(fake, "SCRIPT", mutated)

    run = fake.run_scenarios(java, tmp_path, {scenario: SCENARIOS[scenario]})[scenario]

    assert run["committed"] is True, "Removing this gate must let the bad design commit"


def test_ambiguous_km_check_is_detected(java, tmp_path, monkeypatch):
    original = fake.SCRIPT.read_text(encoding="utf-8")
    assert KM_CARDINALITY in original
    mutated = tmp_path / "km.groovy"
    mutated.write_text(original.replace(KM_CARDINALITY, "if (candidates.isEmpty()) {"), encoding="utf-8")
    monkeypatch.setattr(fake, "SCRIPT", mutated)

    run = fake.run_scenarios(java, tmp_path, {"two": SCENARIOS["two_lkm_candidates"]})["two"]

    assert "persist" in run["trace"]


@pytest.mark.parametrize(
    ("needle", "replacement", "scenario"),
    [
        (SECOND_LKM_PASS, "    List<MapPhysicalNode> accessPoints = verifyPhysicalContract()", "ap_materialized_on_first_validate"),
        (SECOND_LKM_PASS, "    List<MapPhysicalNode> accessPoints = verifyPhysicalContract()", "options_reset_on_first_validate"),
        (SETTLE_PASS, "", "ap_materialized_on_final_validate"),
    ],
)
def test_removing_the_settle_steps_changes_the_outcome(java, tmp_path, monkeypatch, needle, replacement, scenario):
    original = fake.SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert needle in original
    mutated = tmp_path / "settle.groovy"
    mutated.write_text(original.replace(needle, replacement), encoding="utf-8")
    monkeypatch.setattr(fake, "SCRIPT", mutated)

    run = fake.run_scenarios(java, tmp_path, {scenario: SCENARIOS[scenario]})[scenario]

    if needle == SETTLE_PASS:
        # With no settle pass, the final pass is the first call, and the late access point never appears.
        assert run["state"]["lkm_names"] == [fake.LKM_NAME]
    else:
        assert run["committed"] is False


def test_script_uses_only_public_methods_of_the_installed_sdk(java, tmp_path):
    jar = fake.SDK_LIB / "odi-core.jar"
    if not jar.is_file() or not fake.ECLIPSELINK_JAR.is_file():
        pytest.skip("Local ODI SDK not available; no runtime is downloaded")
    java_list = "java.util.List"
    required = [
        {"type": MAPPING, "method": "validate", "params": [java_list, "boolean"]},
        {"type": ISSUE, "method": "isErrorOrFatal", "params": [java_list]},
        {"type": DESIGN, "method": "getAllAPNodes", "params": []},
        {"type": DESIGN, "method": "getPhysicalNodes", "params": []},
        {"type": DESIGN, "method": "resetSpec", "params": []},
        {"type": NODE, "method": "setLKM", "params": [KM_ARG]},
        {"type": NODE, "method": "getLKMName", "params": []},
        {"type": NODE, "method": "setTargetComponentKM", "params": [KM_ARG]},
        {"type": NODE, "method": "getTargetComponentKMName", "params": []},
        {"type": NODE, "method": "getIKMOptionValue", "params": ["java.lang.String"]},
        {"type": OPTION, "method": "getOptionValue", "params": []},
        {"type": OPTION, "method": "setValue", "params": ["java.lang.Object"]},
        {"type": KM, "method": "isGlobal", "params": []},
        {"type": KM, "method": "getProject", "params": []},
    ]

    result = fake.check_real_public_api(java, tmp_path, required)

    assert result == {"missing": [], "script_imports_resolved": True}


def test_locked_option_names_exist_in_the_stock_ikm_reference():
    reference = fake.ODI_HOME / "odi" / "sdk" / "xml-reference" / f"KM_{fake.IKM_NAME}.xml"
    if not reference.is_file():
        pytest.skip("Local stock IKM reference not available; no runtime is downloaded")
    names = set()
    for item in ET.parse(reference).getroot().iter("Object"):
        if item.get("class", "").endswith(".SnpUserExit"):
            for field in item.findall("Field"):
                if field.get("name") == "UeName":
                    names.add(field.text)

    assert set(STOCK_FINAL_OPTIONS) <= names
