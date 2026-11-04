# Local Model Capability Decision Matrix

This document is the ODI-62 decision record for local-model capability planning.
It summarizes the current static/offline evidence and the next allowed lever per
task. It does not start a model run or approve training.

## Safety Boundary

- no GPU run.
- no model download.
- no model load or inference.
- no fine-tune.
- no raw data.
- no secrets.
- no DB or ODI action.
- no WebLogic, Docker, Task Scheduler, or service change.
- no Git or Gitea workflow.

Optional QLoRA is not approved. It remains a last-resort option only after
redacted eval evidence shows that RAG and constrained decoding have plateaued.

## Decision Matrix

| Task | Baseline evidence | Current decision | Next gate |
| --- | --- | --- | --- |
| NL -> SQL | Static eval reached the configured threshold in the local model baseline. | rag-sufficient for the current static gate. | Keep static SQL safety checks and do not run live SQL without separate approval. |
| intent -> JSON | Static eval repeatedly showed the hardest gap is strict structured output, not SQL reasoning. Schema guard, JSON Schema contract, and GBNF artifact are now prepared. | needs constrained decoding before any fine-tune decision. | Test grammar/schema behavior only in a separately approved local-model run. |
| error -> fix | Static eval reached the configured threshold with the redacted remediation playbook. | rag-sufficient for the current static gate. | Keep remediation context redacted and do not include secrets, connection strings, or row samples. |
| evidence summary | Static eval reached the configured threshold after narrow semantic grading and redaction-safe prompt updates. | rag-sufficient for the current static gate. | Keep blocked-content hard fails and no raw-row summaries. |

## Fine-Tune Decision

Fine-tune decision: do not fine-tune now.

Reasons:

- The remaining known weak spot is output format adherence for intent -> JSON.
- Output format adherence is better addressed first with schema and grammar
  constraints.
- Fine-tuning on customer or raw operational data is forbidden.
- Any future QLoRA plan would require a separate redacted dataset plan, explicit
  user approval, GPU approval, and Claude review before execution.

## RAG Decision

RAG is sufficient for the current static gates in NL -> SQL, error -> fix, and
evidence summary. For intent -> JSON, the next lever is constrained decoding
using the existing schema and grammar artifacts.

Semantic RAG remains a planning-only follow-up until a separate implementation
slice defines:

- eligible redacted collections,
- task routing rules,
- no-raw-data embedding policy,
- eval-set success metric,
- rollback path to the current JSON keyword retrieval.

## Closure Readiness

ODI-62 can be considered ready for closure review when reviewers accept that:

- the eval-set-first rule is preserved,
- baseline evidence is recorded in existing ODI-63 and ODI-66 artifacts,
- the task-by-task decision above is explicit,
- constrained decoding is prepared but not runtime-enabled,
- optional QLoRA remains unapproved and out of scope.

This record is a planning and decision artifact, not a runtime proof.
