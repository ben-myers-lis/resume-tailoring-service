"""Storage for one organisation: an administrator plus invited team members.

Two backends share every query:
  * SQLite (a file on disk) - local use, Docker, any single server with a persistent volume.
  * Postgres (RTS_DATABASE_URL / DATABASE_URL) - required on serverless hosts such as Vercel,
    where the disk is temporary and many instances run at once.
Queries are written once in SQLite style ("?" / ":name" placeholders); the Postgres backend
translates them. Access rules (who may see what) live in service/auth.py.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('ADMIN','MEMBER')),
    permissions TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1,
    api_token TEXT UNIQUE, created_at TEXT NOT NULL, last_login_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invites (
    id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, email TEXT NOT NULL COLLATE NOCASE,
    permissions TEXT NOT NULL, candidate_ids TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT, used_by TEXT);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY, created_by TEXT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL, email TEXT, linkedin TEXT, headline TEXT,
    employment TEXT NOT NULL, education TEXT NOT NULL, certifications TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]', profile_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assignments (
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (candidate_id, user_id));
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, created_by TEXT NOT NULL REFERENCES users(id),
    company TEXT, role TEXT NOT NULL, url TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, created_by TEXT NOT NULL REFERENCES users(id),
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL,            -- QUEUED, RUNNING, SKIPPED, DONE, FAILED
    force INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'APPLICATION',
    provider TEXT, prompt_version TEXT, profile_version INTEGER,
    fit_score INTEGER, fit TEXT, delivery_status TEXT, result TEXT, usage TEXT, render TEXT,
    error TEXT, cost_usd REAL, seconds REAL,
    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL, run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    company TEXT, role TEXT NOT NULL, job_url TEXT,
    applied_on TEXT NOT NULL,        -- YYYY-MM-DD in the organisation's time zone
    applied_by TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'APPLIED', notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_versions (
    id TEXT PRIMARY KEY, number INTEGER UNIQUE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('DRAFT','ACTIVE','ARCHIVED')),
    based_on TEXT, files TEXT NOT NULL, note TEXT,
    created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    published_at TEXT, published_by TEXT REFERENCES users(id));
CREATE TABLE IF NOT EXISTS prompt_test_cases (
    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, note TEXT,
    created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_test_rounds (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES prompt_versions(id) ON DELETE CASCADE,
    compare_version_id TEXT REFERENCES prompt_versions(id) ON DELETE SET NULL,
    content_hash TEXT NOT NULL, case_ids TEXT NOT NULL,
    created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_proposals (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES prompt_versions(id) ON DELETE CASCADE,
    instruction TEXT NOT NULL, provider TEXT, status TEXT NOT NULL, summary TEXT,
    edits TEXT NOT NULL, warnings TEXT NOT NULL, cost_usd REAL,
    created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS drive_folders (
    candidate_id TEXT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE, folder_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_files (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, name TEXT NOT NULL,
    data BLOB NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (run_id, name));
CREATE TABLE IF NOT EXISTS login_failures (key TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS login_failures_key ON login_failures(key, at);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status, created_at);
CREATE INDEX IF NOT EXISTS runs_candidate ON runs(candidate_id, created_at);
CREATE INDEX IF NOT EXISTS apps_candidate ON applications(candidate_id, applied_on);
CREATE INDEX IF NOT EXISTS apps_day ON applications(applied_on);
CREATE INDEX IF NOT EXISTS assign_user ON assignments(user_id);
"""

JSON_FIELDS = {"employment", "education", "certifications", "sources", "fit", "result", "usage", "render",
               "permissions", "candidate_ids", "files", "edits", "warnings", "case_ids"}
# Columns added after the first release, in order (older databases get them on start-up).
MIGRATIONS = [
    ("runs", "prompt_version_id", "TEXT"),
    ("runs", "purpose", "TEXT NOT NULL DEFAULT 'LIVE'"),
    ("runs", "test_round_id", "TEXT"),
    ("jobs", "candidate_id", "TEXT REFERENCES candidates(id) ON DELETE SET NULL"),   # v0.4
    ("runs", "attempts", "INTEGER NOT NULL DEFAULT 0"),                                # v0.6 (queues)
]
APP_STATUSES = ("APPLIED", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN")
RUN_SUMMARY_COLS = ("runs.id, runs.candidate_id, runs.job_id, runs.created_by, runs.status, runs.force, runs.mode, "
                    "runs.purpose, runs.prompt_version_id, runs.test_round_id, runs.attempts, "
                    "runs.provider, runs.prompt_version, runs.profile_version, runs.fit_score, runs.delivery_status, "
                    "runs.error, runs.cost_usd, runs.seconds, runs.created_at, runs.started_at, runs.finished_at")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _in(ids) -> tuple[str, list]:
    ids = list(ids)
    return ("(" + ",".join("?" * len(ids)) + ")") if ids else "(NULL)", ids


class Row:
    """A result row readable by position (row[0]) or by name (row["id"]), like sqlite3.Row."""
    __slots__ = ("_keys", "_values", "_index")

    def __init__(self, keys, values):
        self._keys, self._values, self._index = keys, tuple(values), None

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        if self._index is None:
            self._index = {k: i for i, k in enumerate(self._keys)}
        return self._values[self._index[key]]

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class SQLiteBackend:
    dialect = "sqlite"

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def execute(self, sql: str, args=()):
        return self.conn.execute(sql, args)

    def script(self, sql: str) -> None:
        self.conn.executescript(sql)

    def columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def add_column(self, table: str, col: str, decl: str) -> None:
        if col not in self.columns(table):
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise


_NAMED = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_JSON_EXTRACT = re.compile(r"json_extract\(([\w.]+),\s*'\$\.([\w.]+)'\)")


@lru_cache(maxsize=1024)
def pg_sql(sql: str) -> str:
    """Translate the SQLite-style SQL used in this module into Postgres SQL."""
    sql = sql.replace("%", "%%")
    sql = _JSON_EXTRACT.sub(lambda m: f"({m.group(1)}::jsonb #>> '{{{m.group(2).replace('.', ',')}}}')", sql)
    sql = _NAMED.sub(r"%(\1)s", sql)
    return sql.replace("?", "%s")


def pg_schema(sql: str) -> str:
    return sql.replace(" COLLATE NOCASE", "").replace(" REAL", " DOUBLE PRECISION").replace(" BLOB", " BYTEA")


class PostgresBackend:
    """One connection per process (guarded by DB.lock), reconnected if the server dropped it.

    prepare_threshold=None keeps it compatible with pooled URLs (PgBouncer / Neon "-pooler" hosts).
    An optional schema keeps separate data sets (e.g. production / preview / tests) in one database.
    """
    dialect = "postgres"

    def __init__(self, url: str, schema: str = ""):
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise RuntimeError("Postgres needs the psycopg package: pip install 'psycopg[binary]'") from e
        self.psycopg, self.url = psycopg, url
        if schema and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("RTS_DB_SCHEMA must be lowercase letters, digits and underscores")
        self.schema = schema
        self.conn = None
        self._connect()

    @staticmethod
    def _row_factory(cursor):
        keys = [c.name for c in cursor.description] if cursor.description else []
        return lambda values: Row(keys, values)

    def _connect(self):
        self.conn = self.psycopg.connect(self.url, autocommit=True, prepare_threshold=None,
                                         row_factory=self._row_factory, connect_timeout=15)
        if self.schema:
            self.conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            self.conn.execute(f"SET search_path TO {self.schema}")

    def _ready(self):
        if self.conn is None or self.conn.closed or self.conn.broken:
            self._connect()

    @staticmethod
    def _args(args):
        """Booleans become 0/1: the schema stores flags as INTEGER (as SQLite does)."""
        if isinstance(args, dict):
            return {k: int(v) if isinstance(v, bool) else v for k, v in args.items()}
        return [int(v) if isinstance(v, bool) else v for v in args] if args else None

    def execute(self, sql: str, args=()):
        self._ready()
        args = self._args(args)
        try:
            return self.conn.execute(pg_sql(sql), args)
        except self.psycopg.OperationalError:
            if not self.conn.broken:          # a real error from the server, not a dropped connection
                raise
            self._connect()                   # idle connection was closed by the server: retry once
            return self.conn.execute(pg_sql(sql), args)

    def script(self, sql: str) -> None:
        self._ready()
        self.conn.execute(pg_schema(sql))

    def add_column(self, table: str, col: str, decl: str) -> None:
        self.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {decl}")

    @contextmanager
    def transaction(self):
        self._ready()
        with self.conn.transaction():
            yield


def is_postgres_url(target) -> bool:
    return isinstance(target, str) and target.startswith(("postgres://", "postgresql://"))


class DB:
    def __init__(self, target, schema: str = ""):
        """target: a SQLite file path, or a postgres:// URL."""
        self.c = PostgresBackend(target, schema) if is_postgres_url(target) else SQLiteBackend(target)
        self.dialect = self.c.dialect
        self.lock = threading.RLock()
        with self.lock:
            pg = self.dialect == "postgres"
            if pg:   # serverless instances can start together; concurrent CREATE ... IF NOT EXISTS can clash
                self.c.execute("SELECT pg_advisory_lock(7274110)")
            try:
                self.c.script(SCHEMA)
                for table, col, decl in MIGRATIONS:              # upgrade databases created by earlier versions
                    self.c.add_column(table, col, decl)
                self.c.execute("CREATE INDEX IF NOT EXISTS runs_round ON runs(test_round_id)")
                self.c.execute("CREATE INDEX IF NOT EXISTS jobs_candidate ON jobs(candidate_id)")
                if pg:                                         # SQLite does this with COLLATE NOCASE
                    self.c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_ci ON users (lower(email))")
            finally:
                if pg:
                    self.c.execute("SELECT pg_advisory_unlock(7274110)")

    @contextmanager
    def transaction(self):
        with self.lock, self.c.transaction():
            yield

    # ---------- low level ----------
    def _row(self, row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k in JSON_FIELDS & d.keys():
            if d[k] is not None:
                d[k] = json.loads(d[k])
        return d

    def _exec(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args)

    # Reads run execute + fetch under one lock: worker threads share this connection, and fetching
    # after another thread has started a new statement can return the wrong row (or none).
    def _fetchone(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchone()

    def _fetchall(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def _one(self, sql, args=()):
        return self._row(self._fetchone(sql, args))

    def _all(self, sql, args=()):
        return [self._row(r) for r in self._fetchall(sql, args)]

    @staticmethod
    def _dump(v):
        return json.dumps(v, ensure_ascii=False) if v is not None else None

    # ---------- users ----------
    USER_COLS = "id, email, name, role, permissions, active, created_at, last_login_at"

    def count_users(self) -> int:
        return self._fetchone("SELECT COUNT(*) FROM users")[0]

    def create_user(self, email: str, name: str, password_hash: str, role: str, permissions: list[str]) -> dict:
        u = {"id": new_id("usr"), "email": email.strip(), "name": name.strip(), "password_hash": password_hash,
             "role": role, "permissions": self._dump(sorted(set(permissions))), "api_token": None, "created_at": now()}
        self._exec("""INSERT INTO users (id,email,name,password_hash,role,permissions,api_token,created_at)
                      VALUES (:id,:email,:name,:password_hash,:role,:permissions,:api_token,:created_at)""", u)
        return self.get_user(u["id"])

    def get_user(self, uid: str) -> dict | None:
        return self._one(f"SELECT {self.USER_COLS} FROM users WHERE id=?", (uid,))

    def get_user_auth(self, email: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE lower(email)=lower(?)", (email.strip(),))

    def list_users(self) -> list[dict]:
        users = self._all(f"SELECT {self.USER_COLS} FROM users ORDER BY role, created_at")
        assigned = {}
        for r in self._fetchall("SELECT user_id, candidate_id FROM assignments"):
            assigned.setdefault(r["user_id"], []).append(r["candidate_id"])
        for u in users:
            u["candidate_ids"] = assigned.get(u["id"], [])
        return users

    def update_user(self, uid: str, **fields) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in {"name", "permissions", "active", "password_hash", "email"}}
        if "permissions" in allowed:
            allowed["permissions"] = self._dump(sorted(set(allowed["permissions"])))
        if allowed:
            cols = ", ".join(f"{k}=:{k}" for k in allowed)
            self._exec(f"UPDATE users SET {cols} WHERE id=:id", {**allowed, "id": uid})
            if allowed.get("active") == 0 or "password_hash" in allowed:
                self._exec("DELETE FROM sessions WHERE user_id=?", (uid,))   # sign out everywhere
        return self.get_user(uid)

    def touch_login(self, uid: str) -> None:
        self._exec("UPDATE users SET last_login_at=? WHERE id=?", (now(), uid))

    def set_api_token(self, uid: str) -> str:
        token = "rts_" + secrets.token_urlsafe(24)
        self._exec("UPDATE users SET api_token=? WHERE id=?", (token, uid))
        return token

    def user_by_api_token(self, token: str) -> dict | None:
        return self._one(f"SELECT {self.USER_COLS} FROM users WHERE api_token=? AND active=1", (token,))

    # ---------- sessions ----------
    def create_session(self, uid: str, days: int = 14) -> str:
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
        self._exec("INSERT INTO sessions VALUES (?,?,?,?)", (token, uid, now(), exp))
        return token

    def session_user(self, token: str) -> dict | None:
        return self._one(f"""SELECT {', '.join('u.' + c.strip() for c in self.USER_COLS.split(','))}
                             FROM sessions s JOIN users u ON u.id=s.user_id
                             WHERE s.token=? AND s.expires_at>? AND u.active=1""", (token, now()))

    def delete_session(self, token: str) -> None:
        self._exec("DELETE FROM sessions WHERE token=?", (token,))

    # ---------- invites ----------
    def create_invite(self, email: str, permissions: list[str], candidate_ids: list[str], created_by: str,
                      days: int = 7) -> dict:
        inv = {"id": new_id("inv"), "token": secrets.token_urlsafe(24), "email": email.strip(),
               "permissions": self._dump(sorted(set(permissions))), "candidate_ids": self._dump(candidate_ids),
               "created_by": created_by, "created_at": now(),
               "expires_at": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")}
        self._exec("""INSERT INTO invites (id,token,email,permissions,candidate_ids,created_by,created_at,expires_at)
                      VALUES (:id,:token,:email,:permissions,:candidate_ids,:created_by,:created_at,:expires_at)""", inv)
        return self._one("SELECT * FROM invites WHERE id=?", (inv["id"],))

    def invite_by_token(self, token: str) -> dict | None:
        return self._one("SELECT * FROM invites WHERE token=? AND used_at IS NULL AND expires_at>?", (token, now()))

    def list_invites(self) -> list[dict]:
        return self._all("SELECT * FROM invites WHERE used_at IS NULL ORDER BY created_at DESC")

    def delete_invite(self, iid: str) -> bool:
        return self._exec("DELETE FROM invites WHERE id=? AND used_at IS NULL", (iid,)).rowcount == 1

    def mark_invite_used(self, iid: str, uid: str) -> None:
        self._exec("UPDATE invites SET used_at=?, used_by=? WHERE id=?", (now(), uid, iid))

    # ---------- assignments ----------
    def set_assignees(self, cid: str, user_ids: list[str]) -> None:
        with self.transaction():
            self.c.execute("DELETE FROM assignments WHERE candidate_id=?", (cid,))
            for uid in set(user_ids):
                self.c.execute("INSERT INTO assignments VALUES (?,?)", (cid, uid))

    def set_user_candidates(self, uid: str, candidate_ids: list[str]) -> None:
        with self.transaction():
            self.c.execute("DELETE FROM assignments WHERE user_id=?", (uid,))
            for cid in set(candidate_ids):
                if self.c.execute("SELECT 1 FROM candidates WHERE id=?", (cid,)).fetchone():
                    self.c.execute("INSERT INTO assignments VALUES (?,?)", (cid, uid))

    def assign(self, cid: str, uid: str) -> None:
        self._exec("INSERT INTO assignments VALUES (?,?) ON CONFLICT DO NOTHING", (cid, uid))

    def assigned_candidate_ids(self, uid: str) -> set[str]:
        return {r[0] for r in self._fetchall("SELECT candidate_id FROM assignments WHERE user_id=?", (uid,))}

    def assignees(self, cid: str) -> list[dict]:
        return self._all("""SELECT u.id, u.name, u.email FROM assignments a JOIN users u ON u.id=a.user_id
                            WHERE a.candidate_id=? ORDER BY u.name""", (cid,))

    # ---------- candidates ----------
    def create_candidate(self, created_by: str, data: dict) -> dict:
        c = {"id": new_id("cand"), "created_by": created_by, "name": data["name"], "email": data.get("email"),
             "linkedin": data.get("linkedin"), "headline": data.get("headline"),
             "employment": self._dump(data["employment"]), "education": self._dump(data.get("education", [])),
             "certifications": self._dump(data.get("certifications", [])),
             "sources": self._dump(data.get("sources", [])), "profile_version": 1,
             "created_at": now(), "updated_at": now()}
        self._exec("""INSERT INTO candidates (id,created_by,name,email,linkedin,headline,employment,education,
                      certifications,sources,profile_version,created_at,updated_at) VALUES
                      (:id,:created_by,:name,:email,:linkedin,:headline,:employment,:education,:certifications,
                      :sources,:profile_version,:created_at,:updated_at)""", c)
        return self.get_candidate(c["id"])

    def update_candidate(self, cid: str, data: dict) -> dict | None:
        """Any change creates a new profile version; earlier runs keep their own snapshot."""
        self._exec("""UPDATE candidates SET name=?, email=?, linkedin=?, headline=?, employment=?, education=?,
                      certifications=?, sources=?, profile_version=profile_version+1, updated_at=? WHERE id=?""",
                   (data["name"], data.get("email"), data.get("linkedin"), data.get("headline"),
                    self._dump(data["employment"]), self._dump(data.get("education", [])),
                    self._dump(data.get("certifications", [])), self._dump(data.get("sources", [])), now(), cid))
        return self.get_candidate(cid)

    def get_candidate(self, cid: str) -> dict | None:
        return self._one("SELECT * FROM candidates WHERE id=?", (cid,))

    def list_candidates(self, only_ids: set[str] | None = None) -> list[dict]:
        if only_ids is None:
            return self._all("SELECT * FROM candidates ORDER BY lower(name)")
        clause, args = _in(only_ids)
        return self._all(f"SELECT * FROM candidates WHERE id IN {clause} ORDER BY lower(name)", args)

    def delete_candidate(self, cid: str) -> list[dict]:
        runs = self._all("SELECT id FROM runs WHERE candidate_id=?", (cid,))
        self._exec("DELETE FROM candidates WHERE id=?", (cid,))   # cascades: runs, assignments, applications
        return runs

    # ---------- jobs ----------
    def create_job(self, created_by: str, data: dict, candidate_id: str | None = None) -> dict:
        j = {"id": new_id("job"), "created_by": created_by, "company": data.get("company"), "role": data["role"],
             "url": data.get("url"), "text": data["text"], "created_at": now(), "candidate_id": candidate_id}
        self._exec("""INSERT INTO jobs (id,created_by,company,role,url,text,created_at,candidate_id)
                      VALUES (:id,:created_by,:company,:role,:url,:text,:created_at,:candidate_id)""", j)
        return self.get_job(j["id"])

    def update_job(self, jid: str, **fields) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in {"company", "role", "url"}}
        if allowed:
            cols = ", ".join(f"{k}=:{k}" for k in allowed)
            self._exec(f"UPDATE jobs SET {cols} WHERE id=:id", {**allowed, "id": jid})
            # keep application records in step when they were created from this job
            for k_app, k_job in (("company", "company"), ("role", "role"), ("job_url", "url")):
                if k_job in allowed:
                    self._exec(f"UPDATE applications SET {k_app}=? WHERE job_id=?", (allowed[k_job], jid))
        return self.get_job(jid)

    def get_job(self, jid: str) -> dict | None:
        return self._one("SELECT * FROM jobs WHERE id=?", (jid,))

    def list_jobs(self, only_candidate_ids: set[str] | None = None, candidate_id: str | None = None) -> list[dict]:
        sql = ("SELECT id, created_by, candidate_id, company, role, url, created_at, substr(text,1,240) AS preview "
               "FROM jobs WHERE 1=1")
        args: list = []
        if only_candidate_ids is not None:          # members: jobs of their candidates (or not tied to anyone)
            clause, ids = _in(only_candidate_ids)
            sql += f" AND (candidate_id IS NULL OR candidate_id IN {clause})"
            args += ids
        if candidate_id:
            sql += " AND candidate_id=?"
            args.append(candidate_id)
        return self._all(sql + " ORDER BY created_at DESC", args)

    def delete_job(self, jid: str) -> list[dict]:
        runs = self._all("SELECT id FROM runs WHERE job_id=?", (jid,))
        self._exec("DELETE FROM jobs WHERE id=?", (jid,))
        return runs

    # ---------- runs ----------
    def create_run(self, created_by: str, candidate_id: str, job_id: str, force: bool, mode: str,
                   prompt_version_id: str | None = None, purpose: str = "LIVE", test_round_id: str | None = None) -> dict:
        if prompt_version_id is None:
            active = self.active_prompt_version(meta_only=True)
            prompt_version_id = active["id"] if active else None
        r = {"id": new_id("run"), "created_by": created_by, "candidate_id": candidate_id, "job_id": job_id,
             "status": "QUEUED", "force": int(force), "mode": mode, "created_at": now(),
             "prompt_version_id": prompt_version_id, "purpose": purpose, "test_round_id": test_round_id}
        self._exec("""INSERT INTO runs (id,created_by,candidate_id,job_id,status,force,mode,created_at,
                      prompt_version_id,purpose,test_round_id)
                      VALUES (:id,:created_by,:candidate_id,:job_id,:status,:force,:mode,:created_at,
                      :prompt_version_id,:purpose,:test_round_id)""", r)
        return self.get_run(r["id"])

    def get_run(self, rid: str) -> dict | None:
        return self._one("""SELECT runs.*, c.name AS candidate_name, j.company AS job_company, j.role AS job_role,
                            j.url AS job_url FROM runs JOIN candidates c ON c.id=runs.candidate_id
                            JOIN jobs j ON j.id=runs.job_id WHERE runs.id=?""", (rid,))

    def list_runs(self, only_candidate_ids: set[str] | None = None, status: str | None = None,
                  candidate_id: str | None = None, job_id: str | None = None, limit: int = 200,
                  purpose: str = "LIVE") -> list[dict]:
        sql = (f"SELECT {RUN_SUMMARY_COLS}, c.name AS candidate_name, j.company AS job_company, j.role AS job_role, "
               "(SELECT COUNT(*) FROM applications a WHERE a.run_id=runs.id) AS applied, "
               "json_extract(runs.render, '$.drive.link') AS drive_link, "
               "(SELECT number FROM prompt_versions pv WHERE pv.id=runs.prompt_version_id) AS prompt_number "
               "FROM runs JOIN candidates c ON c.id=runs.candidate_id JOIN jobs j ON j.id=runs.job_id WHERE runs.purpose=?")
        args: list = [purpose]
        if only_candidate_ids is not None:
            clause, ids = _in(only_candidate_ids)
            sql += f" AND runs.candidate_id IN {clause}"
            args += ids
        for col, val in (("runs.status", status), ("runs.candidate_id", candidate_id), ("runs.job_id", job_id)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        sql += " ORDER BY runs.created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(sql, args)

    def claim_next_run(self) -> dict | None:
        """Thread workers: take the oldest queued run."""
        with self.lock:
            row = self.c.execute("SELECT id FROM runs WHERE status='QUEUED' ORDER BY created_at LIMIT 1").fetchone()
            return self.claim_run(row["id"]) if row else None

    def claim_run(self, rid: str, stale_before: str | None = None) -> dict | None:
        """Mark one run RUNNING if it is still waiting (or, with stale_before, was abandoned mid-run).

        The conditional UPDATE is atomic on both backends, so when a queue delivers the same message
        twice, or two instances race, exactly one of them gets the run; the others get None.
        """
        cond = "status='QUEUED'"
        args: list = [now(), rid]
        if stale_before:
            cond = "(status='QUEUED' OR (status='RUNNING' AND started_at<?))"
            args.append(stale_before)
        with self.lock:
            cur = self.c.execute(f"UPDATE runs SET status='RUNNING', started_at=?, attempts=attempts+1 "
                                 f"WHERE id=? AND {cond}", args)
            if cur.rowcount != 1:
                return None
            return self._row(self.c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone())

    def runs_to_dispatch(self, queued_before: str, running_before: str) -> list[dict]:
        """Runs a queue may have lost: queued for a while, or RUNNING past the time limit."""
        return self._all("SELECT id, status, attempts FROM runs WHERE (status='QUEUED' AND created_at<?) "
                         "OR (status='RUNNING' AND started_at<?) ORDER BY created_at LIMIT 200",
                         (queued_before, running_before))

    def requeue_run(self, rid: str, running_before: str) -> bool:
        return self._exec("UPDATE runs SET status='QUEUED', started_at=NULL WHERE id=? AND status='RUNNING' "
                          "AND started_at<?", (rid, running_before)).rowcount == 1

    def finish_run(self, rid: str, **fields) -> None:
        fields["finished_at"] = now()
        for k in JSON_FIELDS & fields.keys():
            fields[k] = self._dump(fields[k])
        cols = ", ".join(f"{k}=:{k}" for k in fields)
        self._exec(f"UPDATE runs SET {cols} WHERE id=:id", {**fields, "id": rid})

    def requeue_stale(self) -> int:
        return self._exec("UPDATE runs SET status='QUEUED', started_at=NULL WHERE status='RUNNING'").rowcount

    def queue_size(self) -> int:
        return self._fetchone("SELECT COUNT(*) FROM runs WHERE status IN ('QUEUED','RUNNING')")[0]

    # ---------- applications ----------
    def create_application(self, data: dict) -> dict:
        a = {"id": new_id("app"), "candidate_id": data["candidate_id"], "job_id": data.get("job_id"),
             "run_id": data.get("run_id"), "company": data.get("company"), "role": data["role"],
             "job_url": data.get("job_url"), "applied_on": data["applied_on"], "applied_by": data["applied_by"],
             "status": data.get("status", "APPLIED"), "notes": data.get("notes"),
             "created_at": now(), "updated_at": now()}
        self._exec("""INSERT INTO applications VALUES (:id,:candidate_id,:job_id,:run_id,:company,:role,:job_url,
                      :applied_on,:applied_by,:status,:notes,:created_at,:updated_at)""", a)
        return self.get_application(a["id"])

    def get_application(self, aid: str) -> dict | None:
        return self._one("""SELECT a.*, u.name AS applied_by_name, c.name AS candidate_name FROM applications a
                            JOIN users u ON u.id=a.applied_by JOIN candidates c ON c.id=a.candidate_id
                            WHERE a.id=?""", (aid,))

    def update_application(self, aid: str, **fields) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in {"status", "notes", "applied_on", "job_url"}}
        if allowed:
            allowed["updated_at"] = now()
            cols = ", ".join(f"{k}=:{k}" for k in allowed)
            self._exec(f"UPDATE applications SET {cols} WHERE id=:id", {**allowed, "id": aid})
        return self.get_application(aid)

    def delete_application(self, aid: str) -> None:
        self._exec("DELETE FROM applications WHERE id=?", (aid,))

    def list_applications(self, only_candidate_ids: set[str] | None = None, candidate_id: str | None = None,
                          date_from: str | None = None, date_to: str | None = None, applied_by: str | None = None,
                          limit: int = 1000) -> list[dict]:
        sql = ("SELECT a.*, u.name AS applied_by_name, c.name AS candidate_name FROM applications a "
               "JOIN users u ON u.id=a.applied_by JOIN candidates c ON c.id=a.candidate_id WHERE 1=1")
        args: list = []
        if only_candidate_ids is not None:
            clause, ids = _in(only_candidate_ids)
            sql += f" AND a.candidate_id IN {clause}"
            args += ids
        for cond, val in (("a.candidate_id=?", candidate_id), ("a.applied_on>=?", date_from),
                          ("a.applied_on<=?", date_to), ("a.applied_by=?", applied_by)):
            if val:
                sql += f" AND {cond}"
                args.append(val)
        sql += " ORDER BY a.applied_on DESC, a.created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(sql, args)

    def application_exists(self, candidate_id: str, run_id: str | None, job_id: str | None) -> bool:
        if run_id:
            q, a = "SELECT 1 FROM applications WHERE run_id=?", (run_id,)
        elif job_id:
            q, a = "SELECT 1 FROM applications WHERE candidate_id=? AND job_id=?", (candidate_id, job_id)
        else:
            return False
        return self._fetchone(q, a) is not None

    def counts_by_day(self, candidate_ids: set[str] | None, date_from: str, date_to: str,
                      group_by: str | None = None) -> list[dict]:
        """Rows of (applied_on, [group key], n). group_by: None, 'candidate_id' or 'applied_by'."""
        key = f", a.{group_by}" if group_by else ""
        sql = f"SELECT a.applied_on{key}, COUNT(*) n FROM applications a WHERE a.applied_on BETWEEN ? AND ?"
        args: list = [date_from, date_to]
        if candidate_ids is not None:
            clause, ids = _in(candidate_ids)
            sql += f" AND a.candidate_id IN {clause}"
            args += ids
        sql += f" GROUP BY a.applied_on{key}"
        return self._all(sql, args)

    def total_applications(self, candidate_ids: set[str] | None) -> dict[str, int]:
        sql, args = "SELECT candidate_id, COUNT(*) n FROM applications", []
        if candidate_ids is not None:
            clause, ids = _in(candidate_ids)
            sql += f" WHERE candidate_id IN {clause}"
            args = ids
        return {r["candidate_id"]: r["n"] for r in self._all(sql + " GROUP BY candidate_id", args)}

    def run_counts_since(self, since_iso: str, candidate_ids: set[str] | None) -> dict[str, int]:
        sql, args = "SELECT status, COUNT(*) n FROM runs WHERE purpose='LIVE' AND created_at>=?", [since_iso]
        if candidate_ids is not None:
            clause, ids = _in(candidate_ids)
            sql += f" AND candidate_id IN {clause}"
            args += ids
        return {r["status"]: r["n"] for r in self._all(sql + " GROUP BY status", args)}

    # ---------- prompt versions ----------
    PV_META = "id, number, status, based_on, note, created_by, created_at, updated_at, published_at, published_by"

    def count_prompt_versions(self) -> int:
        return self._fetchone("SELECT COUNT(*) FROM prompt_versions")[0]

    def create_prompt_version(self, files: dict, status: str, created_by: str | None, based_on: str | None,
                              note: str | None) -> dict:
        with self.transaction():
            n = (self.c.execute("SELECT MAX(number) FROM prompt_versions").fetchone()[0] or 0) + 1
            v = {"id": new_id("pv"), "number": n, "status": status, "based_on": based_on, "files": self._dump(files),
                 "note": note, "created_by": created_by, "created_at": now(), "updated_at": now(),
                 "published_at": now() if status == "ACTIVE" else None,
                 "published_by": created_by if status == "ACTIVE" else None}
            self.c.execute("""INSERT INTO prompt_versions VALUES (:id,:number,:status,:based_on,:files,:note,
                                 :created_by,:created_at,:updated_at,:published_at,:published_by)""", v)
        return self.get_prompt_version(v["id"])

    def get_prompt_version(self, vid: str, meta_only: bool = False) -> dict | None:
        cols = self.PV_META if meta_only else "*"
        return self._one(f"SELECT {cols} FROM prompt_versions WHERE id=?", (vid,))

    def active_prompt_version(self, meta_only: bool = False) -> dict | None:
        cols = self.PV_META if meta_only else "*"
        return self._one(f"SELECT {cols} FROM prompt_versions WHERE status='ACTIVE'")

    def list_prompt_versions(self) -> list[dict]:
        return self._all(f"""SELECT {', '.join('v.' + c.strip() for c in self.PV_META.split(','))},
                             u.name AS created_by_name, p.name AS published_by_name
                             FROM prompt_versions v LEFT JOIN users u ON u.id=v.created_by
                             LEFT JOIN users p ON p.id=v.published_by ORDER BY v.number DESC""")

    def update_prompt_files(self, vid: str, files: dict) -> None:
        self._exec("UPDATE prompt_versions SET files=?, updated_at=? WHERE id=? AND status='DRAFT'",
                   (self._dump(files), now(), vid))

    def set_prompt_note(self, vid: str, note: str | None) -> None:
        self._exec("UPDATE prompt_versions SET note=?, updated_at=? WHERE id=?", (note, now(), vid))

    def activate_prompt_version(self, vid: str, by: str) -> None:
        with self.transaction():
            self.c.execute("UPDATE prompt_versions SET status='ARCHIVED' WHERE status='ACTIVE'")
            self.c.execute("UPDATE prompt_versions SET status='ACTIVE', published_at=?, published_by=? WHERE id=?",
                           (now(), by, vid))

    def delete_prompt_version(self, vid: str) -> bool:
        return self._exec("DELETE FROM prompt_versions WHERE id=? AND status='DRAFT'", (vid,)).rowcount == 1

    # ---------- prompt test cases / rounds ----------
    def create_test_case(self, candidate_id: str, job_id: str, note: str | None, by: str) -> dict:
        t = {"id": new_id("tc"), "candidate_id": candidate_id, "job_id": job_id, "note": note,
             "created_by": by, "created_at": now()}
        self._exec("INSERT INTO prompt_test_cases VALUES (:id,:candidate_id,:job_id,:note,:created_by,:created_at)", t)
        return self.get_test_case(t["id"])

    def get_test_case(self, tid: str) -> dict | None:
        return self._one("""SELECT t.*, c.name AS candidate_name, j.role AS job_role, j.company AS job_company
                            FROM prompt_test_cases t JOIN candidates c ON c.id=t.candidate_id
                            JOIN jobs j ON j.id=t.job_id WHERE t.id=?""", (tid,))

    def list_test_cases(self, only_candidate_ids: set[str] | None = None) -> list[dict]:
        sql = ("SELECT t.*, c.name AS candidate_name, j.role AS job_role, j.company AS job_company "
               "FROM prompt_test_cases t JOIN candidates c ON c.id=t.candidate_id JOIN jobs j ON j.id=t.job_id")
        args: list = []
        if only_candidate_ids is not None:
            clause, ids = _in(only_candidate_ids)
            sql += f" WHERE t.candidate_id IN {clause}"
            args = ids
        return self._all(sql + " ORDER BY t.created_at", args)

    def delete_test_case(self, tid: str) -> None:
        self._exec("DELETE FROM prompt_test_cases WHERE id=?", (tid,))

    def create_test_round(self, version_id: str, compare_version_id: str | None, content_hash: str,
                          case_ids: list[str], by: str) -> dict:
        r = {"id": new_id("tr"), "version_id": version_id, "compare_version_id": compare_version_id,
             "content_hash": content_hash, "case_ids": self._dump(case_ids), "created_by": by, "created_at": now()}
        self._exec("""INSERT INTO prompt_test_rounds VALUES (:id,:version_id,:compare_version_id,:content_hash,
                      :case_ids,:created_by,:created_at)""", r)
        return self.get_test_round(r["id"])

    def get_test_round(self, rid: str) -> dict | None:
        return self._one("""SELECT r.*, u.name AS created_by_name FROM prompt_test_rounds r
                            LEFT JOIN users u ON u.id=r.created_by WHERE r.id=?""", (rid,))

    def list_test_rounds(self, version_id: str) -> list[dict]:
        return self._all("""SELECT r.*, u.name AS created_by_name FROM prompt_test_rounds r
                            LEFT JOIN users u ON u.id=r.created_by WHERE r.version_id=? ORDER BY r.created_at DESC""",
                         (version_id,))

    def round_runs(self, round_id: str) -> list[dict]:
        return self._all("""SELECT runs.id, runs.candidate_id, runs.job_id, runs.status, runs.delivery_status,
                            runs.error, runs.cost_usd, runs.seconds, runs.fit_score, runs.prompt_version_id,
                            runs.result, runs.render, c.name AS candidate_name, j.role AS job_role, j.company AS job_company
                            FROM runs JOIN candidates c ON c.id=runs.candidate_id JOIN jobs j ON j.id=runs.job_id
                            WHERE runs.test_round_id=? ORDER BY runs.created_at""", (round_id,))

    # ---------- prompt proposals ----------
    def create_proposal(self, data: dict) -> dict:
        p = {"id": new_id("pp"), "version_id": data["version_id"], "instruction": data["instruction"],
             "provider": data.get("provider"), "status": "PROPOSED", "summary": data.get("summary"),
             "edits": self._dump(data["edits"]), "warnings": self._dump(data.get("warnings", [])),
             "cost_usd": data.get("cost_usd"), "created_by": data["created_by"], "created_at": now()}
        self._exec("""INSERT INTO prompt_proposals VALUES (:id,:version_id,:instruction,:provider,:status,:summary,
                      :edits,:warnings,:cost_usd,:created_by,:created_at)""", p)
        return self.get_proposal(p["id"])

    def get_proposal(self, pid: str) -> dict | None:
        return self._one("""SELECT p.*, u.name AS created_by_name FROM prompt_proposals p
                            LEFT JOIN users u ON u.id=p.created_by WHERE p.id=?""", (pid,))

    def list_proposals(self, version_id: str) -> list[dict]:
        return self._all("""SELECT p.*, u.name AS created_by_name FROM prompt_proposals p
                            LEFT JOIN users u ON u.id=p.created_by WHERE p.version_id=? ORDER BY p.created_at DESC""",
                         (version_id,))

    def update_proposal(self, pid: str, status: str, edits: list) -> None:
        self._exec("UPDATE prompt_proposals SET status=?, edits=? WHERE id=?", (status, self._dump(edits), pid))

    # ---------- app settings (key -> JSON value) ----------
    def get_setting(self, key: str, default=None):
        row = self._fetchone("SELECT value FROM app_settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row and row["value"] is not None else default

    def set_setting(self, key: str, value) -> None:
        self._exec("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, self._dump(value)))

    # ---------- drive ----------
    def drive_folder(self, cid: str) -> str | None:
        row = self._fetchone("SELECT folder_id FROM drive_folders WHERE candidate_id=?", (cid,))
        return row["folder_id"] if row else None

    def set_drive_folder(self, cid: str, folder_id: str | None) -> None:
        if folder_id is None:
            self._exec("DELETE FROM drive_folders WHERE candidate_id=?", (cid,))
        else:
            self._exec("INSERT INTO drive_folders VALUES (?, ?) ON CONFLICT(candidate_id) DO UPDATE SET "
                       "folder_id=excluded.folder_id", (cid, folder_id))

    def set_run_render(self, rid: str, render: dict) -> None:
        self._exec("UPDATE runs SET render=? WHERE id=?", (self._dump(render), rid))

    def candidate_run_renders(self, cid: str) -> list[dict]:
        return self._all("SELECT id, render FROM runs WHERE candidate_id=?", (cid,))

    # ---------- generated files (used when files live in the database, e.g. on Vercel) ----------
    def put_run_file(self, rid: str, name: str, data: bytes) -> None:
        self._exec("INSERT INTO run_files (run_id, name, data, created_at) VALUES (?,?,?,?) "
                   "ON CONFLICT(run_id, name) DO UPDATE SET data=excluded.data", (rid, name, data, now()))

    def get_run_file(self, rid: str, name: str) -> bytes | None:
        row = self._fetchone("SELECT data FROM run_files WHERE run_id=? AND name=?", (rid, name))
        return bytes(row["data"]) if row else None

    def delete_run_files(self, run_ids: list[str]) -> None:
        if run_ids:
            clause, ids = _in(run_ids)
            self._exec(f"DELETE FROM run_files WHERE run_id IN {clause}", ids)

    # ---------- sign-in throttle (shared by every instance) ----------
    def login_failures(self, key: str, since: float) -> int:
        return self._fetchone("SELECT COUNT(*) FROM login_failures WHERE key=? AND at>?", (key, since))[0]

    def add_login_failure(self, key: str) -> None:
        t = time.time()
        self._exec("INSERT INTO login_failures (key, at) VALUES (?, ?)", (key, t))
        if secrets.randbelow(50) == 0:                       # now and then, forget old attempts
            self._exec("DELETE FROM login_failures WHERE at<?", (t - 86400,))

    def clear_login_failures(self, key: str) -> None:
        self._exec("DELETE FROM login_failures WHERE key=?", (key,))
