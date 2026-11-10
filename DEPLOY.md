# Deploying Resume Tailoring

There are two ways to run the app online:

- **Vercel** (section 3c): serverless. Data lives in a Postgres database, tailoring runs through
  Vercel Queues, and PDFs are drawn with ReportLab. Nothing to keep running or patch.
- **A Docker host** (sections 3a, 3b): one container with a **persistent data folder** (`/app/data`:
  the SQLite database, generated resumes and Prompt Studio history).

The same code runs both ways; settings decide which (see the table in README).

> **Before you start:** make the GitHub repository **private** (Settings → General → Danger Zone →
> Change visibility). It contains your prompt set and business logic.

## 1. Choose where to host

| Option | Effort | Pricing model | Notes |
|---|---|---|---|
| **Render** (Web Service + Disk) | Lowest | Fixed monthly plan + disk | Deploys from GitHub on every merge; HTTPS and domain included |
| **Railway** (service + Volume) | Low | Usage-based | Deploys from GitHub; HTTPS included |
| **Fly.io** (app + Volume) | Medium | Usage-based | Command-line setup; HTTPS included |
| **Your own server / VM** (DigitalOcean, Lightsail, Hetzner…) | Medium–high | Fixed monthly server | `docker compose` + an HTTPS proxy (Caddy); you handle updates and backups |
| **Vercel** (Pro) + Neon Postgres | Low | Pro plan + usage, plus the database | Deploys from GitHub; preview site for every pull request; see 3c |

For an app this size, expect a small monthly bill on any of these; check each provider's current pricing.
For a **Docker host** (not Vercel):
- Run **exactly one instance** (the job queue and SQLite database live inside the container).
  Process more resumes at once with `RTS_WORKERS`, not more instances.
- Attach a **persistent disk/volume mounted at `/app/data`**. Without it, everything is lost on redeploy.
- Give the container **at least 1 GB RAM** (LibreOffice PDF conversion).

## 2. Settings

Copy `.env.production.example` and fill it in. On Render/Railway/Fly, enter these as environment variables
in the dashboard instead of a file. The important ones:

| Setting | Value |
|---|---|
| `RTS_PUBLIC_URL` | Your HTTPS address, e.g. `https://resumes.yourcompany.com` |
| `RTS_SECURE_COOKIES` | `1` |
| `RTS_FORWARDED_ALLOW_IPS` | `*` (your host's proxy handles HTTPS) |
| `RTS_SETUP_CODE` | A long random value. It's needed once to create the administrator account, so strangers can't claim it |
| `RTS_AI_PROVIDER` / `ANTHROPIC_API_KEY` | `claude` and your key (or `mock` to try it first) |
| `RTS_GOOGLE_CLIENT_ID` / `RTS_GOOGLE_CLIENT_SECRET` | If you use Google Drive |

The port is picked up automatically from the host's `PORT` variable (default 8000).

## 3a. Render (example of a managed host)

1. Render dashboard → **New → Web Service** → connect the GitHub repo → it detects the `Dockerfile`.
2. Instance type with ≥ 1 GB RAM. **Add Disk**: mount path `/app/data`, 1–5 GB.
3. Add the environment variables from step 2. Health check path: `/api/health`.
4. Deploy. Add your domain under **Settings → Custom Domains** (HTTPS is automatic).

Railway and Fly.io are similar: deploy from the repo/Dockerfile, add a volume at `/app/data`, set the variables.

## 3b. Your own server

On an Ubuntu server with Docker installed and a domain pointing to it:

```bash
git clone https://github.com/<you>/resume-tailoring-service.git && cd resume-tailoring-service
cp .env.production.example .env && nano .env        # fill in the values
docker compose up -d --build
```

The container listens on `127.0.0.1:8000` only. Put HTTPS in front with Caddy (automatic certificates):

```
# /etc/caddy/Caddyfile
resumes.yourcompany.com {
    reverse_proxy 127.0.0.1:8000
}
```

`sudo systemctl reload caddy`. Update later with `git pull && docker compose up -d --build`.

## 3c. Vercel

**Plan:** the free **Hobby** plan works for non-commercial projects. It stops any function after
5 minutes, and the fast v2 pipeline, which is active on new installations, takes about 11–25 seconds per
resume. Use **Pro** if the project is commercial (Hobby doesn't allow commercial use), or if you
publish the full v1 pipeline (3–6 minutes per resume).

On Hobby, watch the included limits in the **Usage** tab. Going over pauses the site until usage in
the rolling 30 days falls back. At ~1,500 resumes a week, the one to watch is **Active CPU** (4 hours
a month). The free Neon database holds 1 GB, roughly two months of resumes at that volume, because the
Word and PDF files are stored in it.

1. **Import the repo.** Vercel dashboard → **Add New → Project** → import `resume-tailoring-service`.
   Keep the defaults (no build command); Vercel reads `pyproject.toml` and `vercel.json`. The
   `Dockerfile` is ignored (`.vercelignore`).
2. **Add the database.** In the project: **Storage → Create Database → Neon (Postgres)** → connect it to
   the project. This sets `DATABASE_URL`; the app creates its tables on first start.
3. **Environment variables** (Settings → Environment Variables, for Production):

   | Variable | Value |
   |---|---|
   | `RTS_SETUP_CODE` | A long random value (needed once to create the administrator) |
   | `CRON_SECRET` | A random value of 16+ characters (protects the daily clean-up job) |
   | `RTS_AI_PROVIDER` / `ANTHROPIC_API_KEY` | `claude` and your key (or `mock` to try it first) |
   | `RTS_PUBLIC_URL` | Your domain once you add one, e.g. `https://resumes.yourcompany.com`. Until then it defaults to the project's `…vercel.app` address |
   | `RTS_GOOGLE_CLIENT_ID` / `RTS_GOOGLE_CLIENT_SECRET` | If you use Google Drive |
   | `RTS_WORKERS` | Resumes processed at once (default 4); keep within your Anthropic rate limits |

   Not needed on Vercel: `RTS_SECURE_COOKIES`, `RTS_FORWARDED_ALLOW_IPS`, `RTS_DB`, `RTS_FILES`, `RTS_PORT`.
4. **Time limit (Pro only, and only for the v1 pipeline).** Settings → **Functions** → Advanced Settings →
   **Function Max Duration** → `800` → Save. Skip this on Hobby.
   This applies to the queue function that does the tailoring. (`vercel.json` keeps web requests at 300 s.)
5. **Deploy**, then open `https://<your-app>/api/health`. It should show
   `"queue": "vercel", "database": "postgres", "files": "db"`.
6. Continue with **First start** (section 4) and **Google Drive** (section 5).

**How it works on Vercel**
- Creating a run saves it in Postgres and sends a message to **Vercel Queues** (topic `rts-runs`).
  Vercel calls `rts_worker.py` (a private function built from `pyproject.toml`) once per run.
- Word and PDF files are stored in the database (they're small), so any instance can serve a download.
- Safety net: a run whose message was lost is re-sent after 2 minutes; a run stopped by the time limit is
  retried after `RTS_RUN_TIMEOUT` (15 min) and marked FAILED after `RTS_MAX_ATTEMPTS` (2) tries. This check runs
  when someone opens the dashboard or a run page, and once a day from the cron job in `vercel.json`. On Pro
  you can make the cron more frequent (e.g. `*/10 * * * *`).
- **Preview deployments** (one per pull request) are separate sites. If they share the production
  database, set `RTS_DB_SCHEMA=preview` for the **Preview** environment so they use their own tables
  (or turn on Neon's preview branching when connecting the database).
- Vercel Queues' Python SDK is in **beta**. If you ever need to move off Vercel, the same code runs on a
  Docker host (3a/3b), and can keep using the Postgres database by setting `RTS_DATABASE_URL`.

**Backups:** Neon keeps point-in-time history (its length depends on the Neon plan); check it in the Neon
console. Resumes uploaded to Google Drive are also kept there.

## 4. First start

1. Open your HTTPS address, enter the **setup code**, and create your administrator account.
   (Alternatively run `python manage.py create-admin "Your Name" you@example.com` inside the container.)
2. Optional: remove `RTS_SETUP_CODE` afterwards. Setup is closed once an administrator exists.

## 5. Google Drive on the live site

In Google Cloud → Credentials → your OAuth client, add the redirect URI
`https://resumes.yourcompany.com/api/google/callback` (exactly `RTS_PUBLIC_URL` + `/api/google/callback`).
Then Settings → **Connect Google Drive** in the app. Make sure the consent screen is **published** ("In production").

## 6. Backups

On a Docker host, everything important is in `/app/data`. Back it up daily:
- **Managed hosts:** enable disk snapshots/backups in the dashboard if offered.
- **Own server:** e.g. a nightly cron job:
  ```bash
  docker run --rm -v resume-tailoring-service_rts-data:/data -v /backups:/backup alpine \
    tar czf /backup/rts-$(date +%F).tgz -C /data .
  ```
Resumes uploaded to Google Drive are also kept there.

## 7. Updating

Changes arrive as pull requests in the repo. GitHub runs the tests automatically (green check = passed).
After you merge into `main`: managed hosts redeploy automatically; on your own server run
`git pull && docker compose up -d --build`. Data on the volume is kept.

## Checklist

- [ ] Repository is private
- [ ] Docker host: one instance, persistent volume at `/app/data`, ≥ 1 GB RAM;
      `RTS_PUBLIC_URL` (https), `RTS_SECURE_COOKIES=1`, `RTS_FORWARDED_ALLOW_IPS=*`, `RTS_SETUP_CODE`
- [ ] Vercel: Hobby (non-commercial, v2 active) or Pro; Neon database connected, `RTS_SETUP_CODE`, `CRON_SECRET`,
      `/api/health` shows `vercel` / `postgres` / `db`
- [ ] Administrator account created
- [ ] Claude API key set and spend limit configured
- [ ] Google redirect URI added for the live address; Drive connected
- [ ] Backups enabled
