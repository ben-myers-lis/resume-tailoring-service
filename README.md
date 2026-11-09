# Resume Tailoring Service (v0.5)

A web app for tailoring resumes to job descriptions and tracking job applications for many candidates.
You are the **administrator**: you invite team members, decide what each one can do, and assign
candidates to them. Team members apply to jobs on candidates' behalf and record each application.

It runs on a **mock AI** by default (free, no API key), using your v1 prompt package and all of its
safety checks. Switching to real Claude later is one setting.

## Preview it in the Claude desktop app

Open the Claude desktop app → **Code** tab → new session with the **Local** environment → choose this folder,
then ask: *"Install the requirements and run the app in the preview."* The included `.claude/launch.json`
starts `python3 run.py` and opens **http://localhost:8000** in the Browser pane, where you can click through
the app. (On Windows, change `python3` to `python` in that file.) The port is fixed at 8000 because Google
Drive sign-in returns to that exact address.

## Start it on your computer

Needs **Python 3.12+**. PDFs work out of the box (ReportLab); if **LibreOffice** is installed, it's used instead
for an exact copy of the Word file's layout.

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt   # app + test tools
cp .env.example .env                 # optional settings
python run.py
```

Open **http://localhost:8000** (use this exact address; Google Drive sign-in returns to it). The first visit shows **first-time setup**: create your administrator
account. (Optional: `python manage.py seed-demo you@example.com` loads 8 fictional candidates and jobs.)

## What's in the app

| Page | What you can do |
|---|---|
| **Dashboard** | Applications today / this week, per-day and per-week charts, counts by candidate, and (administrator) by team member; resumes tailored this week; queue size |
| **Candidates** | Search; applications today / this week / total for each candidate; who they're assigned to |
| **Candidate page** | **Paste a job description and tailor** this candidate's resume for it; per-day (30 days) and per-week (12 weeks) application charts and tables; every application with date, job, link, status, who applied; jobs and tailored resumes; work history; assignments |
| **Applications** | Everything applied for in a date range; change status; export CSV |
| **Resume page** | Preview, **Copy link** (Google Drive), **Download PDF / Word**, **Mark as applied**, fit check, questions for the candidate, gaps, the pasted job description, edit job title / company |
| **Prompt Studio** | Draft, improve (with AI or by hand), test and publish new versions of the tailoring prompts |
| **Team** (administrator) | Invite members, set permissions, choose their candidates, deactivate / reactivate |
| **Settings** (administrator) | Connect Google Drive; link sharing, file types, trash-on-delete |
| **My account** | Change password; create an API token for scripts |

### Roles and permissions

- **Administrator:** everything, including **creating and editing candidate profiles**, the Team page,
  assigning candidates and deleting data.
- **Team member:** sees **only candidates assigned to them**. Every member can view their candidates
  and **mark jobs as applied** / update application status. The administrator can also grant:
  - **Add job descriptions**
  - **Tailor & download resumes** (includes pasting job descriptions)
  - **Manage prompts**: use the Prompt Studio to draft and test changes (only the administrator publishes)

### Inviting a team member

Team → **Invite team member** → enter their email, tick permissions and candidates → **Create invitation**.
Copy the link and send it to them yourself (the app doesn't send email yet). The link works once and
expires after 7 days. They choose their name and password, then they're in.

Deactivating a member signs them out everywhere immediately. Changing permissions or assignments
takes effect on their next click.

### Tailoring a resume

Open the candidate, paste the job description into **Tailor … for a job** (plain text, exactly as posted)
and click **Tailor resume**. The job title and company are detected from the text; you can type them in
the optional fields or fix them later with **Edit title / company** on the resume page. They're only used
for labels, file names and application records. The tailoring always reads the full pasted text.

You land on the resume page, which fills in when the resume is ready (a few seconds on the mock AI;
a few minutes with Claude and the full v1 prompts). Jobs that are a clear mismatch for the candidate's
titles, years and seniority are skipped for free; tick **Tailor even if the fit looks low** to override.

### Recording applications

After submitting a resume on a job site, click **Mark as applied** on the resume page (or **+ Log application**
on the candidate page for a job applied to without tailoring here). The date defaults to today; you can backdate it.
"Today" and "this week" (Monday–Sunday) use the time zone in `RTS_TIMEZONE` (default America/New_York).

### Prompt Studio

The prompts that write and check resumes are versioned. Live resumes always use the **active** version,
and each resume records which version produced it.

1. **New draft**: copy the active version (or any earlier one). Live resumes are unaffected.
2. **Improve with AI**: describe what should change in plain words, e.g. *"summaries are too long; lead
   with leadership for senior roles"*. The AI proposes small before/after edits, optionally after reading
   resumes from your latest test round. Accept, adjust or reject each edit. Or use **Edit files** directly.
3. **Test**: run your saved test set (each case is a candidate plus one of the jobs tailored for them). Each case is tailored with the draft and,
   side by side, with the active version. Test resumes stay out of candidates' records and statistics.
4. **Publish** (administrator only): allowed once the structure checks pass and a test round on the
   *current* draft finished without failures. Changes to **protected safety rules** (P02 fresh starts,
   P03 evidence and claims, P04 illustrative labelling, P06 history and identity, P12 metrics, and the
   fact-check step) are flagged and need your explicit confirmation.
5. **Restore**: make any earlier version active again with one click.

Output formats and the code-level checks can't be changed from the Studio, so even a badly edited prompt
can't make the app deliver resumes that fail the checks. With the mock AI the Studio's workflow works,
but proposals and test resumes are placeholders: switch to Claude to judge real quality.
Cost with Claude: about $0.15–0.25 per AI proposal (Opus by default, `RTS_PROMPT_MODEL`), plus the
normal cost of each test resume.

### Google Drive (resume links)

Every finished resume is uploaded automatically to **your** Google Drive, in a **Resume Tailoring** folder
with one subfolder per candidate (files named `Candidate_Company_Job.pdf`). Team members get two buttons on
each resume: **Copy link** (the Drive link) and **Download**. If an upload fails, the resume still works and a
**Retry upload to Drive** button appears. Test resumes from the Prompt Studio are never uploaded.

The app uses Google's `drive.file` permission: it can only see and manage files it created. The rest of your
Drive stays private.

**One-time setup (about 10 minutes):**
1. Open [Google Cloud Console](https://console.cloud.google.com/) with the Google account whose Drive should hold
   the resumes, and create a project (e.g. "Resume Tailoring").
2. **APIs & Services → Library**: enable **Google Drive API**.
3. **OAuth consent screen** (Google Auth Platform): app name and your email; audience **External** (or
   **Internal** if you use Google Workspace). Then **publish** the app ("In production"). In Testing mode Google
   signs the app out every 7 days.
4. **Credentials → Create credentials → OAuth client ID → Web application.** Under **Authorized redirect URIs**
   add `http://localhost:8000/api/google/callback` (or `https://your-domain/api/google/callback` once online;
   the exact value is shown on the Settings page).
5. Put the Client ID and secret in `.env` and restart:
   ```
   RTS_GOOGLE_CLIENT_ID=....apps.googleusercontent.com
   RTS_GOOGLE_CLIENT_SECRET=...
   ```
6. In the app: **Settings → Connect Google Drive**, choose your account and allow access.

Options (Settings page): upload automatically on/off; who can open links: **anyone with the link** (default;
works for recruiters and job sites) or **only the candidate's assigned team members** (they must use a Google
account with the same email); upload PDF, Word or both; move a candidate's folder to the Drive trash when you
delete the candidate.

Note: most job-application forms ask for a file upload, not a link. Use **Download** for those, and **Copy link**
for email, messages or sites that accept a URL.

### What resumes contain

With title-only profiles, resumes contain the summary, titles, companies, dates and education, plus
**questions to ask the candidate**. Experience bullets appear only where someone filled in
*"What they did here"* for a job, in the candidate's own words. Nothing is invented.

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `RTS_AI_PROVIDER` | `mock` | `mock` (free, simulated) or `claude` |
| `ANTHROPIC_API_KEY` | | Needed for `claude` |
| `RTS_MIN_FIT` | `40` | Skip tailoring below this fit score (only clear mismatches) |
| `RTS_WORKERS` | `4` | Resumes processed at the same time |
| `RTS_TIMEZONE` | `America/New_York` | For "today" and "this week" |
| `RTS_PDF` | `1` | Also make PDFs and check page count |
| `RTS_PDF_ENGINE` | `auto` | `libreoffice` (exact copy of the Word file), `reportlab` (no system packages; used on Vercel), or `auto` (LibreOffice if installed) |
| `RTS_SECURE_COOKIES` | `0` | Set to `1` when the site runs on HTTPS |
| `RTS_ALLOW_ILLUSTRATIVE` | `0` | Keep off: illustrative mode generates example content |
| `RTS_PROMPT_MODEL` | `claude-opus-5-5` | Model for "Improve with AI" proposals |
| `RTS_GOOGLE_CLIENT_ID`, `RTS_GOOGLE_CLIENT_SECRET` | | Google Drive connection (see above) |
| `RTS_PUBLIC_URL` | `http://localhost:8000` | The address people use to open the app (set it when hosting online) |
| `RTS_PORT` | `8000` | Web address port |
| `RTS_DATABASE_URL` (or `DATABASE_URL`) | | A Postgres URL. Empty = SQLite file `RTS_DB`. Required on Vercel |
| `RTS_DB_SCHEMA` | | Optional Postgres schema, to keep separate data sets (e.g. `preview`) in one database |
| `RTS_FILE_STORE` | `local` (`db` with Postgres) | Where generated resumes are kept: a folder (`RTS_FILES`) or the database |
| `RTS_QUEUE` | `threads` (`vercel` on Vercel) | Who processes runs: worker threads in the app, or Vercel Queues |
| `RTS_RUN_TIMEOUT` / `RTS_MAX_ATTEMPTS` | `900` / `2` | A run still RUNNING after this many seconds is retried, up to this many tries |
| `CRON_SECRET` | | Protects `/api/cron/sweep` (Vercel sends it automatically) |

## Admin commands

```bash
python manage.py create-admin "Your Name" you@example.com   # same as the first-run setup page
python manage.py reset-password someone@example.com         # if anyone forgets a password
python manage.py seed-demo you@example.com                  # fictional demo data
```

## Switching to real Claude

1. Create an API key (tied to a workspace) at platform.claude.com, with a spend limit.
2. In `.env`: `RTS_AI_PROVIDER=claude` and `ANTHROPIC_API_KEY=sk-ant-...`
3. Restart `python run.py`. Each resume's report then shows real tokens, cost and time per step.

Candidate names and contact details are not sent to Claude; they're added back when rendering.

## Project layout

| Path | What it is |
|---|---|
| `service/web/` | The web app (HTML, CSS, JavaScript; no build step) |
| `service/api.py` | All routes, with sign-in and permission checks |
| `service/auth.py` | Passwords, permissions, access rules |
| `service/db.py` | Database (SQLite or Postgres, same queries) |
| `service/jobs.py`, `rts_worker.py` | Run dispatch: worker threads, or Vercel Queues + its subscriber |
| `service/storage.py` | Generated files on disk or in the database |
| `main.py`, `pyproject.toml`, `vercel.json` | Vercel entry point and configuration |
| `service/stats.py` | Per-day / per-week application counts |
| `service/jd.py` | Job title / company detection from pasted text |
| `service/gdrive.py`, `drive_service.py` | Google Drive connection and uploads |
| `service/prompts.py`, `prompt_ai.py` | Prompt versions, checks, diffs, AI proposals |
| `service/screening.py` | Fit screening for title-only profiles |
| `service/pipeline.py`, `worker.py` | Background tailoring queue |
| `service/render.py`, `service/fonts/` | Word/PDF output (LibreOffice or ReportLab with the Carlito font, OFL) and page-count check |
| `service/ai/` | Mock AI and Claude provider |
| `prompt_package/` | Your v1 prompt package (becomes Prompt Studio version 1 on first start) plus the Claude adapter |
| `tests/` | End-to-end tests, including a simulated Google Drive and Vercel Queues (`python -m pytest -q tests`; set `RTS_TEST_DATABASE_URL` to run them on Postgres) |

## Security notes

Passwords are stored as salted PBKDF2 hashes. Sessions use HttpOnly cookies; changes require a
same-site request header. Sign-in attempts are rate-limited. Members can't see or even confirm the
existence of candidates that aren't assigned to them.

## Deploying

See **[DEPLOY.md](DEPLOY.md)**: Vercel (serverless, with Postgres and Vercel Queues) or any Docker host,
settings, Google Drive on the live site, backups.

## Before going online

- Host behind **HTTPS**, set `RTS_SECURE_COOKIES=1` and `RTS_PUBLIC_URL=https://your-domain`, and add the
  matching redirect URI in Google Cloud.
- Back up `data/` (database and resume files) and keep `.env` private.
- For several servers or heavy use, move from SQLite to Postgres (only `service/db.py` changes).
- Decide a data-retention policy. Deleting a candidate removes their resumes and application records.
- Optional next: email sending for invitations and password resets.
