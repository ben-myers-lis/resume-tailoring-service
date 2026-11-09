# Resume Tailoring Service — Portable v1.0.0

The complete workflow is bundled here: original-candidate inputs, tailoring policy, the original 22-category AI keyword reference, assessment, bounded repair, explicit revision, and prompt improvement. It does not require the original ChatGPT account, project memory, or a separate Resume Assessment project.

## Use in another account

1. Open a fresh conversation and attach `Resume_Tailoring_Complete_Prompts_v1.txt`.
2. Supply the original candidate information and current JD using `CHAT_INPUT_TEMPLATE.txt`.
3. Say: “Read the complete attached prompt set. Run NEW_JD in APPLICATION mode using the original candidate facts below. Generate, assess every bullet, repair supported issues, and return the resume with a separate report.”

If the environment cannot read attachments, paste the complete prompt set before the inputs. Do not rely on a document title alone. Use a model/context capacity that can receive the complete policy and actual inputs; a smaller prompt field is not a substitute for loading the package. For later changes say REVISION and identify the draft and scope. For a new JD return to the original facts.

APPLICATION uses supplied/documented evidence only. To test hypothetical work, explicitly select ILLUSTRATIVE. Fictional metrics additionally require explicit authorization and visible notices. The illustrative label stays in the resume even if the detailed report is suppressed.

## Use in an application

Read `docs/APPLICATION_INTEGRATION.md`. The included starter assembles model requests, validates contracts, separates source types, runs draft → assess → bounded repair, and renders Markdown. Supply your own provider adapter and DOCX/PDF renderer. There are no model calls, credentials, account-specific lookups, or deployment side effects in the offline demonstration.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m app.demo
.venv/bin/python -m app.runner examples/request.json --stage normalize
```

On environments with a different virtual-environment command path, use that environment's Python executable. The final command prints the provider-neutral request; it does not contact a model. `examples/` contains wholly synthetic fixtures, not anyone's resume facts.

Programmatic entry:

```python
from app.runner import run_pipeline
result = run_pipeline(request, your_generate_json_adapter)
```

The adapter accepts `instructions`, `input_data`, and `output_schema` and returns a parsed dictionary. Keep candidate/profile/JD/run IDs isolated. The starter returns an internal report regardless of output presentation mode; your UI hides it for RESUME_ONLY while retaining status and required notices.

## Package map

| File or folder | Purpose |
|---|---|
| `Resume_Tailoring_Complete_Prompts_v1.txt` | Self-contained conversation version with policy, workflow, input template, keyword reference, assessment, and maintenance |
| `CHAT_INPUT_TEMPLATE.txt` | Reusable original-facts and JD intake |
| `prompts/00_core.txt` | Authoritative shared policy |
| `prompts/01` through `07` | Normalize, draft, assess, repair, revise, report, and improve prompts |
| `reference/` | 22-category keyword catalog in text/JSON, integrated assessment rubric |
| `schemas/` | Request, normalized evidence, draft, and assessment JSON contracts |
| `app/` | Provider-neutral Python starter, deterministic validation, Markdown renderer, offline demo |
| `examples/` | Synthetic request and expected structural output examples |
| `tests/` | Automated contract checks and semantic evaluation cases |
| `docs/` | Integration, rule traceability, provenance, and change history |
| `sources/` | Original inputs preserved for provenance; not runtime instructions |
| `VALIDATION_REPORT.txt` | Checks actually performed and remaining limits |

## Version authority

`prompts/00_core.txt` is authoritative. Stage prompts implement it; vocabulary supplies possible terms; the rubric assesses compliance. Historical files in `sources/` preserve the original template, assessment pack, and tailoring revision 3. Do not load those archived instructions alongside the current core; older rules such as always filling two pages or barring every illustrative metric have been superseded by the user's later decisions.

`app/build_bundle.py` creates the conversation file from the same canonical policy/reference files, then builds a manifest and ZIP. After a prompt change, update the package version, review semantic cases, run tests, regenerate the bundle, and deliberately select the new version in your application.

## Boundaries

This package makes the workflow portable, not model quality identical. It cannot supply missing facts, prove candidate claims, perform absent web research, or verify pages without rendering. A technically valid JSON response is not proof of a credible resume. The semantic assessor and your model evaluation still matter. The application starter is tested offline; it is not a deployed resume service.
