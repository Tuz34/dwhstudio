from pathlib import Path

import local_model_eval as eval_safety


def test_odi62_capability_decision_matrix_covers_all_tasks_and_boundaries():
    path = Path("docs/local-model-capability-decision.md")
    text = path.read_text(encoding="utf-8")
    lower = text.lower()

    for task in [
        "NL -> SQL",
        "intent -> JSON",
        "error -> fix",
        "evidence summary",
    ]:
        assert task in text

    for required in [
        "current decision",
        "baseline evidence",
        "rag-sufficient",
        "needs constrained decoding",
        "fine-tune decision",
        "no gpu run",
        "no model download",
        "no raw data",
        "no secrets",
        "no db or odi action",
    ]:
        assert required in lower

    assert "optional qlora is not approved" in lower
    assert "intent -> JSON" in text and "grammar" in lower
    assert "live-proven" not in lower
    eval_safety.assert_no_blocked_content(text)
