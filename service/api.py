"""Web app + JSON API (Starlette).

Sign-in: email + password -> HttpOnly session cookie (web), or "Authorization: Bearer <api token>" (scripts).
Cookie-authenticated changes must send the header "X-Requested-With: rts" (blocks cross-site requests).

Every route checks: signed in -> permission (if any) -> candidate visibility (members: assigned only).
"""
from __future__ import annotations

import contextlib
import hmac
import logging
from datetime import date, datetime, time, timezone
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo
from pathlib import Path

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import stats as st
from .auth import PERMISSIONS, Actor, LoginThrottle, hash_password, password_problem, verify_password
from . import drive_service as ds
from .config import ON_VERCEL, Settings, db_target, get_settings, public_url
from starlette.responses import RedirectResponse
from .db import APP_STATUSES, DB
from .pipeline import delete_run_files
from . import prompts as pr
from .pipeline import ensure_prompt_versions
from .prompt_ai import check_edit, propose
from .schemas import (AcceptInviteIn, ApplicationIn, ApplicationUpdateIn, ApplyProposalIn, AssigneesIn, BulkRunIn,
                      CandidateIn, DraftIn, FilesUpdateIn, InviteIn, JobIn, LoginIn, MemberUpdateIn,
                      PasswordChangeIn, ProposalIn, PublishIn, RunIn, SetupIn, TailorIn, TestCaseIn, TestRoundIn,
                      JobUpdateIn, DriveOptionsIn)
from starlette.concurrency import run_in_threadpool
from .jd import guess_title_company
from .screening import screen
from .jobs import make_dispatcher
from . import storage

log = logging.getLogger("rts.api")
WEB_DIR = Path(__file__).resolve().parent / "web"
COOKIE = "rts_session"
MEDIA = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "pdf": "application/pdf", "md": "text/markdown"}


class HTTPError(Exception):
    def __init__(self, status: int, message: str, details=None):
        self.status, self.message, self.details = status, message, details


def create_app(settings: Settings | None = None, start_workers: bool = True) -> Starlette:
    settings = settings or get_settings()
    db = DB(db_target(settings), settings.db_schema)
    ensure_prompt_versions(db)
    dispatcher = make_dispatcher(db, settings)
    throttle = LoginThrottle(db)

    # ================= helpers =================
    def actor(request: Request) -> Actor:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            u = db.user_by_api_token(auth[7:].strip())
        else:
            token = request.cookies.get(COOKIE)
            u = db.session_user(token) if token else None
            if u and request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "rts":
                raise HTTPError(403, "Missing X-Requested-With header")
        if not u:
            raise HTTPError(401, "Please sign in")
        return Actor(u, db)

    def require(a: Actor, permission: str):
        if not a.can(permission):
            raise HTTPError(403, f"You don't have permission: {PERMISSIONS[permission]}")

    def admin_only(a: Actor):
        if not a.is_admin:
            raise HTTPError(403, "Administrator only")

    async def body(request: Request, model):
        try:
            data = await request.json()
        except Exception:
            raise HTTPError(400, "Body must be JSON")
        try:
            return model.model_validate(data)
        except ValidationError as e:
            raise HTTPError(422, "Please check the highlighted fields",
                            [{"field": ".".join(map(str, x["loc"])), "error": x["msg"].replace("Value error, ", "")}
                             for x in e.errors()])

    def found(obj, what: str):
        if obj is None:
            raise HTTPError(404, f"{what} not found")
        return obj

    def candidate_for(a: Actor, cid: str) -> dict:
        c = db.get_candidate(cid)
        if c is None or not a.sees_candidate(cid):        # same answer either way: no existence leak
            raise HTTPError(404, "Candidate not found")
        return c

    def run_for(a: Actor, rid: str) -> dict:
        r = db.get_run(rid)
        if r is None or not a.sees_candidate(r["candidate_id"]):
            raise HTTPError(404, "Run not found")
        return r

    def application_for(a: Actor, aid: str) -> dict:
        x = db.get_application(aid)
        if x is None or not a.sees_candidate(x["candidate_id"]):
            raise HTTPError(404, "Application not found")
        return x

    def check_mode(mode: str):
        if mode == "ILLUSTRATIVE" and not settings.allow_illustrative:
            raise HTTPError(403, "Illustrative mode is disabled.")

    def me_payload(a: Actor) -> dict:
        u = a.user
        return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"],
                "permissions": sorted(PERMISSIONS) if a.is_admin else u["permissions"]}

    def set_session(resp: Response, uid: str):
        token = db.create_session(uid)
        resp.set_cookie(COOKIE, token, max_age=14 * 86400, httponly=True, samesite="lax",
                        secure=settings.secure_cookies, path="/")

    def client_ip(request: Request) -> str:
        if ON_VERCEL:                         # set by Vercel's edge, which overwrites any value a client sends
            h = request.headers
            return (h.get("x-vercel-forwarded-for") or h.get("x-forwarded-for") or h.get("x-real-ip") or "").split(",")[0].strip()
        return request.client.host if request.client else ""

    def ok(data, status=200):
        return JSONResponse(data, status_code=status)

    async def queue_runs(run_ids: list[str]):
        """Hand newly created runs to the dispatcher (a network call with Vercel Queues)."""
        await run_in_threadpool(dispatcher.enqueue, run_ids)

    # ================= public =================
    async def health(request):
        return ok({"ok": True, "provider": settings.ai_provider, "workers": settings.workers,
                   "queue": dispatcher.name, "database": db.dialect, "files": settings.file_store})

    async def cron_sweep(request):
        """Vercel Cron calls this with "Authorization: Bearer <CRON_SECRET>"."""
        sent = request.headers.get("authorization", "")
        if not settings.cron_secret or not hmac.compare_digest(sent, f"Bearer {settings.cron_secret}"):
            raise HTTPError(401, "Unauthorized")
        return ok(await run_in_threadpool(dispatcher.sweep))

    async def auth_state(request):
        user = None
        try:
            user = me_payload(actor(request))
        except HTTPError:
            pass
        return ok({"needs_setup": db.count_users() == 0, "setup_code_required": bool(settings.setup_code), "user": user})

    async def auth_setup(request):
        data = await body(request, SetupIn)
        if db.count_users() > 0:
            raise HTTPError(409, "Setup is already complete. Please sign in.")
        if settings.setup_code and not hmac.compare_digest((data.setup_code or "").strip(), settings.setup_code):
            raise HTTPError(403, "The setup code is incorrect.", [{"field": "setup_code", "error": "Incorrect setup code"}])
        if problem := password_problem(data.password):
            raise HTTPError(422, problem, [{"field": "password", "error": problem}])
        u = db.create_user(data.email, data.name, hash_password(data.password, settings.password_iterations),
                           "ADMIN", [])
        db.touch_login(u["id"])
        resp = ok({"user": me_payload(Actor(u, db))}, 201)
        set_session(resp, u["id"])
        return resp

    async def auth_login(request):
        data = await body(request, LoginIn)
        key = f"{data.email.strip().lower()}|{client_ip(request)}"
        if throttle.blocked(key):
            raise HTTPError(429, "Too many attempts. Please wait 15 minutes and try again.")
        u = db.get_user_auth(data.email)
        if not u or not u["active"] or not verify_password(data.password, u["password_hash"]):
            throttle.fail(key)
            raise HTTPError(401, "Email or password is incorrect.")
        throttle.reset(key)
        db.touch_login(u["id"])
        user = db.get_user(u["id"])
        resp = ok({"user": me_payload(Actor(user, db))})
        set_session(resp, u["id"])
        return resp

    async def auth_logout(request):
        token = request.cookies.get(COOKIE)
        if token:
            db.delete_session(token)
        resp = ok({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    async def invite_info(request):
        inv = db.invite_by_token(request.path_params["token"])
        if not inv:
            raise HTTPError(404, "This invitation link is invalid or has expired.")
        return ok({"email": inv["email"], "expires_at": inv["expires_at"]})

    async def invite_accept(request):
        data = await body(request, AcceptInviteIn)
        inv = db.invite_by_token(data.token)
        if not inv:
            raise HTTPError(404, "This invitation link is invalid or has expired.")
        if db.get_user_auth(inv["email"]):
            raise HTTPError(409, "An account with this email already exists. Please sign in.")
        if problem := password_problem(data.password):
            raise HTTPError(422, problem, [{"field": "password", "error": problem}])
        u = db.create_user(inv["email"], data.name, hash_password(data.password, settings.password_iterations),
                           "MEMBER", inv["permissions"])
        db.set_user_candidates(u["id"], inv["candidate_ids"])
        db.mark_invite_used(inv["id"], u["id"])
        db.touch_login(u["id"])
        resp = ok({"user": me_payload(Actor(u, db))}, 201)
        set_session(resp, u["id"])
        return resp

    # ================= account =================
    async def me(request):
        return ok(me_payload(actor(request)))

    async def me_password(request):
        a = actor(request)
        data = await body(request, PasswordChangeIn)
        full = db.get_user_auth(a.user["email"])
        if not verify_password(data.current_password, full["password_hash"]):
            raise HTTPError(401, "Current password is incorrect.")
        if problem := password_problem(data.new_password):
            raise HTTPError(422, problem, [{"field": "new_password", "error": problem}])
        db.update_user(a.id, password_hash=hash_password(data.new_password, settings.password_iterations))
        resp = ok({"ok": True, "message": "Password changed. Please sign in again."})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    async def me_api_token(request):
        a = actor(request)
        return ok({"api_token": db.set_api_token(a.id),
                   "note": "Shown once. Creating a new token replaces the old one."})

    async def meta(request):
        a = actor(request)
        return ok({"permissions": PERMISSIONS, "application_statuses": APP_STATUSES,
                   "min_fit": settings.min_fit, "provider": settings.ai_provider, "time_zone": settings.timezone,
                   "today": st.today(settings.timezone).isoformat(), "me": me_payload(a)})

    # ================= dashboard =================
    async def dashboard(request):
        a = actor(request)
        await run_in_threadpool(dispatcher.maybe_sweep)
        ids = a.visible_candidate_ids
        d = st.dashboard(db, ids, settings.timezone)
        names = {u["id"]: u["name"] for u in db.list_users()}
        d["this_week_by_member"] = sorted(({"user_id": k, "name": names.get(k, "?"), "count": v}
                                           for k, v in d["this_week_by_member"].items()), key=lambda x: -x["count"])
        counts = st.summary_counts(db, ids, settings.timezone)
        cands = db.list_candidates(ids)
        d["candidates"] = sorted(({"id": c["id"], "name": c["name"], **counts.get(c["id"], {"today": 0, "this_week": 0, "total": 0})}
                                  for c in cands), key=lambda x: (-x["this_week"], x["name"]))
        local_start = datetime.combine(date.fromisoformat(d["week_start"]), time.min, ZoneInfo(settings.timezone))
        d["resumes_this_week"] = db.run_counts_since(local_start.astimezone(timezone.utc).isoformat(), ids)
        d["queue"] = db.queue_size()
        d["candidate_count"] = len(cands)
        return ok(d)

    # ================= candidates =================
    def candidate_out(c: dict, counts: dict | None = None) -> dict:
        out = {k: v for k, v in c.items()}
        out["assignees"] = db.assignees(c["id"])
        if counts is not None:
            out["applications"] = counts.get(c["id"], {"today": 0, "this_week": 0, "total": 0})
        return out

    async def candidates(request):
        a = actor(request)
        if request.method == "POST":
            admin_only(a)                                # candidate profiles are managed by the administrator
            data = (await body(request, CandidateIn)).model_dump()
            return ok(candidate_out(db.create_candidate(a.id, data)), 201)
        ids = a.visible_candidate_ids
        counts = st.summary_counts(db, ids, settings.timezone)
        return ok([candidate_out(c, counts) for c in db.list_candidates(ids)])

    async def candidate(request):
        a = actor(request)
        cid = request.path_params["id"]
        c = candidate_for(a, cid)
        if request.method == "PUT":
            admin_only(a)
            data = (await body(request, CandidateIn)).model_dump()
            return ok(candidate_out(db.update_candidate(cid, data)))
        if request.method == "DELETE":
            admin_only(a)
            await run_in_threadpool(ds.trash_candidate, db, settings, cid)
            runs = db.delete_candidate(cid)
            delete_run_files(db, settings, runs)
            return ok({"deleted": cid, "runs_deleted": len(runs)})
        return ok(candidate_out(c, st.summary_counts(db, {cid}, settings.timezone)))

    async def candidate_assignees(request):
        a = actor(request)
        admin_only(a)
        cid = request.path_params["id"]
        candidate_for(a, cid)
        data = await body(request, AssigneesIn)
        members = {u["id"] for u in db.list_users() if u["role"] == "MEMBER"}
        unknown = set(data.user_ids) - members
        if unknown:
            raise HTTPError(422, "Unknown team member(s)", sorted(unknown))
        db.set_assignees(cid, data.user_ids)
        return ok({"candidate_id": cid, "assignees": db.assignees(cid)})

    async def candidate_stats(request):
        a = actor(request)
        cid = request.path_params["id"]
        candidate_for(a, cid)
        days = min(max(int(request.query_params.get("days", 30)), 7), 120)
        weeks = min(max(int(request.query_params.get("weeks", 12)), 4), 52)
        return ok(st.candidate_stats(db, cid, settings.timezone, days, weeks))

    # ================= jobs =================
    # Jobs are created by pasting a job description on a candidate's page (POST /api/candidates/{id}/tailor).
    # POST /api/jobs remains for scripts and the Prompt Studio.
    def job_for(a: Actor, jid: str) -> dict:
        j = db.get_job(jid)
        if j is None or (j.get("candidate_id") and not a.sees_candidate(j["candidate_id"])):
            raise HTTPError(404, "Job not found")
        return j

    async def jobs(request):
        a = actor(request)
        if request.method == "POST":
            require(a, "resumes.tailor")
            data = (await body(request, JobIn)).model_dump()
            return ok(db.create_job(a.id, data), 201)
        cid = request.query_params.get("candidate_id")
        if cid:
            candidate_for(a, cid)
        return ok(db.list_jobs(a.visible_candidate_ids, cid))

    async def job(request):
        a = actor(request)
        j = job_for(a, request.path_params["id"])
        if request.method == "DELETE":
            if not (a.is_admin or (j["created_by"] == a.id and a.can("resumes.tailor"))):
                raise HTTPError(403, "Only the administrator or the person who added this job can delete it")
            runs = db.delete_job(j["id"])
            delete_run_files(db, settings, runs)
            return ok({"deleted": j["id"], "runs_deleted": len(runs)})
        if request.method == "PATCH":
            require(a, "resumes.tailor")
            data = await body(request, JobUpdateIn)
            return ok(db.update_job(j["id"], **data.model_dump(exclude_unset=True)))
        return ok(j)

    async def candidate_tailor(request):
        """Paste a plain-text job description on the candidate's page and tailor for it."""
        a = actor(request)
        require(a, "resumes.tailor")
        c = candidate_for(a, request.path_params["id"])
        data = await body(request, TailorIn)
        text = data.jd_text.strip()
        g_title, g_company = guess_title_company(text)
        job = db.create_job(a.id, {"role": (data.title or "").strip() or g_title or "Untitled job",
                                   "company": (data.company or "").strip() or g_company,
                                   "url": (data.url or "").strip() or None, "text": text}, candidate_id=c["id"])
        run = db.create_run(a.id, c["id"], job["id"], data.force, "APPLICATION")
        await queue_runs([run["id"]])
        return ok({"run": run, "job": job, "detected": {"title": g_title, "company": g_company}}, 202)

    # ================= screening & runs =================
    async def screen_route(request):
        a = actor(request)
        require(a, "resumes.tailor")
        c = candidate_for(a, request.query_params.get("candidate_id", ""))
        j = job_for(a, request.query_params.get("job_id", ""))
        result = screen(c, j)
        result.update(would_tailor=result["fit_score"] >= settings.min_fit, threshold=settings.min_fit)
        return ok(result)

    async def runs(request):
        a = actor(request)
        if request.method == "POST":
            require(a, "resumes.tailor")
            r = await body(request, RunIn)
            check_mode(r.mode)
            candidate_for(a, r.candidate_id)
            job_for(a, r.job_id)
            created = db.create_run(a.id, r.candidate_id, r.job_id, r.force, r.mode)
            await queue_runs([created["id"]])
            return ok(created, 202)
        await run_in_threadpool(dispatcher.maybe_sweep)
        q = request.query_params
        cid = q.get("candidate_id")
        if cid:
            candidate_for(a, cid)
        return ok(db.list_runs(a.visible_candidate_ids, q.get("status"), cid, q.get("job_id"),
                               min(int(q.get("limit", 200)), 1000)))

    async def runs_bulk(request):
        a = actor(request)
        require(a, "resumes.tailor")
        b = await body(request, BulkRunIn)
        check_mode(b.mode)
        for cid in b.candidate_ids:
            candidate_for(a, cid)
        for jid in b.job_ids:
            job_for(a, jid)
        created = [db.create_run(a.id, c, j, b.force, b.mode)["id"] for c in b.candidate_ids for j in b.job_ids]
        await queue_runs(created)
        return ok({"queued": len(created), "run_ids": created}, 202)

    async def run(request):
        a = actor(request)
        r = run_for(a, request.path_params["id"])
        if r["status"] in ("QUEUED", "RUNNING"):
            await run_in_threadpool(dispatcher.maybe_sweep)      # someone is waiting on this run
        result = r.pop("result", None) or {}
        r.pop("usage", None)
        r["resume_markdown"] = result.get("resume_markdown")
        r["gaps"] = result.get("draft", {}).get("gaps")
        r["questions_for_candidate"] = result.get("draft", {}).get("confirmation_items")
        render = r.get("render") or {}
        files = render.get("files", {}) if a.can("resumes.tailor") else {}
        r["files"] = {k: f"/api/runs/{r['id']}/resume.{k}" for k in files}
        drive = render.get("drive") or {}
        drive_on = ds.status(db, settings)
        r["drive"] = ({"link": drive.get("link"), "error": drive.get("error"),
                       "pending": r["status"] == "DONE" and not drive and bool(files) and drive_on["connected"]
                                  and drive_on["options"]["auto_upload"] and r.get("purpose") == "LIVE"}
                      if a.can("resumes.tailor") else {})
        r["applications"] = [x for x in db.list_applications({r["candidate_id"]}) if x["run_id"] == r["id"]]
        return ok(r)

    async def run_report(request):
        a = actor(request)
        r = run_for(a, request.path_params["id"])
        result = r.get("result") or {}
        return ok({"run_id": r["id"], "status": r["status"], "fit": r.get("fit"), "report": result.get("report"),
                   "render": r.get("render"), "usage": r.get("usage")})

    async def run_file(request):
        a = actor(request)
        require(a, "resumes.tailor")
        r = run_for(a, request.path_params["id"])
        ext = request.path_params["ext"]
        name = ((r.get("render") or {}).get("files") or {}).get(ext)
        data = await run_in_threadpool(storage.read_run_file, db, settings, r["id"], name) if name else None
        if data is None or ext not in MEDIA:
            raise HTTPError(404, f"No {ext} file for this run")
        stem = "".join(ch if ch.isalnum() else "_" for ch in f"{r['candidate_name']} {r['job_company'] or ''}").strip("_")
        return Response(data, media_type=MEDIA[ext], headers={
            "Content-Disposition": f"attachment; filename=\"{stem}_resume.{ext}\"; filename*=UTF-8''{quote(stem)}_resume.{ext}",
            "Cache-Control": "private, no-store"})

    # ================= applications =================
    async def applications(request):
        a = actor(request)
        if request.method == "POST":
            data = await body(request, ApplicationIn)
            candidate_for(a, data.candidate_id)
            rec = data.model_dump()
            if data.run_id:
                r = run_for(a, data.run_id)
                if r["candidate_id"] != data.candidate_id:
                    raise HTTPError(422, "That resume belongs to a different candidate")
                rec["job_id"] = r["job_id"]
            if rec.get("job_id"):
                j = job_for(a, rec["job_id"])
                rec["company"] = rec.get("company") or j["company"]
                rec["role"] = rec.get("role") or j["role"]
                rec["job_url"] = rec.get("job_url") or j["url"]
            if not rec.get("role"):
                raise HTTPError(422, "Please enter the job title", [{"field": "role", "error": "required"}])
            if db.application_exists(data.candidate_id, rec.get("run_id"), rec.get("job_id")) and \
                    request.query_params.get("allow_duplicate") != "1":
                raise HTTPError(409, "Already marked as applied for this job")
            rec["applied_on"] = rec.get("applied_on") or st.today(settings.timezone).isoformat()
            rec["applied_by"] = a.id
            return ok(db.create_application(rec), 201)
        q = request.query_params
        cid = q.get("candidate_id")
        if cid:
            candidate_for(a, cid)
        return ok(db.list_applications(a.visible_candidate_ids, cid, q.get("from"), q.get("to"),
                                       q.get("applied_by"), min(int(q.get("limit", 1000)), 5000)))

    async def application(request):
        a = actor(request)
        x = application_for(a, request.path_params["id"])
        if request.method == "DELETE":
            if not (a.is_admin or x["applied_by"] == a.id):
                raise HTTPError(403, "Only the administrator or the person who logged it can delete it")
            db.delete_application(x["id"])
            return ok({"deleted": x["id"]})
        data = await body(request, ApplicationUpdateIn)
        return ok(db.update_application(x["id"], **data.model_dump(exclude_none=True)))

    # ================= team (administrator) =================
    async def team(request):
        a = actor(request)
        admin_only(a)
        base = str(request.base_url).rstrip("/")
        invites = [{**i, "link": f"{base}/#/invite/{i['token']}"} for i in db.list_invites()]
        return ok({"members": db.list_users(), "invites": invites, "permissions": PERMISSIONS})

    async def team_invite(request):
        a = actor(request)
        admin_only(a)
        data = await body(request, InviteIn)
        if db.get_user_auth(data.email):
            raise HTTPError(409, "Someone with this email already has an account")
        for cid in data.candidate_ids:
            found(db.get_candidate(cid), f"Candidate {cid}")
        inv = db.create_invite(data.email, data.permissions, data.candidate_ids, a.id)
        base = str(request.base_url).rstrip("/")
        return ok({**inv, "link": f"{base}/#/invite/{inv['token']}"}, 201)

    async def team_invite_delete(request):
        a = actor(request)
        admin_only(a)
        if not db.delete_invite(request.path_params["id"]):
            raise HTTPError(404, "Invitation not found")
        return ok({"deleted": request.path_params["id"]})

    async def team_member(request):
        a = actor(request)
        admin_only(a)
        u = found(db.get_user(request.path_params["id"]), "Team member")
        if u["role"] == "ADMIN":
            raise HTTPError(403, "The administrator account can't be changed here")
        data = await body(request, MemberUpdateIn)
        fields = {}
        if data.permissions is not None:
            fields["permissions"] = data.permissions
        if data.active is not None:
            fields["active"] = int(data.active)
        db.update_user(u["id"], **fields)
        if data.candidate_ids is not None:
            db.set_user_candidates(u["id"], data.candidate_ids)
        return ok(next(m for m in db.list_users() if m["id"] == u["id"]))

    # ================= prompt studio =================
    def version_for(vid: str) -> dict:
        return found(db.get_prompt_version(vid), "Prompt version")

    def draft_for(vid: str) -> dict:
        v = version_for(vid)
        if v["status"] != "DRAFT":
            raise HTTPError(409, "Only drafts can be changed. Create a new draft from this version.")
        return v

    def round_view(a: Actor, rnd: dict) -> dict:
        runs = db.round_runs(rnd["id"])
        cases = {}
        for r in runs:
            if not a.sees_candidate(r["candidate_id"]):
                continue
            key = (r["candidate_id"], r["job_id"])
            res = r.pop("result", None) or {}
            files = (r.pop("render", None) or {}).get("files", {})
            r.update(resume_markdown=res.get("resume_markdown"),
                     gaps=(res.get("draft") or {}).get("gaps"),
                     findings=[f for f in (res.get("assessment") or {}).get("findings", []) if not f.get("resolved")],
                     files={k: f"/api/runs/{r['id']}/resume.{k}" for k in files} if a.can("resumes.tailor") else {})
            slot = cases.setdefault(key, {"candidate_name": r["candidate_name"], "job_role": r["job_role"],
                                          "job_company": r["job_company"], "draft": None, "compare": None})
            slot["draft" if r["prompt_version_id"] == rnd["version_id"] else "compare"] = r
        draft_runs = [r for r in runs if r["prompt_version_id"] == rnd["version_id"]]
        statuses = [r["status"] for r in draft_runs]
        state = ("RUNNING" if any(s in ("QUEUED", "RUNNING") for s in statuses)
                 else "FAILED" if "FAILED" in statuses else "PASSED")
        return {**rnd, "state": state, "cases": list(cases.values()),
                "cost_usd": round(sum(r["cost_usd"] or 0 for r in runs), 4)}

    def checks_for(v: dict) -> dict:
        active = db.active_prompt_version()
        structural = pr.structural_problems(v["files"])
        protected = pr.protected_changes(active["files"], v["files"]) if active and active["id"] != v["id"] else []
        h = pr.content_hash(v["files"])
        rounds = db.list_test_rounds(v["id"])
        current = next((r for r in rounds if r["content_hash"] == h), None)
        if current:
            test = {"state": round_view(Actor({"id": "", "role": "ADMIN", "permissions": []}, db), current)["state"],
                    "round_id": current["id"]}
        else:
            test = {"state": "STALE" if rounds else "NONE", "round_id": rounds[0]["id"] if rounds else None}
        blockers = []
        if v["status"] != "DRAFT":
            blockers.append("Only drafts can be published")
        blockers += structural
        if test["state"] in ("NONE", "STALE"):
            blockers.append("Run a test round on the current version of this draft")
        elif test["state"] == "RUNNING":
            blockers.append("Wait for the test round to finish")
        elif test["state"] == "FAILED":
            blockers.append("The latest test round has failed resumes; fix and test again")
        return {"structural": structural, "protected": protected, "test": test, "content_hash": h,
                "blockers": blockers, "can_publish": not blockers}

    async def prompts_home(request):
        a = actor(request)
        require(a, "prompts.manage")
        return ok({"versions": db.list_prompt_versions(),
                   "test_cases": db.list_test_cases(a.visible_candidate_ids),
                   "protected_rules": pr.PROTECTED_RULES, "protected_files": pr.PROTECTED_FILES,
                   "can_publish": a.is_admin, "provider": settings.ai_provider})

    async def prompt_versions(request):
        a = actor(request)
        require(a, "prompts.manage")
        data = await body(request, DraftIn)
        base = version_for(data.based_on) if data.based_on else db.active_prompt_version()
        v = db.create_prompt_version(base["files"], "DRAFT", a.id, base["id"], data.note)
        return ok(db.get_prompt_version(v["id"], meta_only=True), 201)

    async def prompt_version(request):
        a = actor(request)
        require(a, "prompts.manage")
        v = version_for(request.path_params["id"])
        if request.method == "DELETE":
            if v["status"] != "DRAFT":
                raise HTTPError(409, "Only drafts can be deleted")
            if not (a.is_admin or v["created_by"] == a.id):
                raise HTTPError(403, "Only the administrator or the draft's creator can delete it")
            db.delete_prompt_version(v["id"])
            return ok({"deleted": v["id"]})
        active = db.active_prompt_version()
        base = version_for(v["based_on"]) if v.get("based_on") else None
        compare_to = active if active and active["id"] != v["id"] else base
        return ok({**{k: x for k, x in v.items()}, "labels": pr.FILE_LABELS, "editable": pr.EDITABLE,
                   "compare_to": {"id": compare_to["id"], "number": compare_to["number"], "status": compare_to["status"]} if compare_to else None,
                   "diff": pr.diff(compare_to["files"], v["files"]) if compare_to else [],
                   "checks": checks_for(v), "proposals": db.list_proposals(v["id"]),
                   "rounds": [round_view(a, r) for r in db.list_test_rounds(v["id"])[:5]],
                   "can_publish": a.is_admin})

    async def prompt_files(request):
        a = actor(request)
        require(a, "prompts.manage")
        v = draft_for(request.path_params["id"])
        data = await body(request, FilesUpdateIn)
        unknown = set(data.files) - set(pr.EDITABLE)
        if unknown:
            raise HTTPError(422, "These files can't be edited: " + ", ".join(sorted(unknown)))
        files = {**v["files"], **data.files}
        db.update_prompt_files(v["id"], files)
        if data.note is not None:
            db.set_prompt_note(v["id"], data.note)
        return ok({"saved": sorted(data.files), "checks": checks_for(db.get_prompt_version(v["id"]))})

    async def prompt_propose(request):
        a = actor(request)
        require(a, "prompts.manage")
        v = draft_for(request.path_params["id"])
        data = await body(request, ProposalIn)
        examples = []
        if data.use_test_resumes:
            rounds = db.list_test_rounds(v["id"]) or (db.list_test_rounds(v["based_on"]) if v.get("based_on") else [])
            if rounds:
                for c in round_view(a, rounds[0])["cases"][:3]:
                    run = c["draft"] or c["compare"]
                    if run and run.get("resume_markdown"):
                        examples.append({"candidate": c["candidate_name"], "job": c["job_role"],
                                         "status": run.get("delivery_status"), "resume": run["resume_markdown"]})
        try:
            result = await run_in_threadpool(propose, v["files"], data.instruction, settings.ai_provider, examples)
        except Exception as e:  # API/network problems: report, don't crash
            raise HTTPError(502, f"Couldn't get a proposal from the AI: {e}")
        prop = db.create_proposal({"version_id": v["id"], "instruction": data.instruction,
                                   "provider": result.get("provider"), "summary": result.get("summary"),
                                   "edits": result["edits"],
                                   "warnings": result.get("warnings", []) + [f"Needs a code change: {x}" for x in result.get("needs_code_change", [])],
                                   "cost_usd": result.get("cost_usd"), "created_by": a.id})
        return ok(prop, 201)

    async def proposal_apply(request):
        a = actor(request)
        require(a, "prompts.manage")
        prop = found(db.get_proposal(request.path_params["id"]), "Proposal")
        if prop["status"] != "PROPOSED":
            raise HTTPError(409, "This proposal was already reviewed")
        v = draft_for(prop["version_id"])
        data = await body(request, ApplyProposalIn)
        files, edits, results = dict(v["files"]), prop["edits"], []
        decisions = {d.index: d for d in data.decisions}
        for i, e in enumerate(edits):
            d = decisions.get(i)
            if not d or not d.accept:
                e["status"] = "REJECTED"
                continue
            if d.replace is not None:
                e["replace"] = d.replace
            try:
                files = pr.apply_edit(files, e)
                e["status"], e["problem"] = "APPLIED", None
            except ValueError as err:
                e["status"], e["problem"] = "FAILED", str(err)
            results.append({"index": i, "status": e["status"], "problem": e["problem"]})
        applied = sum(1 for e in edits if e["status"] == "APPLIED")
        db.update_prompt_files(v["id"], files)
        db.update_proposal(prop["id"], "APPLIED" if applied else "REJECTED", edits)
        return ok({"applied": applied, "results": results, "checks": checks_for(db.get_prompt_version(v["id"]))})

    async def test_cases(request):
        a = actor(request)
        require(a, "prompts.manage")
        if request.method == "POST":
            data = await body(request, TestCaseIn)
            candidate_for(a, data.candidate_id)
            job_for(a, data.job_id)
            return ok(db.create_test_case(data.candidate_id, data.job_id, data.note, a.id), 201)
        return ok(db.list_test_cases(a.visible_candidate_ids))

    async def test_case(request):
        a = actor(request)
        require(a, "prompts.manage")
        t = found(db.get_test_case(request.path_params["id"]), "Test case")
        candidate_for(a, t["candidate_id"])
        db.delete_test_case(t["id"])
        return ok({"deleted": t["id"]})

    async def test_round_create(request):
        a = actor(request)
        require(a, "prompts.manage")
        v = version_for(request.path_params["id"])
        data = await body(request, TestRoundIn)
        cases = db.list_test_cases(a.visible_candidate_ids)
        if data.case_ids is not None:
            wanted = set(data.case_ids)
            cases = [c for c in cases if c["id"] in wanted]
        if not cases:
            raise HTTPError(422, "Add at least one test case (candidate + job) first")
        active = db.active_prompt_version(meta_only=True)
        compare_id = active["id"] if data.compare and active and active["id"] != v["id"] else None
        rnd = db.create_test_round(v["id"], compare_id, pr.content_hash(v["files"]), [c["id"] for c in cases], a.id)
        created = []
        for c in cases:
            for vid in [v["id"]] + ([compare_id] if compare_id else []):
                created.append(db.create_run(a.id, c["candidate_id"], c["job_id"], True, "APPLICATION",
                                             prompt_version_id=vid, purpose="TEST", test_round_id=rnd["id"])["id"])
        await queue_runs(created)
        return ok(round_view(a, rnd), 202)

    async def test_round(request):
        a = actor(request)
        require(a, "prompts.manage")
        return ok(round_view(a, found(db.get_test_round(request.path_params["id"]), "Test round")))

    async def prompt_publish(request):
        a = actor(request)
        admin_only(a)
        v = version_for(request.path_params["id"])
        data = await body(request, PublishIn)
        checks = checks_for(v)
        if not checks["can_publish"]:
            raise HTTPError(409, "Not ready to publish", checks["blockers"])
        if checks["protected"] and not data.confirm_protected:
            raise HTTPError(409, "This version changes protected safety rules. Confirm to publish.",
                            [p["label"] for p in checks["protected"]])
        if data.note is not None:
            db.set_prompt_note(v["id"], data.note)
        db.activate_prompt_version(v["id"], a.id)
        return ok({"published": v["id"], "number": v["number"]})

    async def prompt_restore(request):
        a = actor(request)
        admin_only(a)
        v = version_for(request.path_params["id"])
        if v["status"] != "ARCHIVED":
            raise HTTPError(409, "Only earlier published versions can be restored")
        if pr.structural_problems(v["files"]):
            raise HTTPError(409, "This version no longer passes the structural checks")
        db.activate_prompt_version(v["id"], a.id)
        return ok({"restored": v["id"], "number": v["number"]})

    # ================= settings & Google Drive (administrator) =================
    async def settings_get(request):
        a = actor(request)
        admin_only(a)
        return ok({"drive": ds.status(db, settings), "provider": settings.ai_provider, "time_zone": settings.timezone,
                   "min_fit": settings.min_fit})

    async def settings_drive(request):
        a = actor(request)
        admin_only(a)
        data = await body(request, DriveOptionsIn)
        db.set_setting("drive.options", {**ds.options(db), **data.model_dump(exclude_unset=True)})
        return ok(ds.status(db, settings))

    async def google_connect(request):
        a = actor(request)
        admin_only(a)
        drive = ds.get_drive(db, settings)
        if not drive:
            raise HTTPError(409, "Add RTS_GOOGLE_CLIENT_ID and RTS_GOOGLE_CLIENT_SECRET to .env and restart first")
        expected = urlparse(public_url(settings)).netloc
        if request.url.netloc != expected:
            # Google returns to the registered address; the sign-in cookie only exists on the address in use.
            msg = f"Open the app at {public_url(settings)}, sign in there, then click Connect Google Drive again."
            return RedirectResponse(f"/#/settings?drive_error={quote(msg)}", status_code=302)
        url, state = drive.auth_url()
        db.set_setting("drive.oauth_state", {"state": state, "user": a.id,
                                             "expires": (datetime.now(timezone.utc).timestamp() + 600)})
        return RedirectResponse(url, status_code=302)

    async def google_callback(request):
        q = request.query_params
        back = lambda msg, ok_=False: RedirectResponse(
            "/#/settings?" + ("drive=connected" if ok_ else "drive_error=" + quote(msg)), status_code=302)
        try:
            a = actor(request)
        except HTTPError:
            return back("Please sign in as the administrator, then connect again.")
        if not a.is_admin:
            return back("Only the administrator can connect Google Drive.")
        saved = db.get_setting("drive.oauth_state") or {}
        db.set_setting("drive.oauth_state", None)
        if q.get("error"):
            return back(f"Google said: {q.get('error')}")
        if not saved or saved.get("state") != q.get("state") or saved.get("user") != a.id \
                or saved.get("expires", 0) < datetime.now(timezone.utc).timestamp():
            return back("The sign-in link expired or didn't match. Please try again.")
        drive = ds.get_drive(db, settings)
        try:
            await run_in_threadpool(drive.exchange_code, q.get("code", ""))
            await run_in_threadpool(drive.root_folder)
        except Exception as e:
            return back(str(e))
        return back("", True)

    async def google_disconnect(request):
        a = actor(request)
        admin_only(a)
        drive = ds.get_drive(db, settings)
        if drive and drive.connected:
            await run_in_threadpool(drive.disconnect)
        return ok(ds.status(db, settings))

    async def run_drive_upload(request):
        a = actor(request)
        require(a, "resumes.tailor")
        r = run_for(a, request.path_params["id"])
        st_ = ds.status(db, settings)
        if not st_["connected"]:
            raise HTTPError(409, "Google Drive isn't connected. The administrator can connect it in Settings.")
        if r["status"] != "DONE":
            raise HTTPError(409, "This resume isn't ready yet")
        info = await run_in_threadpool(ds.upload_run, db, settings, r["id"])
        if not info or info.get("error"):
            raise HTTPError(502, (info or {}).get("error") or "Upload failed")
        return ok({"link": info["link"]})

    # ================= web =================
    async def index(request):
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def on_error(request, exc: HTTPError):
        payload = {"error": exc.message}
        if exc.details:
            payload["details"] = exc.details
        return JSONResponse(payload, status_code=exc.status)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if start_workers:
            dispatcher.start()
        yield
        if start_workers:
            dispatcher.stop()

    R = Route
    app = Starlette(
        routes=[
            R("/", index),
            Mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static"),
            R("/api/health", health),
            R("/api/cron/sweep", cron_sweep),
            R("/api/auth/state", auth_state),
            R("/api/auth/setup", auth_setup, methods=["POST"]),
            R("/api/auth/login", auth_login, methods=["POST"]),
            R("/api/auth/logout", auth_logout, methods=["POST"]),
            R("/api/invites/accept", invite_accept, methods=["POST"]),
            R("/api/invites/{token}", invite_info),
            R("/api/me", me),
            R("/api/me/password", me_password, methods=["POST"]),
            R("/api/me/api-token", me_api_token, methods=["POST"]),
            R("/api/meta", meta),
            R("/api/dashboard", dashboard),
            R("/api/candidates", candidates, methods=["GET", "POST"]),
            R("/api/candidates/{id}", candidate, methods=["GET", "PUT", "DELETE"]),
            R("/api/candidates/{id}/assignees", candidate_assignees, methods=["PUT"]),
            R("/api/candidates/{id}/stats", candidate_stats),
            R("/api/jobs", jobs, methods=["GET", "POST"]),
            R("/api/jobs/{id}", job, methods=["GET", "PATCH", "DELETE"]),
            R("/api/candidates/{id}/tailor", candidate_tailor, methods=["POST"]),
            R("/api/screen", screen_route),
            R("/api/runs", runs, methods=["GET", "POST"]),
            R("/api/runs/bulk", runs_bulk, methods=["POST"]),
            R("/api/runs/{id}", run),
            R("/api/runs/{id}/report", run_report),
            R("/api/runs/{id}/resume.{ext:str}", run_file),
            R("/api/applications", applications, methods=["GET", "POST"]),
            R("/api/applications/{id}", application, methods=["PATCH", "DELETE"]),
            R("/api/team", team),
            R("/api/team/invites", team_invite, methods=["POST"]),
            R("/api/team/invites/{id}", team_invite_delete, methods=["DELETE"]),
            R("/api/team/members/{id}", team_member, methods=["PATCH"]),
            R("/api/settings", settings_get),
            R("/api/settings/drive", settings_drive, methods=["PUT"]),
            R("/api/settings/drive/disconnect", google_disconnect, methods=["POST"]),
            R("/api/google/connect", google_connect),
            R("/api/google/callback", google_callback),
            R("/api/runs/{id}/drive", run_drive_upload, methods=["POST"]),
            R("/api/prompts", prompts_home),
            R("/api/prompts/versions", prompt_versions, methods=["POST"]),
            R("/api/prompts/versions/{id}", prompt_version, methods=["GET", "DELETE"]),
            R("/api/prompts/versions/{id}/files", prompt_files, methods=["PUT"]),
            R("/api/prompts/versions/{id}/proposals", prompt_propose, methods=["POST"]),
            R("/api/prompts/versions/{id}/tests", test_round_create, methods=["POST"]),
            R("/api/prompts/versions/{id}/publish", prompt_publish, methods=["POST"]),
            R("/api/prompts/versions/{id}/restore", prompt_restore, methods=["POST"]),
            R("/api/prompts/proposals/{id}/apply", proposal_apply, methods=["POST"]),
            R("/api/prompts/test-cases", test_cases, methods=["GET", "POST"]),
            R("/api/prompts/test-cases/{id}", test_case, methods=["DELETE"]),
            R("/api/prompts/rounds/{id}", test_round),
        ],
        exception_handlers={HTTPError: on_error},
        lifespan=lifespan,
    )
    app.state.db, app.state.settings, app.state.dispatcher = db, settings, dispatcher
    return app
