"""End-to-end tests: real HTTP server, real sessions, temporary database, mock AI."""
from __future__ import annotations

import http.cookiejar
import json
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.api import create_app  # noqa: E402
from service.config import Settings  # noqa: E402
from service.db import DB  # noqa: E402
from service.stats import today, week_start  # noqa: E402

PW = "Str0ngPassword"
TZ = "America/New_York"
STRONG = {
    "name": "Avery Lindqvist", "email": "avery@example.com", "linkedin": "linkedin.com/in/avery-example",
    "employment": [
        {"company": "Quantia Analytics", "title": "Senior Machine Learning Engineer", "start_date": "2022-03",
         "end_date": None, "notes": "I built PyTorch training pipelines for churn models."},
        {"company": "Quantia Analytics", "title": "Machine Learning Engineer", "start_date": "2020-01", "end_date": "2022-02"},
        {"company": "Tidewater Systems", "title": "Software Engineer", "start_date": "2017-06", "end_date": "2019-12"}],
    "education": ["M.S. Computer Science, State University, 2017"],
}
TITLE_ONLY = {
    "name": "Jordan Okafor", "email": "jordan@example.com",
    "employment": [{"company": "Pinecrest Labs", "title": "AI Engineer", "start_date": "2024-02", "end_date": None},
                   {"company": "Ridgeway Digital", "title": "Software Engineer", "start_date": "2019-08", "end_date": "2024-01"}],
}
MISMATCH = {
    "name": "Daniel Whitcombe",
    "employment": [{"company": "Ashford & Lane LLP", "title": "Senior Accountant", "start_date": "2021-05", "end_date": None}],
    "education": ["B.S. Accounting, Central State University, 2018"],
}
JOB = {"company": "Lumen Health AI", "role": "Senior Machine Learning Engineer", "url": "https://jobs.example.com/123",
       "text": "Required: 5+ years of software or ML engineering experience; Python; PyTorch or TensorFlow; "
               "experience deploying models to production. Preferred: MLflow."}
SCIENTIST = {"company": "Helix Research Labs", "role": "Senior Applied Scientist, NLP",
             "text": "Required: PhD in Computer Science or related field; 4+ years of applied research in NLP; PyTorch."}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """A browser-like client: keeps the session cookie and sends the X-Requested-With header."""

    def __init__(self, base: str, csrf: bool = True, bearer: str | None = None):
        self.base, self.csrf, self.bearer = base, csrf, bearer
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, data=None, raw=False):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(data).encode() if data is not None else None)
        req.add_header("Content-Type", "application/json")
        if self.csrf:
            req.add_header("X-Requested-With", "rts")
        if self.bearer:
            req.add_header("Authorization", f"Bearer {self.bearer}")
        try:
            with self.opener.open(req, timeout=30) as r:
                payload = r.read()
                return r.status, (payload if raw else json.loads(payload or b"null"))
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body or b"null")
            except json.JSONDecodeError:
                return e.code, body

    def login(self, email, password=PW):
        return self.call("POST", "/api/auth/login", {"email": email, "password": password})

    def wait(self, run_id: str, timeout: float = 60) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            _, r = self.call("GET", f"/api/runs/{run_id}")
            if r["status"] not in ("QUEUED", "RUNNING"):
                return r
            time.sleep(0.2)
        raise TimeoutError(run_id)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rts")
    settings = Settings(ai_provider="mock", db_path=tmp / "app.db", files_dir=tmp / "files", min_fit=40,
                        workers=3, mock_delay=0.05, make_pdf=True, allow_illustrative=False,
                        timezone=TZ, password_iterations=1000)
    app = create_app(settings)
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    admin = Client(base)
    _, state = admin.call("GET", "/api/auth/state")
    assert state["needs_setup"] is True
    code, _ = admin.call("POST", "/api/auth/setup", {"name": "Michael Admin", "email": "admin@example.com", "password": PW})
    assert code == 201
    ids = {k: admin.call("POST", "/api/candidates", v)[1]["id"]
           for k, v in {"strong": STRONG, "title_only": TITLE_ONLY, "mismatch": MISMATCH}.items()}
    ids["job"] = admin.call("POST", "/api/jobs", JOB)[1]["id"]
    ids["scientist"] = admin.call("POST", "/api/jobs", SCIENTIST)[1]["id"]

    # Invite a member who may tailor/download, assigned to "strong" only.
    code, inv = admin.call("POST", "/api/team/invites", {"email": "sam@example.com",
                                                          "permissions": ["resumes.tailor"],
                                                          "candidate_ids": [ids["strong"]]})
    assert code == 201 and "/#/invite/" in inv["link"]
    member = Client(base)
    code, info = member.call("GET", f"/api/invites/{inv['token']}")
    assert code == 200 and info["email"] == "sam@example.com"
    code, acc = member.call("POST", "/api/invites/accept", {"token": inv["token"], "name": "Sam Member", "password": PW})
    assert code == 201 and acc["user"]["role"] == "MEMBER"
    ids["member"] = acc["user"]["id"]

    yield {"base": base, "admin": admin, "member": member, "ids": ids, "settings": settings}
    srv.should_exit = True
    t.join(10)


# ---------------- accounts ----------------
def test_setup_only_once_and_login(env):
    anon = Client(env["base"])
    assert anon.call("POST", "/api/auth/setup", {"name": "X", "email": "x@example.com", "password": PW})[0] == 409
    assert anon.call("GET", "/api/candidates")[0] == 401
    assert anon.login("admin@example.com", "wrong-Passw0rd")[0] == 401
    code, me = anon.login("ADMIN@example.com")            # email is case-insensitive
    assert code == 200 and me["user"]["role"] == "ADMIN"
    assert anon.call("POST", "/api/auth/logout")[0] == 200
    assert anon.call("GET", "/api/me")[0] == 401


def test_weak_password_rejected(env):
    code, inv = env["admin"].call("POST", "/api/team/invites", {"email": "weak@example.com"})
    c = Client(env["base"])
    code, body = c.call("POST", "/api/invites/accept", {"token": inv["token"], "name": "W", "password": "short"})
    assert code == 422
    assert c.call("GET", f"/api/invites/{inv['token']}")[0] == 200     # invite still usable


def test_login_throttle(env):
    c = Client(env["base"])
    for _ in range(8):
        c.login("nobody@example.com", "Wrong-passw0rd")
    assert c.login("nobody@example.com", "Wrong-passw0rd")[0] == 429


def test_cookie_requests_need_csrf_header(env):
    c = Client(env["base"], csrf=False)
    c.opener.add_handler(urllib.request.HTTPCookieProcessor(env["admin"].jar))
    c.jar = env["admin"].jar
    c.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(env["admin"].jar))
    assert c.call("GET", "/api/candidates")[0] == 200                 # reads are fine
    assert c.call("POST", "/api/jobs", JOB)[0] == 403                 # changes need the header


def test_invite_link_single_use(env):
    _, inv = env["admin"].call("POST", "/api/team/invites", {"email": "once@example.com"})
    c = Client(env["base"])
    assert c.call("POST", "/api/invites/accept", {"token": inv["token"], "name": "Once", "password": PW})[0] == 201
    assert Client(env["base"]).call("POST", "/api/invites/accept",
                                    {"token": inv["token"], "name": "Twice", "password": PW})[0] == 404


def test_api_token(env):
    _, tok = env["admin"].call("POST", "/api/me/api-token")
    bot = Client(env["base"], csrf=False, bearer=tok["api_token"])
    assert bot.call("GET", "/api/candidates")[0] == 200
    assert bot.call("POST", "/api/jobs", {**JOB, "role": "Via API"})[0] == 201


# ---------------- permissions & visibility ----------------
def test_member_sees_only_assigned(env):
    m, ids = env["member"], env["ids"]
    _, cands = m.call("GET", "/api/candidates")
    assert [c["id"] for c in cands] == [ids["strong"]]
    assert m.call("GET", f"/api/candidates/{ids['title_only']}")[0] == 404
    assert m.call("GET", f"/api/candidates/{ids['title_only']}/stats")[0] == 404
    assert m.call("POST", "/api/runs", {"candidate_id": ids["title_only"], "job_id": ids["job"]})[0] == 404
    assert m.call("POST", "/api/applications", {"candidate_id": ids["title_only"], "role": "X"})[0] == 404
    assert m.call("GET", f"/api/applications?candidate_id={ids['title_only']}")[0] == 404


def test_member_without_permissions_is_blocked(env):
    m, ids = env["member"], env["ids"]
    assert m.call("POST", "/api/candidates", TITLE_ONLY)[0] == 403          # administrator only
    assert m.call("PUT", f"/api/candidates/{ids['strong']}", STRONG)[0] == 403
    assert m.call("DELETE", f"/api/candidates/{ids['strong']}")[0] == 403   # admin only
    assert m.call("GET", "/api/team")[0] == 403
    assert m.call("POST", "/api/team/invites", {"email": "z@example.com"})[0] == 403
    assert m.call("PUT", f"/api/candidates/{ids['strong']}/assignees", {"user_ids": []})[0] == 403
    assert m.call("GET", "/api/jobs")[0] == 200
    code, err = env["admin"].call("POST", "/api/team/invites", {"email": "q@example.com", "permissions": ["jobs.add"]})
    assert code == 422                                                      # retired permission


def test_member_tailors_downloads_and_logs_application(env):
    m, ids = env["member"], env["ids"]
    code, run = m.call("POST", "/api/runs", {"candidate_id": ids["strong"], "job_id": ids["job"]})
    assert code == 202
    done = m.wait(run["id"])
    assert done["status"] == "DONE" and done["delivery_status"] == "READY", done.get("error")
    assert "Built **PyTorch** training pipelines for churn models" in done["resume_markdown"]
    code, pdf = m.call("GET", done["files"]["pdf"], raw=True)
    assert code == 200 and pdf[:4] == b"%PDF"
    code, app = m.call("POST", "/api/applications", {"candidate_id": ids["strong"], "run_id": run["id"]})
    assert code == 201
    assert app["company"] == "Lumen Health AI" and app["job_url"] == JOB["url"] and app["applied_by_name"] == "Sam Member"
    assert app["applied_on"] == today(TZ).isoformat()
    assert m.call("POST", "/api/applications", {"candidate_id": ids["strong"], "run_id": run["id"]})[0] == 409
    _, r = m.call("GET", f"/api/runs/{run['id']}")
    assert len(r["applications"]) == 1
    code, upd = m.call("PATCH", f"/api/applications/{app['id']}", {"status": "INTERVIEW", "notes": "Phone screen Tue"})
    assert code == 200 and upd["status"] == "INTERVIEW"


def test_per_day_and_per_week_counts(env):
    a, ids = env["admin"], env["ids"]
    cid = ids["title_only"]
    t = today(TZ)
    days = [t, t, t - timedelta(days=1), week_start(t) - timedelta(days=1), t - timedelta(days=20)]
    for i, d in enumerate(days):
        code, _ = a.call("POST", "/api/applications", {"candidate_id": cid, "company": f"Co {i}",
                                                       "role": "AI Engineer", "applied_on": d.isoformat()})
        assert code == 201
    _, s = a.call("GET", f"/api/candidates/{cid}/stats?days=30&weeks=12")
    per_day = {x["date"]: x["count"] for x in s["per_day"]}
    assert s["today"] == 2 and s["total"] == 5
    assert per_day[t.isoformat()] == 2
    assert per_day[(t - timedelta(days=20)).isoformat()] == 1
    expected_week = sum(1 for d in days if d >= week_start(t))
    assert s["this_week"] == expected_week
    assert sum(w["count"] for w in s["per_week"]) == 5
    assert s["per_week"][-1]["week_start"] == week_start(t).isoformat()
    assert len(s["per_day"]) == 30 and len(s["per_week"]) == 12
    _, cands = a.call("GET", "/api/candidates")
    row = next(c for c in cands if c["id"] == cid)
    assert row["applications"] == {"today": 2, "this_week": expected_week, "total": 5}


def test_admin_dashboard_by_member(env):
    _, d = env["admin"].call("GET", "/api/dashboard")
    names = {m["name"]: m["count"] for m in d["this_week_by_member"]}
    assert names.get("Sam Member", 0) >= 1 and names.get("Michael Admin", 0) >= 2
    assert d["candidate_count"] >= 3
    _, md = env["member"].call("GET", "/api/dashboard")
    assert md["candidate_count"] == 1                          # members see their own scope only


def test_delete_application_rules(env):
    a, m, ids = env["admin"], env["member"], env["ids"]
    _, admins_app = a.call("POST", "/api/applications", {"candidate_id": ids["strong"], "company": "AdminCo", "role": "MLE"})
    assert m.call("DELETE", f"/api/applications/{admins_app['id']}")[0] == 403    # not theirs
    assert a.call("DELETE", f"/api/applications/{admins_app['id']}")[0] == 200


def test_admin_changes_permissions_assignments_and_deactivates(env):
    a, m, ids = env["admin"], env["member"], env["ids"]
    mid = ids["member"]
    # candidate profiles are administrator-only and the old permission can't be granted
    assert a.call("PATCH", f"/api/team/members/{mid}", {"permissions": ["resumes.tailor", "candidates.edit"]})[0] == 422
    assert m.call("POST", "/api/candidates", {**TITLE_ONLY, "name": "Member Added"})[0] == 403
    # assign another candidate from the candidate side
    a.call("PUT", f"/api/candidates/{ids['title_only']}/assignees", {"user_ids": [mid]})
    assert m.call("GET", f"/api/candidates/{ids['title_only']}")[0] == 200
    # remove tailoring: downloads blocked, files hidden
    a.call("PATCH", f"/api/team/members/{mid}", {"permissions": []})
    _, runs = m.call("GET", "/api/runs")
    rid = next(r["id"] for r in runs if r["status"] == "DONE")
    assert m.call("GET", f"/api/runs/{rid}/resume.pdf")[0] == 403
    assert m.call("GET", f"/api/runs/{rid}")[1]["files"] == {}
    assert m.call("POST", "/api/runs", {"candidate_id": ids["strong"], "job_id": ids["job"]})[0] == 403
    # the admin account can't be edited via the team endpoint
    _, team = a.call("GET", "/api/team")
    admin_id = next(u["id"] for u in team["members"] if u["role"] == "ADMIN")
    assert a.call("PATCH", f"/api/team/members/{admin_id}", {"active": False})[0] == 403
    # deactivate: signed out immediately, can't sign back in
    a.call("PATCH", f"/api/team/members/{mid}", {"active": False})
    assert m.call("GET", "/api/me")[0] == 401
    assert Client(env["base"]).login("sam@example.com")[0] == 401
    a.call("PATCH", f"/api/team/members/{mid}", {"active": True, "permissions": ["resumes.tailor"]})
    assert m.login("sam@example.com")[0] == 200


# ---------------- tailoring ----------------
def test_title_only_profile_invents_nothing(env):
    a, ids = env["admin"], env["ids"]
    _, run = a.call("POST", "/api/runs", {"candidate_id": ids["title_only"], "job_id": ids["job"]})
    done = a.wait(run["id"])
    assert done["status"] == "DONE" and done["delivery_status"] == "INCOMPLETE_EVIDENCE"
    md = done["resume_markdown"]
    assert "AI Engineer" in md and "Pinecrest Labs" in md
    assert "\n- " not in md.split("## Professional Experience")[1].split("##")[0]
    assert done["questions_for_candidate"]


def test_mismatch_skipped_then_forced(env):
    a, ids = env["admin"], env["ids"]
    code, fit = a.call("GET", f"/api/screen?candidate_id={ids['mismatch']}&job_id={ids['scientist']}")
    assert code == 200 and not fit["would_tailor"]
    _, run = a.call("POST", "/api/runs", {"candidate_id": ids["mismatch"], "job_id": ids["scientist"]})
    skipped = a.wait(run["id"])
    assert skipped["status"] == "SKIPPED" and skipped["cost_usd"] == 0
    _, run2 = a.call("POST", "/api/runs", {"candidate_id": ids["mismatch"], "job_id": ids["scientist"], "force": True})
    assert a.wait(run2["id"])["status"] == "DONE"


def test_bulk_and_illustrative_blocked(env):
    a, ids = env["admin"], env["ids"]
    code, bulk = a.call("POST", "/api/runs/bulk", {"candidate_ids": [ids["strong"], ids["title_only"]],
                                                    "job_ids": [ids["job"], ids["scientist"]]})
    assert code == 202 and bulk["queued"] == 4
    assert all(a.wait(r)["status"] in ("DONE", "SKIPPED") for r in bulk["run_ids"])
    assert a.call("POST", "/api/runs", {"candidate_id": ids["strong"], "job_id": ids["job"],
                                        "mode": "ILLUSTRATIVE"})[0] == 403


def test_delete_candidate_cascades(env):
    a, s = env["admin"], env["settings"]
    _, c = a.call("POST", "/api/candidates", {**STRONG, "name": "To Delete"})
    _, run = a.call("POST", "/api/runs", {"candidate_id": c["id"], "job_id": env["ids"]["job"]})
    a.wait(run["id"])
    a.call("POST", "/api/applications", {"candidate_id": c["id"], "run_id": run["id"]})
    folder = s.files_dir / run["id"]
    assert folder.exists()
    code, res = a.call("DELETE", f"/api/candidates/{c['id']}")
    assert code == 200 and res["runs_deleted"] == 1 and not folder.exists()
    assert a.call("GET", f"/api/runs/{run['id']}")[0] == 404
    _, apps = a.call("GET", "/api/applications")
    assert all(x["candidate_id"] != c["id"] for x in apps)


def test_web_page_served(env):
    code, html = Client(env["base"]).call("GET", "/", raw=True)
    assert code == 200 and b"<html" in html.lower()


# ---------------- pipeline-level ----------------
def test_all_package_test_cases_pass_contracts_with_mock():
    from service.ai.mock import MockAI
    from service.package import runner
    for f in sorted((ROOT / "prompt_package/test_cases").glob("case_*.json")):
        result = runner.run_pipeline(json.loads(f.read_text()), MockAI())
        assert result["delivery_status"] in {"READY", "INCOMPLETE_EVIDENCE"}, f.name


def test_claude_provider_wiring(tmp_path, monkeypatch):
    """Service with RTS_AI_PROVIDER=claude against a simulated Anthropic client."""
    from service.ai import claude as claude_mod
    from service.ai.mock import MockAI
    from service.package import stage_of
    from service.pipeline import process_run

    sent = []

    class Stream:
        def __init__(self, kw):
            self.kw = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            kw = self.kw
            sent.append(kw)
            text = "\n".join(b["text"] for b in kw["system"])
            data = json.loads(kw["messages"][0]["content"][-1]["text"].split("\n", 1)[1].rsplit("\n", 1)[0])
            out = MockAI()(instructions=text, input_data=data, output_schema={})
            usage = types.SimpleNamespace(input_tokens=5000, output_tokens=1500,
                                          cache_creation_input_tokens=0, cache_read_input_tokens=6000)
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="tool_use", id="t", input=out)],
                                         usage=usage, stop_reason="tool_use")

    monkeypatch.setattr(claude_mod, "_client",
                        lambda: types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **kw: Stream(kw))))
    settings = Settings(ai_provider="claude", db_path=tmp_path / "c.db", files_dir=tmp_path / "f",
                        min_fit=40, workers=1, make_pdf=False)
    db = DB(settings.db_path)
    u = db.create_user("c@example.com", "C", "x", "ADMIN", [])
    cand = db.create_candidate(u["id"], STRONG)
    job = db.create_job(u["id"], JOB)
    run = db.create_run(u["id"], cand["id"], job["id"], False, "APPLICATION")
    process_run(db, settings, db.claim_next_run())
    done = db.get_run(run["id"])
    assert done["status"] == "DONE", done["error"]
    assert done["provider"] == "claude" and done["cost_usd"] > 0
    assert [stage_of("\n".join(b["text"] for b in k["system"])) for k in sent] == ["NORMALIZE", "DRAFT", "ASSESS"]
    assert [k["model"] for k in sent] == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-sonnet-5-5"]
    blob = json.dumps([k["messages"] for k in sent])
    assert "Avery Lindqvist" not in blob and "avery@example.com" not in blob
    assert "Avery Lindqvist" in done["result"]["resume_markdown"]


# ---------------- paste a job description on the candidate page ----------------
PLAIN_JD = """Senior Machine Learning Engineer
Lumen Health AI

About us: we build clinical risk models.
Required: 5+ years of ML engineering; Python; PyTorch; deploying models to production.
Nice to have: MLflow."""


def test_paste_jd_and_tailor_on_candidate_page(env):
    m, ids = env["member"], env["ids"]
    code, res = m.call("POST", f"/api/candidates/{ids['strong']}/tailor", {"jd_text": PLAIN_JD})
    assert code == 202
    assert res["job"]["role"] == "Senior Machine Learning Engineer" and res["job"]["company"] == "Lumen Health AI"
    assert res["job"]["candidate_id"] == ids["strong"] and res["job"]["text"] == PLAIN_JD
    done = m.wait(res["run"]["id"])
    assert done["status"] == "DONE" and done["job_role"] == "Senior Machine Learning Engineer"
    # optional fields override detection; too-short text is rejected
    code, res2 = m.call("POST", f"/api/candidates/{ids['strong']}/tailor",
                        {"jd_text": "We need someone great. " * 3, "title": "ML Lead", "company": "Acme"})
    assert code == 202 and res2["job"]["role"] == "ML Lead" and res2["job"]["company"] == "Acme"
    assert m.call("POST", f"/api/candidates/{ids['strong']}/tailor", {"jd_text": "short"})[0] == 422
    # job history for this candidate
    _, jobs = m.call("GET", f"/api/jobs?candidate_id={ids['strong']}")
    assert {j["id"] for j in jobs} >= {res["job"]["id"], res2["job"]["id"]}
    # fixing the title also updates the application record created from it
    _, app = m.call("POST", "/api/applications", {"candidate_id": ids["strong"], "run_id": res2["run"]["id"]})
    code, j = m.call("PATCH", f"/api/jobs/{res2['job']['id']}", {"role": "Machine Learning Lead"})
    assert code == 200 and j["role"] == "Machine Learning Lead"
    _, apps = m.call("GET", f"/api/applications?candidate_id={ids['strong']}")
    assert next(x for x in apps if x["id"] == app["id"])["role"] == "Machine Learning Lead"


def test_members_cannot_see_other_candidates_jobs(env):
    a, m, ids = env["admin"], env["member"], env["ids"]
    _, res = a.call("POST", f"/api/candidates/{ids['mismatch']}/tailor", {"jd_text": PLAIN_JD, "force": True})
    jid = res["job"]["id"]
    assert m.call("GET", f"/api/jobs/{jid}")[0] == 404
    assert all(j["id"] != jid for j in m.call("GET", "/api/jobs")[1])
    assert m.call("POST", f"/api/candidates/{ids['mismatch']}/tailor", {"jd_text": PLAIN_JD})[0] == 404
    assert m.call("PATCH", f"/api/jobs/{jid}", {"role": "X"})[0] == 404


def test_tailor_needs_permission(env):
    a, ids = env["admin"], env["ids"]
    _, inv = a.call("POST", "/api/team/invites", {"email": "viewer@example.com", "candidate_ids": [ids["strong"]]})
    v = Client(env["base"])
    v.call("POST", "/api/invites/accept", {"token": inv["token"], "name": "Viewer", "password": PW})
    assert v.call("POST", f"/api/candidates/{ids['strong']}/tailor", {"jd_text": PLAIN_JD})[0] == 403


def test_jd_title_company_detection():
    from service.jd import guess_title_company as g
    assert g("Job Title: Data Analyst\nCompany: Harborview Insurance") == ("Data Analyst", "Harborview Insurance")
    assert g("Northwind Commerce is hiring an AI Platform Engineer who will own serving.") == ("AI Platform Engineer", "Northwind Commerce")
    assert g("Staff Machine Learning Engineer - Orbital Robotics\n\nRequired: 8+ years") == ("Staff Machine Learning Engineer", "Orbital Robotics")
    assert g("We're looking for a Junior AI Engineer to join Sparrow Labs. You will build") == ("Junior AI Engineer", "Sparrow Labs")
    assert g("Senior Data Engineer\nRemote, US\nWe build data platforms.") == ("Senior Data Engineer", None)
    assert g("Responsibilities:\n- write code") == (None, None)


def test_screening_reads_field_from_text_when_title_unknown():
    from datetime import date
    from service.screening import screen
    mle = {"employment": [{"title": "Senior Machine Learning Engineer", "company": "Q", "start_date": "2020-01", "end_date": None}],
           "education": [], "certifications": []}
    acct = {"employment": [{"title": "Senior Accountant", "company": "A", "start_date": "2019-05", "end_date": None}],
            "education": [], "certifications": []}
    job = {"role": "Untitled job", "text": "Build LLM-powered product features. Required: 3+ years of software engineering; Python."}
    assert screen(mle, job, date(2026, 11, 8))["fit_score"] >= 80
    assert screen(acct, job, date(2026, 11, 8))["fit_score"] < 40
