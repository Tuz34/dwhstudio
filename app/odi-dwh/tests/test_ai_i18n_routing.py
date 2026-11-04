import ai_agent
import ai_helpers


def _hybrid_env(monkeypatch, ollama_result):
    monkeypatch.setattr(ai_agent, "normalize_provider", lambda provider=None: "hybrid")
    monkeypatch.setattr(ai_agent, "is_local_openai_available", lambda: False)
    monkeypatch.setattr(ai_agent, "is_ollama_available", lambda: True)
    monkeypatch.setattr(ai_agent, "is_claude_configured", lambda: True)
    monkeypatch.setattr(ai_agent, "call_ollama", lambda messages: ollama_result)
    monkeypatch.setattr(ai_agent, "call_claude", lambda messages: "CLAUDE_FALLBACK")


def test_hybrid_keeps_valid_answer_that_mentions_unavailable(monkeypatch):
    answer = '{"description": "Table ODS_X is unavailable in this schema.", "queries": []}'
    _hybrid_env(monkeypatch, answer)
    assert ai_agent.call_llm([{"role": "user", "content": "hi"}]) == answer


def test_hybrid_falls_back_only_on_ollama_sentinel(monkeypatch):
    _hybrid_env(monkeypatch, ai_agent.OLLAMA_UNAVAILABLE_RESPONSE)
    assert ai_agent.call_llm([{"role": "user", "content": "hi"}]) == "CLAUDE_FALLBACK"


HISTORY_MISSING = [{"role": "assistant", "content": "3 tables are missing an ODI flow."}]


def test_bulk_intent_ignores_words_containing_tabl_substring():
    history = [{"role": "assistant", "content": "The stable, suitable plan has missing items."}]
    assert ai_helpers._has_recent_bulk_model_intent("create mapping", history) is False


def test_bulk_intent_matches_table_and_tablo_words():
    assert ai_helpers._has_recent_bulk_model_intent("create mapping", HISTORY_MISSING) is True
    turkish = [{"role": "assistant", "content": "Eksik olan tablolar var."}]
    assert ai_helpers._has_recent_bulk_model_intent("mapping olustur", turkish) is True


def test_english_create_mapping_phrases_route_to_create_not_list():
    for phrase in (
        "create a mapping for the HR.EMPLOYEES table",
        "create mapping for HR.EMPLOYEES table",
        "make a mapping for the SH.SALES table",
        "build a mapping for the HR.JOBS table",
    ):
        assert ai_helpers._is_modeled_tables_question(phrase) is False, phrase


def test_english_create_phrase_excluded_from_missing_flow_question():
    msg = "create a mapping for HR tables missing an ODI flow to DWH"
    assert ai_helpers._is_unmapped_odi_flow_question(msg) is False
    assert ai_helpers._is_unmapped_odi_flow_question("HR tables missing an ODI flow to DWH") is True


def test_turkish_and_list_phrases_route_as_before():
    assert ai_helpers._is_modeled_tables_question("HR tablolarini modellemek istiyorum") is False
    assert ai_helpers._is_modeled_tables_question("mapping olustur HR tablosu") is False
    assert ai_helpers._is_modeled_tables_question("which tables are modeled?") is True
    assert ai_helpers._is_modeled_tables_question("modellenmis tablolari listele") is True
    assert ai_helpers._is_unmapped_odi_flow_question("HR semasinda DWH ODI akisi yapilmamis tablo listesi") is True
