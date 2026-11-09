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


@dataclass(frozen=True)
class Settings:
    ai_provider: str = os.environ.get("RTS_AI_PROVIDER", "mock")          # mock | claude
    db_path: Path = Path(os.environ.get("RTS_DB", ROOT / "data" / "app.db"))
    files_dir: Path = Path(os.environ.get("RTS_FILES", ROOT / "data" / "files"))
    min_fit: int = int(os.environ.get("RTS_MIN_FIT", "40"))                # skip clear mismatches only
    workers: int = int(os.environ.get("RTS_WORKERS", "4"))                 # resumes processed at once
    mock_delay: float = float(os.environ.get("RTS_MOCK_DELAY", "0.3"))     # seconds per mock stage
    make_pdf: bool = _bool("RTS_PDF", True)                                # needs LibreOffice
    allow_illustrative: bool = _bool("RTS_ALLOW_ILLUSTRATIVE", False)      # keep off in production
    max_repair_rounds: int = int(os.environ.get("RTS_MAX_REPAIR_ROUNDS", "1"))
    timezone: str = os.environ.get("RTS_TIMEZONE", "America/New_York")   # for "today" / "this week"
    secure_cookies: bool = _bool("RTS_SECURE_COOKIES", False)           # set 1 when served over HTTPS
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


def public_url(s: Settings) -> str:
    return (s.public_url or f"http://{'localhost' if s.host in ('127.0.0.1', '0.0.0.0') else s.host}:{s.port}").rstrip("/")
