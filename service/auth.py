"""Passwords, permissions and access rules.

Roles
- ADMIN: everything.
- MEMBER: sees only candidates assigned to them (and the runs/applications of those candidates).
  Always allowed: view assigned candidates, mark jobs as applied, update application status.
  Grantable permissions (PERMISSIONS below) unlock the rest.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

PERMISSIONS = {
    "resumes.tailor": "Tailor & download resumes",
    "prompts.manage": "Manage prompts (draft & test; only the administrator publishes)",
}


# ---------- passwords (PBKDF2-SHA256, standard library) ----------
def hash_password(password: str, iterations: int) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    if len(password) < 10:
        return "Password must be at least 10 characters."
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        return "Use upper- and lower-case letters and at least one number."
    return None


# ---------- login throttling (per email+IP, stored in the database so every instance sees it) ----------
class LoginThrottle:
    def __init__(self, db, max_attempts: int = 8, window_seconds: int = 900):
        self.db, self.max, self.window = db, max_attempts, window_seconds

    def blocked(self, key: str) -> bool:
        return self.db.login_failures(key, time.time() - self.window) >= self.max

    def fail(self, key: str) -> None:
        self.db.add_login_failure(key)

    def reset(self, key: str) -> None:
        self.db.clear_login_failures(key)


# ---------- access ----------
class Actor:
    """The signed-in user plus their access rules."""

    def __init__(self, user: dict, db):
        self.user, self.db = user, db
        self.id, self.role = user["id"], user["role"]
        self.is_admin = self.role == "ADMIN"
        self._visible: set[str] | None = None

    def can(self, permission: str) -> bool:
        return self.is_admin or permission in (self.user.get("permissions") or [])

    @property
    def visible_candidate_ids(self) -> set[str] | None:
        """None means 'all' (administrator)."""
        if self.is_admin:
            return None
        if self._visible is None:
            self._visible = self.db.assigned_candidate_ids(self.id)
        return self._visible

    def sees_candidate(self, cid: str) -> bool:
        ids = self.visible_candidate_ids
        return ids is None or cid in ids
