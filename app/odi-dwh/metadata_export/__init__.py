"""Neutral metadata graph export contract (ODI-79 Phase 0).

Static types, a draft 2020-12 schema, a synthetic fixture, and a validator.
This package does not open connections and does not register HTTP routes.
"""

from .contract import CONTRACT_VERSION, PRODUCER
from .validate import validate_document

__all__ = ["CONTRACT_VERSION", "PRODUCER", "validate_document"]
