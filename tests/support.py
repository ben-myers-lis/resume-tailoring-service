"""Test helpers: every test runs on SQLite by default, or on Postgres when RTS_TEST_DATABASE_URL is set.

With Postgres, each Settings object gets its own schema, so tests never see each other's data.
"""
from __future__ import annotations

import os
import uuid

from service.config import Settings, db_target
from service.db import DB

PG_URL = os.environ.get("RTS_TEST_DATABASE_URL", "")
ON_PG = bool(PG_URL)


def make_settings(**kw) -> Settings:
    if ON_PG:
        kw.setdefault("database_url", PG_URL)
        kw.setdefault("db_schema", "t_" + uuid.uuid4().hex[:12])
        kw.setdefault("file_store", "db")
    else:
        kw.setdefault("database_url", "")          # ignore any DATABASE_URL in the environment
        kw.setdefault("file_store", "local")
    kw.setdefault("queue", "threads")
    return Settings(**kw)


def make_db(settings: Settings) -> DB:
    return DB(db_target(settings), settings.db_schema)
