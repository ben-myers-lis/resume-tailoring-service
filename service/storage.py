"""Where generated resume files live.

"local": a folder per run under RTS_FILES (fine on one server with a persistent disk).
"db":    inside the database (run_files table). Used on Vercel, where the disk is temporary and the
         request that downloads a file may reach a different instance from the one that made it.
Resumes are small (tens of KB), so the database copes easily.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .config import Settings


def store_run_files(db, settings: Settings, rid: str, src_dir: Path, render: dict) -> None:
    """Called after rendering into src_dir. For the database store, copy the files in and tidy up."""
    if settings.file_store != "db":
        return
    for name in (render.get("files") or {}).values():
        db.put_run_file(rid, name, (src_dir / name).read_bytes())
    shutil.rmtree(src_dir, ignore_errors=True)


def read_run_file(db, settings: Settings, rid: str, name: str) -> bytes | None:
    if settings.file_store == "db":
        return db.get_run_file(rid, name)
    path = settings.files_dir / rid / name
    return path.read_bytes() if path.is_file() else None


def delete_run_files(db, settings: Settings, runs: list[dict]) -> None:
    ids = [r["id"] for r in runs]
    if settings.file_store == "db":
        db.delete_run_files(ids)                 # also removed by ON DELETE CASCADE with the run
    for rid in ids:
        shutil.rmtree(settings.files_dir / rid, ignore_errors=True)
