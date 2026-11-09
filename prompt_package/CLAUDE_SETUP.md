# Running this prompt set with Claude

Added files (the prompts, schemas and runner are unchanged):

| File | Purpose |
|---|---|
| `app/claude_adapter.py` | Implements the package's `generate_json` contract with the Claude API |
| `run_claude.py` | Runs one request or a folder of requests; writes the result, the resume and a cost/time report |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt anthropic
export ANTHROPIC_API_KEY=sk-ant-...        # never commit or share this key
.venv/bin/python run_claude.py examples/request.json --out results/
```

Each run writes these files to `results/`:
- `<run_id>.resume.md`: the tailored resume (withheld if blocking findings remain)
- `<run_id>.result.json`: draft, assessment, coverage, gaps and the full report
- `<run_id>.usage.json`: tokens, cost (USD) and seconds for every model call
- `summary.csv`: one row per request with status, findings, cost and time

## Models per stage (edit `STAGE_MODELS` in `app/claude_adapter.py`)

| Stage | Default | Notes |
|---|---|---|
| NORMALIZE | claude-haiku-5-5 | Extraction with exact quotes |
| DRAFT / REVISION / REPAIR | claude-sonnet-5-5 | The writing |
| ASSESS | claude-sonnet-5-5 | Main safety gate. To test a cheaper option, run with `--assess-model claude-haiku-5-5` |

## Built-in behaviour
- **Personal details:** the candidate's name and contact details are not sent to Claude. They are added back when the resume is rendered. Strip them from free-text sources at ingestion if they appear there.
- **Prompt caching:** the core policy, rubric and keyword catalog are sent first and marked cacheable.
- **Structured output:** uses forced tool use, validated against the package schemas, with one automatic retry on a schema error. If your account rejects forced tool use, add `--no-force-tool`.
- **Long outputs:** responses are streamed. If a stage hits its token limit, the run stops with a clear error; raise `STAGE_MAX_TOKENS` for that stage.

Tested here with a stand-in client only (no live API access in this environment).
