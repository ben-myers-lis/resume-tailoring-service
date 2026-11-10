"""Prompt Studio tests: versions, permissions, proposals, test rounds, publish gating, rollback."""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_service import JOB, PW, STRONG, TITLE_ONLY, Client, free_port  # noqa: E402
from service.api import create_app  # noqa: E402
from service.config import Settings  # noqa: E402
from support import ON_PG, make_db, make_settings  # noqa: E402
from service.db import DB  # noqa: E402
from service import prompts as pr  # noqa: E402

DRAFT = "prompts/02_draft.txt"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rtsp")
    settings = make_settings(ai_provider="mock", db_path=tmp / "app.db", files_dir=tmp / "files", min_fit=40,
                        workers=3, mock_delay=0.02, make_pdf=False, timezone="America/New_York",
                        password_iterations=1000)
    app = create_app(settings)
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    admin = Client(base)
    admin.call("POST", "/api/auth/setup", {"name": "Admin", "email": "admin@example.com", "password": PW})
    ids = {"strong": admin.call("POST", "/api/candidates", STRONG)[1]["id"],
           "title_only": admin.call("POST", "/api/candidates", TITLE_ONLY)[1]["id"],
           "job": admin.call("POST", "/api/jobs", JOB)[1]["id"]}
    _, inv = admin.call("POST", "/api/team/invites", {"email": "pat@example.com", "permissions": [],
                                                      "candidate_ids": [ids["strong"]]})
    member = Client(base)
    _, acc = member.call("POST", "/api/invites/accept", {"token": inv["token"], "name": "Pat", "password": PW})
    ids["member"] = acc["user"]["id"]
    yield {"base": base, "admin": admin, "member": member, "ids": ids, "settings": settings}
    srv.should_exit = True
    t.join(10)


def wait_round(c: Client, rid: str, timeout=60) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        _, r = c.call("GET", f"/api/prompts/rounds/{rid}")
        if r["state"] != "RUNNING":
            return r
        time.sleep(0.2)
    raise TimeoutError(rid)


def test_v1_seeded_and_active(env):
    _, home = env["admin"].call("GET", "/api/prompts")
    assert [(v["number"], v["status"], v["pipeline"]) for v in home["versions"]] == \
        [(2, "ACTIVE", "fast"), (1, "ARCHIVED", "full")]       # new installs start on the fast v2
    assert home["can_publish"] is True


def test_permission_required_and_granted(env):
    m, a, ids = env["member"], env["admin"], env["ids"]
    assert m.call("GET", "/api/prompts")[0] == 403
    assert a.call("PATCH", f"/api/team/members/{ids['member']}", {"permissions": ["prompts.manage"]})[0] == 200
    code, home = m.call("GET", "/api/prompts")
    assert code == 200 and home["can_publish"] is False


def test_member_drafts_tests_but_cannot_publish(env):
    m, a, ids = env["member"], env["admin"], env["ids"]
    # test cases: only for visible candidates
    assert m.call("POST", "/api/prompts/test-cases", {"candidate_id": ids["title_only"], "job_id": ids["job"]})[0] == 404
    code, tc = m.call("POST", "/api/prompts/test-cases", {"candidate_id": ids["strong"], "job_id": ids["job"]})
    assert code == 201
    code, v = m.call("POST", "/api/prompts/versions", {"note": "Shorter summaries"})
    assert code == 201 and v["status"] == "DRAFT" and v["number"] == 3
    _, full = m.call("GET", f"/api/prompts/versions/{v['id']}")
    text = full["files"][DRAFT] + "\nKeep the summary under 50 words."
    code, saved = m.call("PUT", f"/api/prompts/versions/{v['id']}/files", {"files": {DRAFT: text}})
    assert code == 200 and saved["checks"]["test"]["state"] == "NONE"
    _, full = m.call("GET", f"/api/prompts/versions/{v['id']}")
    assert full["diff"][0]["file"] == DRAFT and any(o["type"] == "add" and "under 50 words" in o["text"] for o in full["diff"][0]["ops"])
    code, rnd = m.call("POST", f"/api/prompts/versions/{v['id']}/tests", {"compare": True})
    assert code == 202
    done = wait_round(m, rnd["id"])
    assert done["state"] == "PASSED"
    case = done["cases"][0]
    assert case["draft"]["status"] == "DONE" and case["compare"]["status"] == "DONE"
    assert case["draft"]["resume_markdown"]
    assert m.call("POST", f"/api/prompts/versions/{v['id']}/publish", {})[0] == 403
    env["draft_id"] = v["id"]


def test_test_runs_hidden_from_live_lists(env):
    _, runs = env["admin"].call("GET", "/api/runs")
    assert all(r["purpose"] == "LIVE" for r in runs)
    _, d = env["admin"].call("GET", "/api/dashboard")
    assert sum(d["resumes_this_week"].values()) == 0


def test_publish_gating_and_version_tracking(env):
    a, ids, vid = env["admin"], env["ids"], env["draft_id"]
    # edit after testing -> test is stale -> blocked
    _, full = a.call("GET", f"/api/prompts/versions/{vid}")
    a.call("PUT", f"/api/prompts/versions/{vid}/files", {"files": {DRAFT: full["files"][DRAFT] + "\nOne more line."}})
    code, err = a.call("POST", f"/api/prompts/versions/{vid}/publish", {})
    assert code == 409 and any("test round" in b for b in err["details"])
    _, rnd = a.call("POST", f"/api/prompts/versions/{vid}/tests", {"compare": False})
    assert wait_round(a, rnd["id"])["state"] == "PASSED"
    code, res = a.call("POST", f"/api/prompts/versions/{vid}/publish", {"note": "Shorter summaries"})
    assert code == 200 and res["number"] == 3
    _, home = a.call("GET", "/api/prompts")
    assert {v["number"]: v["status"] for v in home["versions"]} == {3: "ACTIVE", 2: "ARCHIVED", 1: "ARCHIVED"}
    # published versions are read-only
    assert a.call("PUT", f"/api/prompts/versions/{vid}/files", {"files": {DRAFT: "x"}})[0] == 409
    # new live resumes record v2
    _, run = a.call("POST", "/api/runs", {"candidate_id": ids["strong"], "job_id": ids["job"]})
    done = a.wait(run["id"])
    assert done["prompt_version"] == "v3" and done["prompt_version_id"] == vid


def test_protected_rules_need_confirmation(env):
    a = env["admin"]
    _, v = a.call("POST", "/api/prompts/versions", {})
    _, full = a.call("GET", f"/api/prompts/versions/{v['id']}")
    core = full["files"]["prompts/00_core.txt"].replace("Do not invent projects", "Avoid inventing projects")
    a.call("PUT", f"/api/prompts/versions/{v['id']}/files", {"files": {"prompts/00_core.txt": core}})
    _, full = a.call("GET", f"/api/prompts/versions/{v['id']}")
    assert [p["id"] for p in full["checks"]["protected"]] == ["P03"]
    _, rnd = a.call("POST", f"/api/prompts/versions/{v['id']}/tests", {"compare": False})
    wait_round(a, rnd["id"])
    code, err = a.call("POST", f"/api/prompts/versions/{v['id']}/publish", {})
    assert code == 409 and "protected" in err["error"]
    assert a.call("POST", f"/api/prompts/versions/{v['id']}/publish", {"confirm_protected": True})[0] == 200
    env["v3"] = v["id"]


def test_structural_problems_block_publish(env):
    a = env["admin"]
    _, v = a.call("POST", "/api/prompts/versions", {})
    a.call("PUT", f"/api/prompts/versions/{v['id']}/files", {"files": {DRAFT: "Write a resume."}})
    _, full = a.call("GET", f"/api/prompts/versions/{v['id']}")
    assert any("STAGE: DRAFT" in p for p in full["checks"]["structural"])
    assert not full["checks"]["can_publish"]
    assert a.call("PUT", f"/api/prompts/versions/{v['id']}/files", {"files": {"schemas/draft.schema.json": "{}"}})[0] == 422
    assert a.call("DELETE", f"/api/prompts/versions/{v['id']}")[0] == 200


def test_restore_earlier_version(env):
    a = env["admin"]
    _, home = a.call("GET", "/api/prompts")
    v1 = next(v for v in home["versions"] if v["number"] == 1)
    assert a.call("POST", f"/api/prompts/versions/{v1['id']}/restore")[0] == 200
    _, home = a.call("GET", "/api/prompts")
    assert next(v["status"] for v in home["versions"] if v["number"] == 1) == "ACTIVE"
    assert env["member"].call("POST", f"/api/prompts/versions/{v1['id']}/restore")[0] == 403


def test_proposals_accept_and_reject(env):
    m = env["member"]
    _, v = m.call("POST", "/api/prompts/versions", {})
    code, prop = m.call("POST", f"/api/prompts/versions/{v['id']}/proposals",
                        {"instruction": "Lead with leadership experience for senior roles"})
    assert code == 201 and prop["edits"][0]["status"] == "PENDING" and prop["edits"][0]["file"] == DRAFT
    code, res = m.call("POST", f"/api/prompts/proposals/{prop['id']}/apply", {"decisions": [{"index": 0, "accept": True}]})
    assert code == 200 and res["applied"] == 1
    _, full = m.call("GET", f"/api/prompts/versions/{v['id']}")
    assert "Lead with leadership experience" in full["files"][DRAFT]
    assert m.call("POST", f"/api/prompts/proposals/{prop['id']}/apply", {"decisions": []})[0] == 409
    _, prop2 = m.call("POST", f"/api/prompts/versions/{v['id']}/proposals", {"instruction": "Check dates carefully in the assessment"})
    assert prop2["edits"][0]["file"] == "prompts/03_assess.txt" and prop2["edits"][0]["protected"]
    _, res2 = m.call("POST", f"/api/prompts/proposals/{prop2['id']}/apply", {"decisions": [{"index": 0, "accept": False}]})
    assert res2["applied"] == 0
    _, full = m.call("GET", f"/api/prompts/versions/{v['id']}")
    assert "Check dates carefully" not in full["files"]["prompts/03_assess.txt"]


def test_check_edit_flags_bad_find():
    files = pr.package_files()
    from service.prompt_ai import check_edit
    e = check_edit(files, {"file": "prompts/00_core.txt", "find": "the", "replace": "x", "reason": ""})
    assert e["status"] == "INVALID"
    e = check_edit(files, {"file": "prompts/00_core.txt", "find": "Do not invent projects", "replace": "x", "reason": ""})
    assert e["status"] == "PENDING" and e["protected"]


def test_claude_proposer_wiring(monkeypatch):
    from service import prompt_ai
    from service.ai import claude as claude_mod
    sent = {}
    files = pr.package_files()
    target = "Summary: approximately 45–70 words"
    assert files["prompts/00_core.txt"].count(target) == 1

    class Stream:
        def __init__(self, kw):
            sent.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            out = {"summary": "Shorter summary.", "warnings": [], "needs_code_change": ["Add a 'leadership' section"],
                   "edits": [{"file": "prompts/00_core.txt", "find": target, "replace": "Summary: approximately 35-50 words",
                              "reason": "Feedback asked for shorter summaries"}]}
            u = types.SimpleNamespace(input_tokens=30000, output_tokens=800, cache_creation_input_tokens=0, cache_read_input_tokens=0)
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="tool_use", id="t", input=out)], usage=u, stop_reason="tool_use")

    monkeypatch.setattr(claude_mod, "_client", lambda: types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **kw: Stream(kw))))
    res = prompt_ai.propose(files, "Summaries are too long", "claude",
                            [{"candidate": "A", "job": "MLE", "status": "READY", "resume": "# A\n\nSummary text"}])
    assert sent["tool_choice"] == {"type": "tool", "name": "submit_prompt_edits"}
    assert "STAGE: PROMPT MAINTENANCE" in sent["system"][0]["text"]
    user = sent["messages"][0]["content"]
    assert "<recent_test_resumes>" in user[1]["text"] and "Summaries are too long" in user[1]["text"]
    assert res["edits"][0]["status"] == "PENDING" and res["cost_usd"] > 0
    assert res["needs_code_change"] == ["Add a 'leadership' section"]


@pytest.mark.skipif(ON_PG, reason="SQLite upgrade path")
def test_upgrade_from_v02_database(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT NOT NULL,
        password_hash TEXT NOT NULL, role TEXT NOT NULL, permissions TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1,
        api_token TEXT UNIQUE, created_at TEXT NOT NULL, last_login_at TEXT);
        CREATE TABLE runs (id TEXT PRIMARY KEY, created_by TEXT NOT NULL, candidate_id TEXT NOT NULL, job_id TEXT NOT NULL,
        status TEXT NOT NULL, force INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'APPLICATION', provider TEXT,
        prompt_version TEXT, profile_version INTEGER, fit_score INTEGER, fit TEXT, delivery_status TEXT, result TEXT,
        usage TEXT, render TEXT, error TEXT, cost_usd REAL, seconds REAL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);""")
    con.close()
    db = DB(path)
    cols = db.c.columns("runs")
    assert {"prompt_version_id", "purpose", "test_round_id", "attempts"} <= cols


def test_concurrent_runs_across_versions(tmp_path):
    """Regression: worker threads share one DB connection; reads must not interleave."""
    from service.pipeline import ensure_prompt_versions
    from service.worker import WorkerPool
    settings = make_settings(ai_provider="mock", db_path=tmp_path / "s.db", files_dir=tmp_path / "f", min_fit=40,
                        workers=8, mock_delay=0.0, make_pdf=False)
    db = make_db(settings)
    ensure_prompt_versions(db)
    u = db.create_user("s@example.com", "S", "x", "ADMIN", [])
    v1 = db.active_prompt_version()
    v2 = db.create_prompt_version(v1["files"], "DRAFT", u["id"], v1["id"], None)
    cands = [db.create_candidate(u["id"], {**STRONG, "name": f"C{i}"}) for i in range(6)]
    job = db.create_job(u["id"], JOB)
    ids = [db.create_run(u["id"], c["id"], job["id"], True, "APPLICATION",
                         prompt_version_id=(v1 if i % 2 else v2)["id"], purpose="TEST")["id"]
           for i in range(5) for c in cands]
    pool = WorkerPool(db, settings, poll_seconds=0.05)
    pool.start()
    try:
        end = time.time() + 120
        while time.time() < end and any(db.get_run(r)["status"] in ("QUEUED", "RUNNING") for r in ids):
            time.sleep(0.2)
    finally:
        pool.stop()
    statuses = [db.get_run(r)["status"] for r in ids]
    assert statuses.count("DONE") == len(ids), [db.get_run(r)["error"] for r in ids if db.get_run(r)["status"] != "DONE"][:3]
