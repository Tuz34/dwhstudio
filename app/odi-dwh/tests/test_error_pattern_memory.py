import json
import ast
from pathlib import Path

import pytest

import error_pattern_memory as memory
import rag_manager


@pytest.fixture
def isolated_rag(tmp_path, monkeypatch):
    db_path = tmp_path / "rag_database.json"
    monkeypatch.setattr(rag_manager, "CHROMA_DB_FILE", str(db_path))
    monkeypatch.setattr(rag_manager, "_cache", {"db": None, "ts": 0.0})
    monkeypatch.setattr(rag_manager, "get_embedding", lambda _text: [])
    yield db_path


def test_add_error_pattern_stores_sanitized_document(isolated_rag):
    result = memory.add_error_pattern(
        memory.ErrorPattern(
            code="ORA-01031",
            title="Insufficient privileges during setup",
            context="Oracle setup script",
            symptom="DDL operation is rejected",
            root_cause="Runtime user is not the schema owner",
            resolution="Use a narrow setup user for initialization",
            guardrails="Do not persist credentials",
            tags=("oracle", "security"),
        )
    )

    assert result["status"] == "stored"
    assert result["error_code"] == "ORA-01031"

    db = json.loads(isolated_rag.read_text(encoding="utf-8"))
    docs = db["error_patterns"]
    assert len(docs) == 1
    assert "ORA-01031" in docs[0]["text"]
    assert docs[0]["metadata"]["category"] == "learned_error_pattern"
    assert docs[0]["metadata"]["safety"] == "blocked_secret_like_and_raw_rows"


def test_add_error_pattern_is_idempotent_for_same_pattern(isolated_rag):
    pattern = memory.ErrorPattern(
        code="ODI-16171",
        title="Merge KM requires update key",
        context="ODI physical design",
        symptom="KM validation reports missing update key",
        root_cause="Target datastore key metadata is missing",
        resolution="Reverse-engineer or define target key metadata",
    )

    first = memory.add_error_pattern(pattern)
    second = memory.add_error_pattern(pattern)

    assert first["doc_id"] == second["doc_id"]
    db = json.loads(isolated_rag.read_text(encoding="utf-8"))
    assert len(db["error_patterns"]) == 1


@pytest.mark.parametrize(
    "field_value",
    [
        "PASSWORD=oracle1",
        "jdbc:oracle:thin:@localhost:1521/orcl",
        "rows=[{'ID': 1, 'NAME': 'Alice'}]",
        "-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----",
        "sk-ant-abcdef1234567890",
    ],
)
def test_add_error_pattern_blocks_sensitive_inputs(isolated_rag, field_value):
    with pytest.raises(memory.SensitivePatternError):
        memory.add_error_pattern(
            memory.ErrorPattern(
                code="ORA-01017",
                title="Blocked pattern",
                context=field_value,
                symptom="Login failed",
                root_cause="Credential mismatch",
                resolution="Check configured secret outside RAG",
            )
        )

    assert not isolated_rag.exists()


def test_search_error_patterns_blocks_sensitive_query(isolated_rag):
    with pytest.raises(memory.SensitivePatternError):
        memory.search_error_patterns("TOKEN=abc123")


def test_seed_recent_safe_patterns_writes_known_project_patterns(isolated_rag):
    results = memory.seed_recent_safe_patterns()

    codes = {result["error_code"] for result in results}
    assert {
        "ODI-METAMODEL_DATA_FORMAT",
        "ODI-STUDIO_FREEZE_METAMODEL",
        "ODI-16171",
        "ORA-01031",
    } <= codes

    db = json.loads(isolated_rag.read_text(encoding="utf-8"))
    text = "\n".join(doc["text"] for doc in db["error_patterns"])
    assert "SNP_*" in text
    assert "oracle1" not in text
    assert "jdbc:" not in text.lower()


def test_rag_module_import_does_not_write_the_store():
    source_path = Path(rag_manager.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    top_level_calls = [
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]

    assert "init_collections" not in top_level_calls


def test_rag_atomic_save_preserves_previous_file_on_serialization_failure(isolated_rag, monkeypatch):
    isolated_rag.write_text('{"existing": ["safe"]}', encoding="utf-8")

    def fail_dump(*_args, **_kwargs):
        raise TypeError("synthetic serialization failure")

    monkeypatch.setattr(rag_manager.json, "dump", fail_dump)

    with pytest.raises(TypeError, match="synthetic serialization failure"):
        rag_manager._save_db({"replacement": []})

    assert json.loads(isolated_rag.read_text(encoding="utf-8")) == {"existing": ["safe"]}
    assert list(isolated_rag.parent.glob("*.tmp")) == []


def test_corrupt_rag_store_fails_closed_without_overwriting_existing_bytes(isolated_rag):
    corrupt_content = '{"existing": ['
    isolated_rag.write_text(corrupt_content, encoding="utf-8")

    with pytest.raises(rag_manager.RagStoreCorruptionError):
        rag_manager.add_document("error_patterns", "synthetic safe document")

    assert isolated_rag.read_text(encoding="utf-8") == corrupt_content
