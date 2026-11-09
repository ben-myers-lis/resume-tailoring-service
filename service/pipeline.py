"""One tailoring run: screen -> (skip or) package pipeline -> render -> store."""
from __future__ import annotations

import logging
import tempfile
import time
import traceback
from pathlib import Path

from .ai import make_generator
from .config import Settings
from .db import DB
from .package import PACKAGE_VERSION, build_request, runner
from .render import render_files
from .prompts import VersionedGenerator
from .screening import screen
from . import storage

log = logging.getLogger("rts.pipeline")


def process_run(db: DB, settings: Settings, run: dict) -> None:
    rid = run["id"]
    t0 = time.time()
    candidate = db.get_candidate(run["candidate_id"])
    job = db.get_job(run["job_id"])
    version = (db.get_prompt_version(run["prompt_version_id"]) if run.get("prompt_version_id") else None) \
        or db.active_prompt_version()
    base = {"provider": settings.ai_provider,
            "prompt_version": f"v{version['number']}" if version else PACKAGE_VERSION}
    if not candidate or not job:
        db.finish_run(rid, status="FAILED", error="Candidate or job was deleted.", **base)
        return
    base["profile_version"] = candidate["profile_version"]

    fit = screen(candidate, job)
    if fit["fit_score"] < settings.min_fit and not run["force"] and run.get("purpose", "LIVE") == "LIVE":
        db.finish_run(rid, status="SKIPPED", fit_score=fit["fit_score"], fit=fit, cost_usd=0.0,
                      seconds=round(time.time() - t0, 2), **base)
        log.info("run %s skipped (fit %s)", rid, fit["fit_score"])
        return

    generator = make_generator(settings)
    if version:
        generator = VersionedGenerator(generator, version["files"])
    try:
        request = build_request(run_id=rid, candidate=candidate, job=job, mode=run["mode"],
                                max_repair_rounds=settings.max_repair_rounds)
        result = runner.run_pipeline(request, generator)
        if settings.file_store == "db":
            out_dir = Path(tempfile.mkdtemp(prefix=f"{rid}_"))
        else:
            out_dir = settings.files_dir / rid
        render = render_files(request, result, out_dir, make_pdf=settings.make_pdf, pdf_engine=settings.pdf_engine)
        storage.store_run_files(db, settings, rid, out_dir, render)
        usage = generator.usage_report()
        db.finish_run(rid, status="DONE", fit_score=fit["fit_score"], fit=fit,
                      delivery_status=result["delivery_status"], result=_slim(result), usage=usage,
                      render=render, cost_usd=usage["total"]["cost_usd"],
                      seconds=round(time.time() - t0, 2), **base)
        log.info("run %s done: %s in %.1fs", rid, result["delivery_status"], time.time() - t0)
        if run.get("purpose", "LIVE") == "LIVE":
            from . import drive_service
            if drive_service.options(db)["auto_upload"]:
                drive_service.upload_run(db, settings, rid)   # failures are recorded on the run, never raised
    except Exception as e:  # contract failures and API errors: nothing is delivered
        usage = generator.usage_report() if hasattr(generator, "usage_report") else None
        db.finish_run(rid, status="FAILED", fit_score=fit["fit_score"], fit=fit, usage=usage,
                      error=f"{type(e).__name__}: {e}", cost_usd=(usage or {}).get("total", {}).get("cost_usd"),
                      seconds=round(time.time() - t0, 2), **base)
        log.warning("run %s failed: %s\n%s", rid, e, traceback.format_exc())


def _slim(result: dict) -> dict:
    """Store what the app shows; the full request stays reproducible from the stored profile/job."""
    keep = ("delivery_status", "layout_status", "resume_markdown", "report", "draft", "assessment")
    return {k: result[k] for k in keep}


def ensure_prompt_versions(db: DB) -> None:
    """First start: the prompt package on disk becomes version 1 (active)."""
    if db.count_prompt_versions() == 0:
        from .prompts import package_files
        try:
            db.create_prompt_version(package_files(), "ACTIVE", None, None,
                                     "Initial version from Resume_Tailoring_Portable_v1")
        except Exception:
            # Two instances starting at once (serverless): the other one created version 1 first.
            if db.count_prompt_versions() == 0:
                raise


def delete_run_files(db: DB, settings: Settings, runs: list[dict]) -> None:
    storage.delete_run_files(db, settings, runs)
