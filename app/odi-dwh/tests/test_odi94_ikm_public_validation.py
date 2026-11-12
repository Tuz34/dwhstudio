"""Preserve only the six application-authored IKM validation sentences."""

import json

import pytest


_BASE_PARAMS = {
    "mapping_name": "MAP_X",
    "src_table": "SRC_X",
    "tgt_table": "ODS_X",
}


@pytest.mark.parametrize(
    "ikm_name,integration_type,approved,priority,sentence",
    [
        ("invalid?", "IU", "IKM Oracle Insert", "IKM Oracle Insert", "ikm_name is invalid."),
        ("IKM Unknown", "IU", "IKM Oracle Insert", "IKM Oracle Insert",
         "ikm_name is not in the approved IKM list (ODI_APPROVED_IKMS)."),
        (None, "CONTROL_APPEND", "IKM Oracle Merge", "IKM Oracle Merge",
         "CONTROL_APPEND needs an insert IKM in the approved IKM list."),
        (None, "IU", "IKM Oracle Insert,IKM Oracle Merge", "Missing",
         "Specify ikm_name: the first IKM priority is not one unambiguous approved name."),
        (None, "IU", "IKM Invalid?Name", "IKM Invalid",
         "The configured ikm_name is invalid."),
        ("IKM Oracle Merge", "CONTROL_APPEND", "IKM Oracle Insert,IKM Oracle Merge", "IKM Oracle Insert",
         "CONTROL_APPEND cannot use a merge or update IKM."),
    ],
)
def test_analyze_prompt_preserves_exact_ikm_sentence(
    app_client, monkeypatch, ikm_name, integration_type, approved, priority, sentence,
):
    import mapping_generator

    params = {**_BASE_PARAMS, "integration_type": integration_type}
    if ikm_name is not None:
        params["ikm_name"] = ikm_name
    monkeypatch.setattr(mapping_generator, "ODI_APPROVED_IKMS", approved)
    monkeypatch.setattr(mapping_generator, "ODI_IKM_PRIORITY", priority)
    monkeypatch.setattr(mapping_generator.MappingGenerator, "parse_prompt", lambda *_args: params)

    response = app_client.post("/api/mapping/analyze-prompt", json={"prompt": "map X"})
    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "prompt": "map X", "params": params, "preview": None,
        "validation": {"status": "INVALID", "error": sentence, "error_type": "ValueError"},
    }

    direct = mapping_generator.MappingGenerator().analyze_prompt("map X")
    assert direct["validation"]["error"] == sentence


@pytest.mark.parametrize("detail", [
    "ODI94_UNTRUSTED_VALIDATION_MARKER",
    "ikm_name is invalid. ODI94_UNTRUSTED_VALIDATION_MARKER",
])
@pytest.mark.parametrize("ikm_name", [None, "IKM Oracle Insert"])
def test_analyze_prompt_rejects_other_ikm_value_errors(app_client, monkeypatch, detail, ikm_name):
    import mapping_generator

    params = {**_BASE_PARAMS}
    if ikm_name is not None:
        params["ikm_name"] = ikm_name
    monkeypatch.setattr(mapping_generator.MappingGenerator, "parse_prompt", lambda *_args: params)

    def fail(*_args):
        raise ValueError(detail)

    monkeypatch.setattr(mapping_generator, "resolve_approved_ikm", fail)
    response = app_client.post("/api/mapping/analyze-prompt", json={"prompt": "map X"})
    assert response.status_code == 200
    body = response.get_json()
    assert detail not in json.dumps(body)
    assert body["validation"] == {"status": "INVALID", "error": "mapping prompt analysis failed.",
                                  "error_type": "OperationError"}
    direct = mapping_generator.MappingGenerator().analyze_prompt("map X")
    assert direct["validation"]["error"] == "Parameter validation failed."
