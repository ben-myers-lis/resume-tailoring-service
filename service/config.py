"""Settings, read from environment variables (or a .env file in the project folder)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


ON_VERCEL = os.environ.get("VERCEL") == "1"          # set by Vercel at build and run time
# Postgres URL: our own name first, then the names the Vercel/Neon integration sets.
DATABASE_URL = (os.environ.get("RTS_DATABASE_URL") or os.environ.get("DATABASE_URL")
                or os.environ.get("POSTGRES_URL") or "")


@dataclass(frozen=True)
class Settings:
    ai_provider: str = os.environ.get("RTS_AI_PROVIDER", "mock")          # mock | claude
    db_path: Path = Path(os.environ.get("RTS_DB", ROOT / "data" / "app.db"))
    files_dir: Path = Path(os.environ.get("RTS_FILES") or ("/tmp/rts-files" if ON_VERCEL else ROOT / "data" / "files"))
    database_url: str = DATABASE_URL                                       # set -> Postgres instead of SQLite
    db_schema: str = os.environ.get("RTS_DB_SCHEMA", "")                   # optional: separate data sets in one DB
    # Where generated resumes are kept: "local" (files_dir) or "db" (inside the database).
    file_store: str = os.environ.get("RTS_FILE_STORE") or ("db" if DATABASE_URL else "local")
    # How runs are processed: "threads" (in this process) or "vercel" (Vercel Queues).
    queue: str = os.environ.get("RTS_QUEUE") or ("vercel" if ON_VERCEL else "threads")
    run_timeout: int = int(os.environ.get("RTS_RUN_TIMEOUT", "900"))      # RUNNING longer than this = abandoned
    max_attempts: int = int(os.environ.get("RTS_MAX_ATTEMPTS", "2"))      # tries per run before it is FAILED
    cron_secret: str = os.environ.get("CRON_SECRET", "")                   # Vercel sends it to /api/cron/*
    min_fit: int = int(os.environ.get("RTS_MIN_FIT", "40"))                # skip clear mismatches only
    workers: int = int(os.environ.get("RTS_WORKERS", "4"))                 # resumes processed at once
    mock_delay: float = float(os.environ.get("RTS_MOCK_DELAY", "0.3"))     # seconds per mock stage
    make_pdf: bool = _bool("RTS_PDF", True)
    pdf_engine: str = os.environ.get("RTS_PDF_ENGINE", "auto")             # auto | libreoffice | reportlab
    allow_illustrative: bool = _bool("RTS_ALLOW_ILLUSTRATIVE", False)      # keep off in production
    max_repair_rounds: int = int(os.environ.get("RTS_MAX_REPAIR_ROUNDS", "1"))
    timezone: str = os.environ.get("RTS_TIMEZONE", "America/New_York")   # for "today" / "this week"
    secure_cookies: bool = _bool("RTS_SECURE_COOKIES", ON_VERCEL)           # set 1 when served over HTTPS
    password_iterations: int = int(os.environ.get("RTS_PASSWORD_ITERATIONS", "600000"))
    google_client_id: str = os.environ.get("RTS_GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.environ.get("RTS_GOOGLE_CLIENT_SECRET", "")
    public_url: str = os.environ.get("RTS_PUBLIC_URL", "")      # e.g. https://resumes.example.com
    host: str = os.environ.get("RTS_HOST", "127.0.0.1")
    port: int = int(os.environ.get("RTS_PORT") or os.environ.get("PORT") or "8000")   # PORT: set by most hosts
    setup_code: str = os.environ.get("RTS_SETUP_CODE", "")          # required to create the first admin, if set
    forwarded_allow_ips: str = os.environ.get("RTS_FORWARDED_ALLOW_IPS", "127.0.0.1")  # "*" behind a host's proxy


def get_settings() -> Settings:
    return Settings()


def db_target(s: Settings):
    if ON_VERCEL and not s.database_url:
        raise RuntimeError("On Vercel the app needs a Postgres database: add one under the project's Storage tab "
                           "(it sets DATABASE_URL), or set RTS_DATABASE_URL. See DEPLOY.md.")
    return s.database_url or s.db_path


def public_url(s: Settings) -> str:
    if s.public_url:
        return s.public_url.rstrip("/")
    if ON_VERCEL and os.environ.get("VERCEL_PROJECT_PRODUCTION_URL"):     # e.g. my-app.vercel.app
        return f"https://{os.environ['VERCEL_PROJECT_PRODUCTION_URL']}"
    return f"http://{'localhost' if s.host in ('127.0.0.1', '0.0.0.0') else s.host}:{s.port}"
