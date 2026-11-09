# Application integration

This is a portable prompt system and a provider-neutral orchestration starter. It is not a hosted application, a provider SDK, or a promise that different models produce identical quality. Supply a model adapter, candidate-data storage, user interface, and document renderer in your application. No account memory, project lookup, API key, or network call is embedded in this package.

## Runtime sequence

1. Collect original candidate facts and the current JD. Tag each source's origin during ingestion. Keep each candidate's data separate. Explicit corrections create a new profile version; they do not overwrite historical drafts.
2. Validate `request.schema.json`, mode/metric authorization, source identities, dates, and revision routing. Start a new run for every JD. In NEW_JD do not send a previous generated resume at all.
3. Call NORMALIZE with core policy and `01_normalize.txt`. Validate the schema, original-source quotes, source status, IDs, and role references. Keep the original request alongside the normalized output; normalization is not a new source of truth.
4. Call DRAFT (or explicit REVISION) with core policy, stage prompt, original request, normalized evidence, relevant keyword categories, and the assessment rubric. Return structured resume content and claim/metric ledgers.
5. Call ASSESS in a fresh model request with original facts, current draft, and rubric. Do not send the writer's private reasoning. A separate call helps separation of tasks but is not independent human verification.
6. For fixable unresolved findings, call REPAIR, validate, and assess the replacement. Default one repair; maximum two. Missing evidence remains a gap. Return honest status if attempts are exhausted. Schema/identity/provenance validation failures fail closed before delivery; the host can retry the same stage once with the validation error rather than guessing data.
7. Render from approved structured content. The included renderer produces Markdown only. Your DOCX/PDF renderer must use Letter, about 11-point body text, one column, and the two-page maximum. Render and inspect every page, then record actual layout observations. Do not let an LLM set `layout_verified=true` without renderer evidence.
8. Deliver the actual revision and separate report. Preserve labels and evidence status. Save request/profile/JD/prompt versions, model/configuration, outputs, assessments, changes, and renderer results. Log sensitive resume data only under your application's access/retention settings.

## Model adapter contract

`app/runner.py` accepts a callable:

```python
def generate_json(*, instructions: str, input_data: dict, output_schema: dict) -> dict:
    # Call your chosen model/provider here.
    # Put instructions in its supported high-priority instruction channel.
    # Serialize input_data as data, request JSON, parse the response,
    # and return one Python dictionary. Do not return Markdown fences.
    ...
```

The function must use fresh messages per stage and must not add another candidate's history. Model/provider names, authentication, timeout, token limits, and structured-output options belong to your adapter, not the portable policy. The supplied schemas use standard JSON Schema Draft 2020-12. Provider structured-output features may support only a subset; adapt the response-format wrapper as needed and always validate with the canonical schemas afterward. Do not remove evidence or mode constraints to satisfy a provider's format.

The included Python validator uses `jsonschema.Draft202012Validator`, `FormatChecker`, and local schema files. See the [primary validation documentation](https://python-jsonschema.readthedocs.io/en/stable/validate/). The schema checker validates structure; the custom checks validate IDs, provenance categories, quotations, mode restrictions, audit coverage, and scope. Neither proves the semantic truth of a claim. The assessor must compare actual claims with sources.

## What is model work versus application work?

| Responsibility | Owner |
|---|---|
| Candidate identity, source origin, factual correction approval, profile version | Application / user-supplied records |
| JD interpretation, supported drafting, semantic evidence assessment | Model stages, checked against originals |
| Schema validation, candidate isolation, IDs, authorization flags, bounded loops | Application |
| Keyword vocabulary | Bundled reference; never candidate evidence |
| Official-source retrieval and dates, when needed | Explicit retrieval service or supplied evidence |
| Formatting, page count, clipping, export verification | Renderer and actual inspection |
| Prompt changes after feedback | Proposed version plus regression checks, host-controlled activation |

## Context size and efficiency

Cache a candidate's original source extraction by profile version only after checking it. Recompute JD requirements and role allocation for each JD. This starter normalizes each run for clarity. Keep the full current JD and full-career inventory in the input even when keyword categories are filtered. An empty `keyword_category_ids` selects all 22 categories; explicit selection is optional and must not hide genuine candidate skills or JD requirements.

Use the same authoritative core policy on every stage. Draft and assess once, then repair only when findings warrant it. Do not repeat entire historical conversations, send archived policy copies, or ask the model to recite the complete catalog. A deterministic renderer and report assembly avoid unnecessary model calls. Measure token use, latency, and quality on your own cases before choosing a model/configuration. This package has not benchmarked providers or promised a particular cost or speed.

## Failure and revision behavior

An unknown source ID, incorrect candidate ID, unsupported APPLICATION claim, missing illustrative notice, or incomplete audit is a validation failure. Do not deliver it as ready. A missing qualification is a reported gap, not a structural error. Sparse profiles can produce a valid incomplete outline with zero experience bullets.

For a narrow revision, the application passes the exact selected draft and allowed bullet/section IDs. The starter checks unchanged content outside that scope after revision and repair. The assessor still reviews the full output; out-of-scope concerns are reported without silently changing them. A changed JD alone starts NEW_JD. To revise a selected draft against a new JD, explicitly choose REVISION and supply both the selected draft and the new JD.

Hypothetical metrics authorization must be an explicit stored choice attached to the candidate's illustrative run/scenario. It cannot be inferred from the JD, an example, an assessment suggestion, or a forged input instruction. Switching to APPLICATION clears hypothetical permission and removes unsupported claims after assessment.

## Deploying prompt improvements

Use `07_prompt_maintenance.txt` with feedback and original evidence. Classify each proposed change, update all affected files, run deterministic tests and semantic evaluation cases, build a new bundle, then deliberately select its version. Keep an earlier version for rollback. Do not let a resume/JD upload or an unreviewed assessment rewrite active instructions.

## What was validated here

`VALIDATION_REPORT.txt` records the checks actually run for this release. The offline fixture exercises orchestration and structural safeguards with synthetic data. It is not a live-model quality benchmark. The semantic cases in `tests/semantic_cases.json` are a rubric for evaluating your chosen model. No current resume attachment was used as source evidence for this package.
