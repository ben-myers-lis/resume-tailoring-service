# Deploying Resume Tailoring

The app is one Docker container with a **persistent data folder** (`/app/data`: the database, generated
resumes and Prompt Studio history). Any host that runs Docker containers with a persistent disk works.

> **Before you start:** make the GitHub repository **private** (Settings → General → Danger Zone →
> Change visibility). It contains your prompt set and business logic.

## 1. Choose where to host

| Option | Effort | Pricing model | Notes |
|---|---|---|---|
| **Render** (Web Service + Disk) | Lowest | Fixed monthly plan + disk | Deploys from GitHub on every merge; HTTPS and domain included |
| **Railway** (service + Volume) | Low | Usage-based | Deploys from GitHub; HTTPS included |
| **Fly.io** (app + Volume) | Medium | Usage-based | Command-line setup; HTTPS included |
| **Your own server / VM** (DigitalOcean, Lightsail, Hetzner…) | Medium–high | Fixed monthly server | `docker compose` + an HTTPS proxy (Caddy); you handle updates and backups |

For an app this size, expect a small monthly bill on any of these; check each provider's current pricing. Whatever you choose:
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

## 4. First start

1. Open your HTTPS address, enter the **setup code**, and create your administrator account.
   (Alternatively run `python manage.py create-admin "Your Name" you@example.com` inside the container.)
2. Optional: remove `RTS_SETUP_CODE` afterwards. Setup is closed once an administrator exists.

## 5. Google Drive on the live site

In Google Cloud → Credentials → your OAuth client, add the redirect URI
`https://resumes.yourcompany.com/api/google/callback` (exactly `RTS_PUBLIC_URL` + `/api/google/callback`).
Then Settings → **Connect Google Drive** in the app. Make sure the consent screen is **published** ("In production").

## 6. Backups

Everything important is in `/app/data`. Back it up daily:
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
- [ ] One instance, persistent volume at `/app/data`, ≥ 1 GB RAM
- [ ] `RTS_PUBLIC_URL` (https), `RTS_SECURE_COOKIES=1`, `RTS_FORWARDED_ALLOW_IPS=*`, `RTS_SETUP_CODE`
- [ ] Administrator account created
- [ ] Claude API key set and spend limit configured
- [ ] Google redirect URI added for the live address; Drive connected
- [ ] Backups enabled
