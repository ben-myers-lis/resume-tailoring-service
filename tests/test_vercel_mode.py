"""Vercel mode: runs go through a queue (simulated here), files live in the database, PDFs come
from ReportLab, and the sweep recovers lost or abandoned runs."""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from service import jobs  # noqa: E402
from service.api import create_app  # noqa: E402
from service.db import pg_sql  # noqa: E402
from support import make_db, make_settings  # noqa: E402
from test_service import JOB, PW, STRONG, Client, free_port  # noqa: E402


class FakeQueue:
    """Stands in for vercel.queue.sync.QueueClient: dedupes idempotency keys like the real service and
    'delivers' each message by calling the subscriber's handler in a background thread."""

    def __init__(self, deliver=True):
        self.sent: list[tuple[str, dict, str]] = []
        self.keys: set[str] = set()
        self.deliver, self.db, self.settings = deliver, None, None
        self.lock = threading.Lock()

    def send(self, topic, payload, idempotency_key=None):
        with self.lock:
            if idempotency_key in self.keys:
                return None
            self.keys.add(idempotency_key)
            self.sent.append((topic, payload, idempotency_key))
        if self.deliver:
            threading.Thread(target=jobs.process_message, args=(payload, self.db, self.settings), daemon=True).start()
        return f"msg_{len(self.sent)}"


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


@pytest.fixture(scope="module")
def vapp(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("vercel")
    settings = make_settings(ai_provider="mock", db_path=tmp / "app.db", files_dir=tmp / "scratch", min_fit=40,
                             workers=2, mock_delay=0.01, make_pdf=True, pdf_engine="reportlab",
                             timezone="America/New_York", password_iterations=1000, queue="vercel",
                             file_store="db", cron_secret="cron-secret-0123456789")
    app = create_app(settings)
    fake = FakeQueue()
    fake.db, fake.settings = app.state.db, settings
    app.state.dispatcher._client = fake
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    admin = Client(base)
    assert admin.call("POST", "/api/auth/setup", {"name": "Admin", "email": "admin@example.com", "password": PW})[0] == 201
    cid = admin.call("POST", "/api/candidates", STRONG)[1]["id"]
    yield {"app": app, "admin": admin, "base": base, "cid": cid, "fake": fake, "settings": settings}
    srv.should_exit = True


def test_health_reports_vercel_mode(vapp):
    _, h = vapp["admin"].call("GET", "/api/health")
    assert h["queue"] == "vercel" and h["files"] == "db"


def test_tailor_goes_through_queue_and_files_come_from_db(vapp):
    a, fake = vapp["admin"], vapp["fake"]
    code, res = a.call("POST", f"/api/candidates/{vapp['cid']}/tailor", {"jd_text": JOB["text"], "title": JOB["role"],
                                                                          "company": JOB["company"]})
    assert code == 202
    rid = res["run"]["id"]
    assert any(p == {"run_id": rid} and k == f"{rid}-0" and t == jobs.TOPIC for t, p, k in fake.sent)
    run = a.wait(rid)
    assert run["status"] == "DONE", run.get("error")
    assert set(run["files"]) >= {"pdf", "docx"}
    # nothing left on the (temporary) disk; the files are in the database
    assert not (vapp["settings"].files_dir / rid).exists()
    code, pdf = a.call("GET", f"/api/runs/{rid}/resume.pdf", raw=True)
    assert code == 200 and pdf.startswith(b"%PDF")
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(pdf))
    text = "".join(p.extract_text() for p in reader.pages)
    assert STRONG["name"] in text and "PROFESSIONAL EXPERIENCE" in text
    assert 1 <= len(reader.pages) <= 2
    code, docx = a.call("GET", f"/api/runs/{rid}/resume.docx", raw=True)
    assert code == 200 and docx[:2] == b"PK"


def test_duplicate_delivery_is_processed_once(vapp):
    db, s = vapp["app"].state.db, vapp["settings"]
    u = db.get_user_auth("admin@example.com")
    job = db.create_job(u["id"], JOB, candidate_id=vapp["cid"])
    run = db.create_run(u["id"], vapp["cid"], job["id"], True, "APPLICATION")
    results = []
    threads = [threading.Thread(target=lambda: results.append(jobs.process_message({"run_id": run["id"]}, db, s)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(results) == ["not-claimed"] * 3 + ["processed"]
    done = db.get_run(run["id"])
    assert done["status"] == "DONE" and done["attempts"] == 1
    assert jobs.process_message({"run_id": run["id"]}, db, s) == "not-claimed"     # late redelivery
    assert jobs.process_message({}, db, s) == "ignored"


def test_sweep_resends_lost_runs_retries_and_fails_abandoned_ones(tmp_path):
    s = make_settings(ai_provider="mock", db_path=tmp_path / "s.db", files_dir=tmp_path / "f", mock_delay=0.0,
                      make_pdf=False, queue="vercel", file_store="db", run_timeout=600, max_attempts=2)
    db = make_db(s)
    u = db.create_user("s@example.com", "S", "x", "ADMIN", [])
    cand = db.create_candidate(u["id"], STRONG)
    job = db.create_job(u["id"], JOB)
    d = jobs.make_dispatcher(db, s)
    fake = FakeQueue(deliver=False)
    d._client = fake

    lost = db.create_run(u["id"], cand["id"], job["id"], True, "APPLICATION")       # send failed
    fresh = db.create_run(u["id"], cand["id"], job["id"], True, "APPLICATION")      # just queued
    stuck = db.create_run(u["id"], cand["id"], job["id"], True, "APPLICATION")      # function timed out once
    dead = db.create_run(u["id"], cand["id"], job["id"], True, "APPLICATION")       # timed out twice
    db._exec("UPDATE runs SET created_at=? WHERE id IN (?,?,?)", (_ago(300), lost["id"], stuck["id"], dead["id"]))
    db._exec("UPDATE runs SET status='RUNNING', started_at=?, attempts=1 WHERE id=?", (_ago(700), stuck["id"]))
    db._exec("UPDATE runs SET status='RUNNING', started_at=?, attempts=2 WHERE id=?", (_ago(700), dead["id"]))

    stats = d.sweep()
    assert stats == {"resent": 1, "retried": 1, "failed": 1}
    keys = {k for _, _, k in fake.sent}
    assert keys == {f"{lost['id']}-0", f"{stuck['id']}-1"}
    assert db.get_run(stuck["id"])["status"] == "QUEUED"
    assert db.get_run(dead["id"])["status"] == "FAILED" and "took longer" in db.get_run(dead["id"])["error"]
    assert db.get_run(fresh["id"])["status"] == "QUEUED"
    # a second sweep re-sends with the same keys: deduplicated, nothing new
    d.sweep()
    assert {k for _, _, k in fake.sent} == keys
    # the retried run can now be processed; its attempt counter keeps counting
    assert jobs.process_message({"run_id": stuck["id"]}, db, s) == "processed"
    assert db.get_run(stuck["id"])["status"] == "DONE" and db.get_run(stuck["id"])["attempts"] == 2


def test_abandoned_run_can_be_reclaimed_by_redelivery(tmp_path):
    s = make_settings(ai_provider="mock", db_path=tmp_path / "r.db", files_dir=tmp_path / "f", mock_delay=0.0,
                      make_pdf=False, queue="vercel", file_store="db", run_timeout=600, max_attempts=2)
    db = make_db(s)
    u = db.create_user("r@example.com", "R", "x", "ADMIN", [])
    run = db.create_run(u["id"], db.create_candidate(u["id"], STRONG)["id"], db.create_job(u["id"], JOB)["id"],
                        True, "APPLICATION")
    db._exec("UPDATE runs SET status='RUNNING', started_at=?, attempts=1 WHERE id=?", (_ago(60), run["id"]))
    assert jobs.process_message({"run_id": run["id"]}, db, s) == "not-claimed"      # still within its time
    db._exec("UPDATE runs SET started_at=? WHERE id=?", (_ago(900), run["id"]))
    assert jobs.process_message({"run_id": run["id"]}, db, s) == "processed"
    db._exec("UPDATE runs SET status='QUEUED', attempts=2 WHERE id=?", (run["id"],))
    assert jobs.process_message({"run_id": run["id"]}, db, s) == "failed"             # 3rd attempt > max


def test_cron_endpoint_requires_secret(vapp):
    c = Client(vapp["base"], csrf=False)
    assert c.call("GET", "/api/cron/sweep")[0] == 401
    c.bearer = "wrong"
    assert c.call("GET", "/api/cron/sweep")[0] == 401
    c.bearer = "cron-secret-0123456789"
    code, stats = c.call("GET", "/api/cron/sweep")
    assert code == 200 and set(stats) == {"resent", "retried", "failed"}


def test_login_throttle_is_shared_through_the_database(vapp):
    c = Client(vapp["base"])
    for _ in range(8):
        assert c.login("ghost@example.com", "Wrong-passw0rd")[0] == 401
    assert c.login("ghost@example.com", "Wrong-passw0rd")[0] == 429
    db = vapp["app"].state.db
    assert db.login_failures("ghost@example.com|127.0.0.1", 0) == 8


def test_postgres_sql_translation():
    assert pg_sql("SELECT * FROM t WHERE a=? AND b=?") == "SELECT * FROM t WHERE a=%s AND b=%s"
    assert pg_sql("INSERT INTO t VALUES (:id,:name)") == "INSERT INTO t VALUES (%(id)s,%(name)s)"
    assert pg_sql("SELECT json_extract(runs.render, '$.drive.link') AS l") == \
        "SELECT (runs.render::jsonb #>> '{drive,link}') AS l"
    assert pg_sql("SELECT '50%' WHERE x::text=?") == "SELECT '50%%' WHERE x::text=%s"


def test_vercel_config_files_agree():
    """pyproject (used by Vercel) and requirements.txt (Docker/CI) must list the same packages; the
    subscriber module and the web entry point named in pyproject must exist."""
    import json
    import re
    import tomllib
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    reqs = [ln.strip() for ln in (ROOT / "requirements.txt").read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]
    assert sorted(pyproject["project"]["dependencies"]) == sorted(reqs)
    vercel = pyproject["tool"]["vercel"]
    module, attr = vercel["entrypoint"].split(":")
    assert (ROOT / f"{module}.py").exists() and attr == "app"
    for sub in vercel["subscribers"]:
        assert (ROOT / f"{sub['entrypoint'].replace('.', '/')}.py").exists()
    cfg = json.loads((ROOT / "vercel.json").read_text())
    assert f"{module}.py" in cfg["functions"]
    for cron in cfg["crons"]:
        assert cron["path"] == "/api/cron/sweep"
        minute, hour, *rest = cron["schedule"].split()
        assert re.fullmatch(r"\d+", minute) and re.fullmatch(r"\d+", hour), "Hobby plans allow daily crons only"
    assert "Dockerfile" in (ROOT / ".vercelignore").read_text().split()
