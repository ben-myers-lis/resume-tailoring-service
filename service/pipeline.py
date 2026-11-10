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
from .prompts import VersionedGenerator, pipeline_of, with_defaults
from . import fast
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

    files = with_defaults(version["files"]) if version else None
    use_fast = bool(files) and pipeline_of(files) == "fast" and run["mode"] == "APPLICATION"
    base["pipeline"] = "fast" if use_fast else "full"
    inner = make_generator(settings)
    generator = VersionedGenerator(inner, files) if files and not use_fast else inner
    try:
        request = build_request(run_id=rid, candidate=candidate, job=job, mode=run["mode"],
                                max_repair_rounds=settings.max_repair_rounds)
        if use_fast:
            result = fast.run_fast(request, inner, files, max_fix_rounds=settings.max_repair_rounds)
        else:
            result = runner.run_pipeline(request, generator)
        if settings.file_store == "db":
            out_dir = Path(tempfile.mkdtemp(prefix=f"{rid}_"))
        else:
            out_dir = settings.files_dir / rid
        render = render_files(request, result, out_dir, make_pdf=settings.make_pdf, pdf_engine=settings.pdf_engine)
        storage.store_run_files(db, settings, rid, out_dir, render)
        usage = generator.usage_report()
        if run.get("purpose") == "TEST":
            from .grader import grade_run
            result["grade"] = grade_run(settings, request, result)   # same judge for every version
            usage = _add_usage(usage, result["grade"].get("usage"))
        db.finish_run(rid, status="DONE", fit_score=fit["fit_score"], fit=fit,
                      delivery_status=result["delivery_status"], result=_slim(result), usage=usage,
                      render=render, cost_usd=usage["total"]["cost_usd"],
                      seconds=round(time.time() - t0, 2), **{k: v for k, v in base.items() if k != "pipeline"})
        log.info("run %s done: %s in %.1fs", rid, result["delivery_status"], time.time() - t0)
        if run.get("purpose", "LIVE") == "LIVE":
            from . import drive_service
            if drive_service.options(db)["auto_upload"]:
                drive_service.upload_run(db, settings, rid)   # failures are recorded on the run, never raised
    except Exception as e:  # contract failures and API errors: nothing is delivered
        usage = generator.usage_report() if hasattr(generator, "usage_report") else None
        db.finish_run(rid, status="FAILED", fit_score=fit["fit_score"], fit=fit, usage=usage,
                      error=f"{type(e).__name__}: {e}", cost_usd=(usage or {}).get("total", {}).get("cost_usd"),
                      seconds=round(time.time() - t0, 2), **{k: v for k, v in base.items() if k != "pipeline"})
        log.warning("run %s failed: %s\n%s", rid, e, traceback.format_exc())


def _slim(result: dict) -> dict:
    """Store what the app shows; the full request stays reproducible from the stored profile/job."""
    keep = ("delivery_status", "layout_status", "resume_markdown", "report", "draft", "assessment")
    out = {k: result[k] for k in keep}
    out["pipeline"] = result.get("pipeline", "full")
    if "grade" in result:
        out["grade"] = {k: v for k, v in result["grade"].items() if k != "usage"}
    return out


def _add_usage(usage: dict, extra: dict | None) -> dict:
    if not extra:
        return usage
    calls = list(usage.get("calls", [])) + list(extra.get("calls", []))
    total = {k: round(usage["total"].get(k, 0) + extra["total"].get(k, 0), 4)
             for k in ("input", "output", "cache_write", "cache_read", "seconds", "cost_usd")}
    return {"calls": calls, "total": total}


FAST_SEED_KEY = "seed.fast_v2_draft"


def ensure_prompt_versions(db: DB) -> None:
    """Start-up: v1 from the package on disk, new files for older versions, and the v2 draft once."""
    from .prompts import PIPELINE_FILE, package_files
    if db.count_prompt_versions() == 0:
        try:
            db.create_prompt_version(package_files(), "ACTIVE", None, None,
                                     "Initial version from Resume_Tailoring_Portable_v1")
        except Exception:
            # Two instances starting at once (serverless): the other one created version 1 first.
            if db.count_prompt_versions() == 0:
                raise
    for vid in db.prompt_version_ids():          # files added by an app update (content unchanged)
        v = db.get_prompt_version(vid)
        filled = with_defaults(v["files"])
        if filled is not v["files"]:
            db.fill_prompt_files(vid, filled)
    if not db.get_setting(FAST_SEED_KEY):        # once, so deleting the draft keeps it deleted
        active = db.active_prompt_version()
        if active:
            files = {**with_defaults(active["files"]), PIPELINE_FILE: "fast\n"}
            db.create_prompt_version(files, "DRAFT", None, active["id"],
                                     "Fast pipeline (v2): about 11–25 s and $0.01–0.03 per resume (estimates). "
                                     "Run a test round against the active version, then publish.")
        db.set_setting(FAST_SEED_KEY, "1")


def delete_run_files(db: DB, settings: Settings, runs: list[dict]) -> None:
    storage.delete_run_files(db, settings, runs)
