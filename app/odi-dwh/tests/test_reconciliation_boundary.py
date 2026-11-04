from __future__ import annotations

import inspect

import reconciliator
import reconciliation_service


def test_reconciliation_is_metadata_only_and_has_no_persistence_side_effects():
    compare_source = inspect.getsource(reconciliator._compare_data_by_pk)
    reconcile_source = inspect.getsource(reconciliator.reconcile_table)

    assert "sample_keys" not in compare_source
    assert '"pk_value"' not in compare_source
    assert '"src_value"' not in compare_source
    assert '"tgt_value"' not in compare_source
    assert "_write_diff_rows" not in compare_source
    assert "RECONCILIATION_LOG" not in reconcile_source
    assert "INSERT INTO" not in reconcile_source
    assert "CREATE TABLE" not in reconcile_source


def test_reconciliation_source_contract_declares_no_raw_evidence():
    source = inspect.getsource(reconciliation_service.reconcile_table)

    assert '"pk_values_included": False' in source
    assert '"raw_rows_included": False' in source
    assert '"write_operations_attempted": []' in source
