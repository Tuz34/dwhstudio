"""Wizard owner list stays aligned with the generator. No network."""

from __future__ import annotations

import re
from pathlib import Path

import mapping_generator
from mapping_generator import ALLOWED_INTERNAL_OWNERS, classic_internal_source_model

_WIZARD = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "components"
    / "modals"
    / "mappingWizardRequests.js"
)


def test_wizard_internal_owners_match_the_generator_set():
    text = _WIZARD.read_text(encoding="utf-8")
    owners = re.search(r"INTERNAL_SOURCE_OWNERS = Object\.freeze\(\[(.*?)\]\)", text, re.DOTALL)
    assert owners is not None
    listed = re.findall(r"'([A-Z][A-Z0-9_]*)'", owners.group(1))
    assert listed == sorted(ALLOWED_INTERNAL_OWNERS)
    model = re.search(r"INTERNAL_SOURCE_MODEL = '([A-Z][A-Z0-9_]*)'", text)
    assert model is not None
    assert model.group(1) == mapping_generator.CLASSIC_INTERNAL_SOURCE_MODEL == "ORACLE_DWH"
    for owner in sorted(ALLOWED_INTERNAL_OWNERS):
        assert classic_internal_source_model(owner) == "ORACLE_DWH"
        assert classic_internal_source_model(owner.lower()) == "ORACLE_DWH"
    assert classic_internal_source_model("HR") is None
    assert classic_internal_source_model("SH") is None
    assert classic_internal_source_model("") is None
