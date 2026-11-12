"""Fixed sentences for expected skips logged by MappingGenerator.

The redactor shows one of these details unchanged. Every other skip detail
becomes STEP_SKIPPED. Failure statuses are not decided here.
"""

STEP_SKIPPED = "Step skipped."

AUDIT_PRESERVATION_DOES_NOT_APPLY = "Audit preservation does not apply to this load."
SOURCE_DATASTORE_ALREADY_EXISTS = "Source datastore already exists."
TARGET_DATASTORE_ALREADY_EXISTS = "Target datastore already exists."
SKEY_NOT_REQUESTED = "SKEY not requested"
SKEY_NATURAL_KEY_NOT_SPECIFIED = "skey_natural_key was not specified"
NO_SKEY_CHANGE = "No SKEY change"
TARGET_TABLE_DID_NOT_CHANGE = "Target table did not change"
NO_ROLLBACK_RECORD = "No rollback record found."

EXPECTED_SKIP_SENTENCES = frozenset({
    AUDIT_PRESERVATION_DOES_NOT_APPLY,
    SOURCE_DATASTORE_ALREADY_EXISTS,
    TARGET_DATASTORE_ALREADY_EXISTS,
    SKEY_NOT_REQUESTED,
    SKEY_NATURAL_KEY_NOT_SPECIFIED,
    NO_SKEY_CHANGE,
    TARGET_TABLE_DID_NOT_CHANGE,
    NO_ROLLBACK_RECORD,
})
