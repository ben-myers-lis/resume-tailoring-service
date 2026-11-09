# Running the title-only test (about 15 minutes)

The test runs 8 fictional candidates (titles, companies and dates only) through your full v1 pipeline on Claude.
**Expected cost for all 8: about $1–2.** Expected time: about 1–2 minutes per candidate.

| File | Candidate | Job | What we're checking |
|---|---|---|---|
| case_01 | 7 yrs ML titles | Senior ML Engineer | Strong match |
| case_02 | SWE then AI Engineer | LLM Application Engineer | Strong match |
| case_03 | 4 yrs data science | Staff ML Engineer (8+ yrs) | Stretch: seniority gap reported |
| case_04 | Backend / SRE, AWS cert | AI Platform Engineer | Adjacent experience |
| case_05 | Data analyst | ML Engineer | Transferable stretch |
| case_06 | Accountant | PhD Applied Scientist | Clear mismatch |
| case_07 | Overlapping roles + gap | Lead AI Engineer | Correct tenure counting |
| case_08 | Intern + 1 yr | Junior AI Engineer | Junior profile |

All runs use APPLICATION mode: nothing is invented, so expect short resumes (titles, dates, education,
supported summary) plus a list of questions to ask each candidate.

## 1. Get an API key
1. Go to https://console.anthropic.com and sign up.
2. **Billing:** add a payment method and a small spending limit (e.g. $20).
3. **API keys → Create key.** Copy it. Keep it private; don't email it or paste it into chat.

## 2. Install (one time)
You need Python 3.10 or newer (check with `python3 --version`). In a terminal, inside this folder:

**Mac / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt anthropic
export ANTHROPIC_API_KEY=paste-your-key-here
```

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt anthropic
$env:ANTHROPIC_API_KEY="paste-your-key-here"
```

## 3. Check the connection with one candidate
```bash
python run_claude.py test_cases/case_01_strong_mle.json --out results/
```
You should see a line like `case_01_strong_mle.json: READY  $0.15  80s  (3 calls)`.

## 4. Run all 8
```bash
python run_claude.py test_cases/ --out results/
```

## 5. Send back the results
Zip the `results` folder and send it. It contains, for each candidate:
- `*.resume.md`: the tailored resume
- `*.result.json`: assessment, gaps and questions for the candidate
- `*.usage.json`: tokens, cost and seconds for each step
- `summary.csv`: one line per candidate (status, cost, time)

Results include the fictional candidates' names, so they're safe to share.

## If something goes wrong
| Message | Fix |
|---|---|
| `AuthenticationError` | The API key wasn't set in this terminal. Repeat the `export` / `$env:` line. |
| `tool_choice` or `thinking` error | Add `--no-force-tool` to the command. |
| `output hit max_tokens` | Send me the message; I'll raise the limit. |
| `ContractError: ...` | Your package's safety checks rejected an output. That's useful test data: keep it in the results. |
| Rate limit (429) errors | New accounts have low limits. Run one case at a time, or wait a minute and rerun. |
