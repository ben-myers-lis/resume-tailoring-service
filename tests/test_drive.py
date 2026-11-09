"""Google Drive integration tests against a simulated Google (no network)."""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_service import PW, STRONG, Client, free_port  # noqa: E402
from service import drive_service, gdrive  # noqa: E402
from service.api import create_app  # noqa: E402
from service.config import Settings  # noqa: E402
from support import ON_PG, make_db, make_settings  # noqa: E402

JD = """Senior Machine Learning Engineer
Lumen Health AI

Required: 5+ years of ML engineering; Python; PyTorch; deploying models to production."""


class FakeGoogle:
    """Just enough of Google OAuth + Drive v3 for the app."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.revoked = False
        self.fail_uploads = False
        self.n = 0

    def _id(self):
        self.n += 1
        return f"f{self.n}"

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        u = urllib.parse.urlparse(url)
        path = f"{u.scheme}://{u.netloc}{u.path}"
        if path == gdrive.TOKEN_URL:
            form = dict(urllib.parse.parse_qsl(body.decode()))
            if form["grant_type"] == "authorization_code":
                if form["code"] != "good-code":
                    return 400, json.dumps({"error": "invalid_grant"}).encode()
                return 200, json.dumps({"access_token": "at1", "expires_in": 3600, "refresh_token": "rt1"}).encode()
            return 200, json.dumps({"access_token": "at2", "expires_in": 3600}).encode()
        if path == gdrive.REVOKE_URL:
            self.revoked = True
            return 200, b"{}"
        assert headers.get("Authorization", "").startswith("Bearer "), url
        if path == gdrive.USERINFO_URL:
            return 200, json.dumps({"email": "owner@gmail.com"}).encode()
        if path == gdrive.UPLOAD_URL:
            if self.fail_uploads:
                return 500, json.dumps({"error": {"message": "backend error"}}).encode()
            boundary = re.search(r"boundary=(\S+)", headers["Content-Type"]).group(1).encode()
            parts = body.split(b"--" + boundary)
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].strip())
            data = parts[2].split(b"\r\n\r\n", 1)[1][:-2]
            fid = self._id()
            self.files[fid] = {"name": meta["name"], "parents": meta["parents"], "data": data, "perms": [], "trashed": False,
                               "mime": parts[2].split(b"\r\n\r\n", 1)[0].decode().split(": ", 1)[1]}
            return 200, json.dumps({"id": fid, "name": meta["name"], "webViewLink": f"https://drive.google.com/file/d/{fid}/view"}).encode()
        if path == gdrive.FILES_URL and method == "POST":
            meta = json.loads(body)
            fid = self._id()
            self.files[fid] = {**meta, "perms": [], "trashed": False}
            return 200, json.dumps({"id": fid}).encode()
        m = re.match(re.escape(gdrive.FILES_URL) + r"/(\w+)(/permissions)?$", path)
        if m:
            fid = m.group(1)
            if fid not in self.files:
                return 404, b'{"error": {"message": "not found"}}'
            if m.group(2):
                self.files[fid]["perms"].append(json.loads(body))
                return 200, b'{"id": "p"}'
            if method == "PATCH":
                self.files[fid].update(json.loads(body))
                if self.files[fid].get("trashed"):
                    for f in self.files.values():
                        if fid in f.get("parents", []):
                            f["trashed"] = True
                return 200, b"{}"
            return 200, json.dumps({"id": fid, "trashed": self.files[fid]["trashed"]}).encode()
        raise AssertionError(f"unexpected call {method} {url}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get_location(client: Client, path: str) -> str:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(client.jar), NoRedirect)
    try:
        opener.open(client.base + path)
    except urllib.error.HTTPError as e:
        assert e.code == 302, e.code
        return e.headers["Location"]
    raise AssertionError("expected a redirect")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rtsd")
    fake = FakeGoogle()
    drive_service.TRANSPORT = fake
    port = free_port()
    settings = make_settings(ai_provider="mock", db_path=tmp / "app.db", files_dir=tmp / "files", min_fit=40, workers=2,
                        mock_delay=0.02, make_pdf=True, password_iterations=1000, timezone="America/New_York",
                        google_client_id="cid.apps.googleusercontent.com", google_client_secret="secret",
                        port=port, public_url=f"http://127.0.0.1:{port}")
    app = create_app(settings)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    admin = Client(base)
    admin.call("POST", "/api/auth/setup", {"name": "Admin", "email": "admin@example.com", "password": PW})
    cid = admin.call("POST", "/api/candidates", STRONG)[1]["id"]
    _, inv = admin.call("POST", "/api/team/invites", {"email": "sam@example.com", "permissions": ["resumes.tailor"],
                                                      "candidate_ids": [cid]})
    member = Client(base)
    member.call("POST", "/api/invites/accept", {"token": inv["token"], "name": "Sam", "password": PW})
    yield {"base": base, "admin": admin, "member": member, "cid": cid, "fake": fake, "settings": settings}
    srv.should_exit = True
    t.join(10)
    drive_service.TRANSPORT = gdrive.urllib_transport


def tailor_and_wait_for_link(c: Client, cid: str, timeout=60) -> dict:
    _, res = c.call("POST", f"/api/candidates/{cid}/tailor", {"jd_text": JD})
    rid = res["run"]["id"]
    end = time.time() + timeout
    while time.time() < end:
        _, r = c.call("GET", f"/api/runs/{rid}")
        if r["status"] == "DONE" and not r["drive"].get("pending"):
            return r
        time.sleep(0.2)
    raise TimeoutError(rid)


def test_connect_flow(env):
    a, m, fake = env["admin"], env["member"], env["fake"]
    _, st = a.call("GET", "/api/settings")
    assert st["drive"]["configured"] and not st["drive"]["connected"]
    assert st["drive"]["redirect_uri"].endswith("/api/google/callback")
    assert m.call("GET", "/api/settings")[0] == 403
    loc = get_location(a, "/api/google/connect")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
    assert loc.startswith(gdrive.AUTH_URL) and "drive.file" in q["scope"] and q["access_type"] == "offline"
    assert get_location(a, f"/api/google/callback?code=good-code&state=WRONG").startswith("/#/settings?drive_error=")
    loc = get_location(a, "/api/google/connect")
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))["state"]
    assert get_location(m, f"/api/google/callback?code=good-code&state={state}").startswith("/#/settings?drive_error=")
    loc = get_location(a, "/api/google/connect")
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))["state"]
    assert get_location(a, f"/api/google/callback?code=good-code&state={state}") == "/#/settings?drive=connected"
    _, st = a.call("GET", "/api/settings")
    assert st["drive"]["connected"] and st["drive"]["account_email"] == "owner@gmail.com"
    root = [f for f in fake.files.values() if f.get("name") == "Resume Tailoring"]
    assert len(root) == 1 and st["drive"]["folder_link"]


def test_connect_from_wrong_address_explains(env):
    a = env["admin"]
    other = Client(env["base"].replace("127.0.0.1", "localhost"))
    other.login("admin@example.com")
    loc = get_location(other, "/api/google/connect")
    assert loc.startswith("/#/settings?drive_error=") and "127.0.0.1" in urllib.parse.unquote(loc)


def test_auto_upload_and_copy_link(env):
    m, fake = env["member"], env["fake"]
    r = tailor_and_wait_for_link(m, env["cid"])
    assert r["drive"]["link"].startswith("https://drive.google.com/file/d/")
    fid = r["drive"]["link"].split("/d/")[1].split("/")[0]
    f = fake.files[fid]
    assert f["name"] == "Avery_Lindqvist_Lumen_Health_AI_Senior_Machine_Learning_Engineer.pdf"
    assert f["data"][:4] == b"%PDF" and f["mime"] == "application/pdf"
    assert {"type": "anyone", "role": "reader"} in f["perms"]
    folder = fake.files[f["parents"][0]]
    assert folder["name"] == "Avery Lindqvist" and fake.files[folder["parents"][0]]["name"] == "Resume Tailoring"
    r2 = tailor_and_wait_for_link(m, env["cid"])                       # same candidate -> same folder
    fid2 = r2["drive"]["link"].split("/d/")[1].split("/")[0]
    assert fake.files[fid2]["parents"] == f["parents"]
    assert "pdf" in r["files"] and "docx" in r["files"]                 # Download still available
    env["run_id"] = r["id"]


def test_team_only_sharing_and_formats(env):
    a, m, fake = env["admin"], env["member"], env["fake"]
    assert a.call("PUT", "/api/settings/drive", {"share_mode": "team", "formats": ["pdf", "docx"]})[0] == 200
    assert m.call("PUT", "/api/settings/drive", {"share_mode": "anyone"})[0] == 403
    r = tailor_and_wait_for_link(m, env["cid"])
    new = [f for f in fake.files.values() if f.get("data") and f["name"].endswith((".pdf", ".docx")) and
           any(p.get("emailAddress") for p in f["perms"])]
    assert {f["name"].rsplit(".", 1)[1] for f in new} == {"pdf", "docx"}
    assert all(p == {"type": "user", "role": "reader", "emailAddress": "sam@example.com"} for f in new for p in f["perms"])
    assert r["drive"]["link"].endswith("/view")
    a.call("PUT", "/api/settings/drive", {"share_mode": "anyone", "formats": ["pdf"]})


def test_upload_failure_then_retry(env):
    m, fake = env["member"], env["fake"]
    fake.fail_uploads = True
    r = tailor_and_wait_for_link(m, env["cid"])
    assert r["status"] == "DONE" and r["resume_markdown"] and "backend error" in r["drive"]["error"]
    code, _ = m.call("POST", f"/api/runs/{r['id']}/drive")
    assert code == 502
    fake.fail_uploads = False
    code, res = m.call("POST", f"/api/runs/{r['id']}/drive")
    assert code == 200 and res["link"]
    _, r = m.call("GET", f"/api/runs/{r['id']}")
    assert r["drive"]["link"] == res["link"] and not r["drive"]["error"]


def test_member_without_tailor_permission_sees_no_link(env):
    a = env["admin"]
    _, inv = a.call("POST", "/api/team/invites", {"email": "view@example.com", "candidate_ids": [env["cid"]]})
    v = Client(env["base"])
    v.call("POST", "/api/invites/accept", {"token": inv["token"], "name": "Viewer", "password": PW})
    _, r = v.call("GET", f"/api/runs/{env['run_id']}")
    assert r["drive"] == {} and r["files"] == {}
    assert v.call("POST", f"/api/runs/{env['run_id']}/drive")[0] == 403


def test_test_runs_not_uploaded(env):
    a, fake = env["admin"], env["fake"]
    before = sum(1 for f in fake.files.values() if f.get("data"))
    _, jobs = a.call("GET", f"/api/jobs?candidate_id={env['cid']}")
    a.call("POST", "/api/prompts/test-cases", {"candidate_id": env["cid"], "job_id": jobs[0]["id"]})
    _, v = a.call("POST", "/api/prompts/versions", {})
    _, rnd = a.call("POST", f"/api/prompts/versions/{v['id']}/tests", {"compare": True})
    end = time.time() + 60
    while time.time() < end and a.call("GET", f"/api/prompts/rounds/{rnd['id']}")[1]["state"] == "RUNNING":
        time.sleep(0.2)
    assert sum(1 for f in fake.files.values() if f.get("data")) == before


def test_delete_candidate_trashes_drive_folder(env):
    a, fake = env["admin"], env["fake"]
    _, c = a.call("POST", "/api/candidates", {**STRONG, "name": "Delete Me"})
    r = tailor_and_wait_for_link(a, c["id"])
    fid = r["drive"]["link"].split("/d/")[1].split("/")[0]
    folder = fake.files[fid]["parents"][0]
    assert a.call("DELETE", f"/api/candidates/{c['id']}")[0] == 200
    assert fake.files[folder]["trashed"] and fake.files[fid]["trashed"]


def test_disconnect(env):
    a, fake = env["admin"], env["fake"]
    code, st = a.call("POST", "/api/settings/drive/disconnect")
    assert code == 200 and not st["connected"] and fake.revoked
    _, res = a.call("POST", f"/api/candidates/{env['cid']}/tailor", {"jd_text": JD})
    r = a.wait(res["run"]["id"])
    assert r["drive"].get("link") is None and not r["drive"].get("pending")
    assert a.call("POST", f"/api/runs/{r['id']}/drive")[0] == 409
