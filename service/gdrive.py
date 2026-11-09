"""Google Drive storage for tailored resumes (standard library only).

- OAuth 2.0 "web server" flow with your Google account. Scopes: `drive.file` (the app can only see and
  manage files it created; nothing else in your Drive) plus `openid email` to show which account is connected.
- The refresh token is stored in the app database; client ID/secret come from the .env file.
- Folder layout: "Resume Tailoring" / <candidate name> / <file>.
- Sharing: "anyone with the link can view" (default) or "team only" (each assigned team member's
  email gets view access; they need a Google account with that email).
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
SCOPES = "https://www.googleapis.com/auth/drive.file openid email"
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT_FOLDER_NAME = "Resume Tailoring"

# transport(method, url, headers, body) -> (status, body_bytes). Swappable for tests.
Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class DriveError(Exception):
    pass


@dataclass
class DriveConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


class Drive:
    """One connected Google account. `store` persists tokens/ids: get(key) / set(key, value)."""

    def __init__(self, config: DriveConfig, store, transport: Transport = urllib_transport):
        self.cfg, self.store, self.http = config, store, transport
        self._access: tuple[str, float] | None = None

    # ---------- OAuth ----------
    def auth_url(self) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        q = {"client_id": self.cfg.client_id, "redirect_uri": self.cfg.redirect_uri, "response_type": "code",
             "scope": SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
             "state": state}
        return f"{AUTH_URL}?{urllib.parse.urlencode(q)}", state

    def exchange_code(self, code: str) -> dict:
        tok = self._form(TOKEN_URL, {"code": code, "client_id": self.cfg.client_id,
                                     "client_secret": self.cfg.client_secret,
                                     "redirect_uri": self.cfg.redirect_uri, "grant_type": "authorization_code"})
        if not tok.get("refresh_token"):
            raise DriveError("Google didn't return a refresh token. Remove the app's access in your Google "
                             "account settings and connect again.")
        self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60)
        info = self._json("GET", USERINFO_URL)
        self.store.set("refresh_token", tok["refresh_token"])
        self.store.set("account_email", info.get("email"))
        self.store.set("root_folder_id", None)
        return info

    def disconnect(self) -> None:
        token = self.store.get("refresh_token")
        if token:
            self.http("POST", f"{REVOKE_URL}?{urllib.parse.urlencode({'token': token})}",
                      {"Content-Type": "application/x-www-form-urlencoded"}, b"")
        for k in ("refresh_token", "account_email", "root_folder_id"):
            self.store.set(k, None)
        self._access = None

    @property
    def connected(self) -> bool:
        return bool(self.store.get("refresh_token"))

    def access_token(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        rt = self.store.get("refresh_token")
        if not rt:
            raise DriveError("Google Drive isn't connected")
        tok = self._form(TOKEN_URL, {"refresh_token": rt, "client_id": self.cfg.client_id,
                                     "client_secret": self.cfg.client_secret, "grant_type": "refresh_token"})
        self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60)
        return self._access[0]

    # ---------- files ----------
    def root_folder(self) -> str:
        fid = self.store.get("root_folder_id")
        if fid and self._exists(fid):
            return fid
        fid = self.create_folder(ROOT_FOLDER_NAME, None)
        self.store.set("root_folder_id", fid)
        return fid

    def create_folder(self, name: str, parent: str | None) -> str:
        meta = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            meta["parents"] = [parent]
        return self._json("POST", f"{FILES_URL}?fields=id", meta)["id"]

    def folder_link(self, fid: str) -> str:
        return f"https://drive.google.com/drive/folders/{fid}"

    def upload(self, data: bytes, name: str, mime: str, parent: str) -> dict:
        boundary = "rts" + secrets.token_hex(12)
        meta = json.dumps({"name": name, "parents": [parent]}).encode()
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta +
                f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode() + data + f"\r\n--{boundary}--\r\n".encode())
        url = f"{UPLOAD_URL}?uploadType=multipart&fields=id,name,webViewLink"
        return self._request("POST", url, body, f"multipart/related; boundary={boundary}")

    def share_anyone(self, fid: str) -> None:
        self._json("POST", f"{FILES_URL}/{fid}/permissions", {"type": "anyone", "role": "reader"})

    def share_user(self, fid: str, email: str) -> None:
        self._json("POST", f"{FILES_URL}/{fid}/permissions?sendNotificationEmail=false",
                   {"type": "user", "role": "reader", "emailAddress": email})

    def trash(self, fid: str) -> None:
        self._json("PATCH", f"{FILES_URL}/{fid}", {"trashed": True})

    def _exists(self, fid: str) -> bool:
        status, body = self.http("GET", f"{FILES_URL}/{fid}?fields=id,trashed",
                                 {"Authorization": f"Bearer {self.access_token()}"}, None)
        return status == 200 and not json.loads(body or b"{}").get("trashed")

    # ---------- http helpers ----------
    def _form(self, url: str, data: dict) -> dict:
        status, body = self.http("POST", url, {"Content-Type": "application/x-www-form-urlencoded"},
                                 urllib.parse.urlencode(data).encode())
        payload = json.loads(body or b"{}")
        if status != 200:
            raise DriveError(f"Google sign-in failed: {payload.get('error_description') or payload.get('error') or status}")
        return payload

    def _json(self, method: str, url: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        return self._request(method, url, body, "application/json; charset=UTF-8" if body is not None else None)

    def _request(self, method: str, url: str, body: bytes | None, content_type: str | None) -> dict:
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        if content_type:
            headers["Content-Type"] = content_type
        status, raw = self.http(method, url, headers, body)
        if status == 401:                                   # expired token: refresh once and retry
            self._access = None
            headers["Authorization"] = f"Bearer {self.access_token()}"
            status, raw = self.http(method, url, headers, body)
        data = json.loads(raw or b"{}")
        if status >= 300:
            msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
            raise DriveError(f"Google Drive error ({status}): {msg or 'unknown'}")
        return data
