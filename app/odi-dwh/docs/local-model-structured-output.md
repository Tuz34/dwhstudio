# Local Model Structured Output Plan

This note records the offline structured-output work for ODI-62. It is a
developer/operator reference, not a runtime configuration change.

## Current Artifacts

### `--intent-schema-guard`

Purpose: score already-captured local model predictions more fairly when an
`intent_to_json` answer is wrapped in a single top-level envelope such as
`result`, `request`, `data`, `payload`, or `intent`.

Boundary:

- It is opt-in.
- It is scoring-only.
- It unwraps only one whole-response object.
- It does not extract JSON from prose.
- It does not use golden expected answers to decide the transformation.

### `--intent-schema-out`

Purpose: write a static JSON Schema-style contract for the intent JSON shapes
accepted by the offline validator.

Supported operation families:

- `CREATE`
- `UPDATE`
- `DELETE`
- `VALIDATE`

Boundary:

- Static/offline artifact only.
- No model call.
- No runtime enforcement.
- No case-specific golden answers.

### `--intent-grammar-out`

Purpose: write a static GBNF grammar artifact for constrained-decoding
experiments.

Boundary:

- Static/offline artifact only.
- Generated from the schema contract family.
- Field order is fixed and intentionally narrower than free JSON.
- `UPDATE.changes` currently permits one static-eval change field per generated
  object.
- This does not enable any runtime grammar enforcement by itself.

## Operator Safety Boundary

- No GPU use.
- No model load or inference.
- No model download or fine-tune.
- No DB or ODI action.
- No WebLogic, Docker, Task Scheduler, or service change.
- No raw rows, key values, secrets, or credential material.
- No Git or Gitea workflow.

## Usage

Generate the schema contract:

```powershell
.\.venv\Scripts\python.exe local_model_eval.py --intent-schema-out artifacts\intent_schema.json
```

Generate the grammar contract:

```powershell
.\.venv\Scripts\python.exe local_model_eval.py --intent-grammar-out artifacts\intent_grammar.gbnf.json
```

These commands only write local files. They do not contact LM Studio, Ollama,
OpenAI-compatible endpoints, DB, or ODI.

## Review Rules

Reviewers should check:

- The artifact is static and offline.
- The artifact does not include held-out expected answers.
- The artifact does not add a new operation family.
- The artifact is narrower than or equal to the validator's accepted task
  family.
- The report does not imply that a live model run has already verified grammar
  behavior.

## RAG Semantic Planning

RAG semantic planning should stay as a separate design slice before any runtime
retrieval change.

First design slice:

- Define which redacted collections are eligible for semantic retrieval.
- Define which task routes may use each collection.
- Define the eval-set score that must improve before replacing keyword
  retrieval.
- Define a no-raw-data embedding rule.
- Define a rollback path to the existing JSON keyword retrieval.

Implementation boundary for the first slice:

- Design and acceptance criteria only.
- No Chroma runtime mutation.
- No embedding job.
- No model call.
- No DB or ODI access.
