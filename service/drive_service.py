"""Glue between the app and Google Drive: settings, per-candidate folders, uploads, links, trash."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone

from .config import Settings, public_url
from .db import DB
from .gdrive import Drive, DriveConfig, DriveError, urllib_transport

log = logging.getLogger("rts.drive")
TRANSPORT = urllib_transport          # tests replace this with a simulated Google
DEFAULT_OPTIONS = {"auto_upload": True, "share_mode": "anyone", "formats": ["pdf"], "trash_on_delete": True}
MIME = {"pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_folder_lock = threading.Lock()


class _Store:
    def __init__(self, db: DB):
        self.db = db

    def get(self, key):
        return self.db.get_setting(f"drive.{key}")

    def set(self, key, value):
        self.db.set_setting(f"drive.{key}", value)


def redirect_uri(settings: Settings) -> str:
    return public_url(settings) + "/api/google/callback"


def configured(settings: Settings) -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def get_drive(db: DB, settings: Settings) -> Drive | None:
    if not configured(settings):
        return None
    return Drive(DriveConfig(settings.google_client_id, settings.google_client_secret, redirect_uri(settings)),
                 _Store(db), TRANSPORT)


def options(db: DB) -> dict:
    return {**DEFAULT_OPTIONS, **(db.get_setting("drive.options") or {})}


def status(db: DB, settings: Settings) -> dict:
    d = get_drive(db, settings)
    root = db.get_setting("drive.root_folder_id")
    return {"configured": configured(settings), "connected": bool(d and d.connected),
            "account_email": db.get_setting("drive.account_email"),
            "folder_link": f"https://drive.google.com/drive/folders/{root}" if root else None,
            "redirect_uri": redirect_uri(settings), "options": options(db)}


def _safe(s: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:60]


def _candidate_folder(drive: Drive, db: DB, candidate: dict) -> str:
    with _folder_lock:                                   # avoid duplicate folders from parallel workers
        fid = db.drive_folder(candidate["id"])
        if fid and drive._exists(fid):
            return fid
        fid = drive.create_folder(candidate["name"], drive.root_folder())
        db.set_drive_folder(candidate["id"], fid)
        return fid


def upload_run(db: DB, settings: Settings, run_id: str) -> dict | None:
    """Upload a finished run's files to Drive and store the links in run.render['drive']."""
    run = db.get_run(run_id)
    drive = get_drive(db, settings)
    if not run or run["status"] != "DONE" or not drive or not drive.connected:
        return None
    render = run.get("render") or {}
    files = render.get("files") or {}
    if render.get("withheld") or not files:
        return None
    opts = options(db)
    wanted = [f for f in opts["formats"] if f in files] or [f for f in ("pdf", "docx") if f in files][:1]
    candidate = db.get_candidate(run["candidate_id"])
    try:
        folder = _candidate_folder(drive, db, candidate)
        uploaded = {}
        stem = "_".join(x for x in (_safe(candidate["name"]), _safe(run["job_company"]), _safe(run["job_role"])) if x)
        for fmt in wanted:
            data = (settings.files_dir / run_id / files[fmt]).read_bytes()
            meta = drive.upload(data, f"{stem}.{fmt}", MIME[fmt], folder)
            if opts["share_mode"] == "anyone":
                drive.share_anyone(meta["id"])
            else:
                for person in db.assignees(candidate["id"]):
                    drive.share_user(meta["id"], person["email"])
            uploaded[fmt] = {"id": meta["id"], "link": meta.get("webViewLink") or f"https://drive.google.com/file/d/{meta['id']}/view"}
        primary = uploaded.get("pdf") or next(iter(uploaded.values()))
        info = {"files": uploaded, "link": primary["link"], "folder_id": folder,
                "share_mode": opts["share_mode"], "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    except (DriveError, OSError, KeyError, ValueError) as e:
        log.warning("Drive upload failed for %s: %s", run_id, e)
        info = {"error": str(e), "failed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    render["drive"] = info
    db.set_run_render(run_id, render)
    return info


def trash_candidate(db: DB, settings: Settings, candidate_id: str) -> int:
    """Move a candidate's Drive folder (and files) to the Drive trash. Returns items trashed."""
    drive = get_drive(db, settings)
    if not drive or not drive.connected or not options(db)["trash_on_delete"]:
        return 0
    n = 0
    folder = db.drive_folder(candidate_id)
    try:
        if folder:
            drive.trash(folder)                       # trashing the folder trashes its files too
            n += 1
        else:
            for r in db.candidate_run_renders(candidate_id):
                for f in ((r.get("render") or {}).get("drive") or {}).get("files", {}).values():
                    drive.trash(f["id"])
                    n += 1
    except DriveError as e:
        log.warning("Couldn't move Drive files to trash for %s: %s", candidate_id, e)
    return n
