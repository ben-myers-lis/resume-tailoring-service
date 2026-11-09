"""Admin commands (the web app's first-run setup page does the same as create-admin).

    python manage.py create-admin "Your Name" you@example.com     # prompts for a password
    python manage.py reset-password you@example.com               # prompts for a new password
    python manage.py seed-demo you@example.com                    # 8 fictional candidates + jobs
"""
import getpass
import json
import sys
from pathlib import Path

from service.auth import hash_password, password_problem
from service.config import db_target, get_settings
from service.db import DB

ROOT = Path(__file__).resolve().parent


def ask_password() -> str:
    while True:
        p = getpass.getpass("Password: ")
        if problem := password_problem(p):
            print(problem)
            continue
        if getpass.getpass("Repeat password: ") == p:
            return p
        print("Passwords don't match.")


def create_admin(name: str, email: str):
    s = get_settings()
    db = DB(db_target(s), s.db_schema)
    if db.count_users():
        sys.exit("An account already exists. Sign in, or use reset-password.")
    u = db.create_user(email, name, hash_password(ask_password(), s.password_iterations), "ADMIN", [])
    print(f"Administrator created: {u['name']} <{u['email']}>")


def reset_password(email: str):
    s = get_settings()
    db = DB(db_target(s), s.db_schema)
    u = db.get_user_auth(email) or sys.exit("No account with that email.")
    db.update_user(u["id"], password_hash=hash_password(ask_password(), s.password_iterations))
    print("Password updated; all sessions for this account were signed out.")


def seed_demo(email: str):
    db = DB(db_target(get_settings()), get_settings().db_schema)
    u = db.get_user_auth(email) or sys.exit("No account with that email.")
    jobs = {}
    for f in sorted((ROOT / "prompt_package/test_cases").glob("case_*.json")):
        req = json.loads(f.read_text())
        c, jd = req["candidate"], req["jd"]
        contacts = {x["kind"]: x["value"] for x in c["contacts"]}
        cand = db.create_candidate(u["id"], {
            "name": c["name"], "email": contacts.get("EMAIL"), "linkedin": contacts.get("LINKEDIN"),
            "employment": [{"company": r["employer"], "title": r["official_title"], "start_date": r["start_date"],
                            "end_date": r["end_date"], "location": r["location"]} for r in c["employment"]],
            "education": c["education"], "certifications": c["certifications"]})
        if jd["jd_id"] not in jobs:
            jobs[jd["jd_id"]] = db.create_job(u["id"], {"company": jd["company"], "role": jd["role"], "text": jd["text"]})
        print(f"{cand['name']:20s} -> {jd['role']} at {jd['company']}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["create-admin"] and len(a) == 3:
        create_admin(a[1], a[2])
    elif a[:1] == ["reset-password"] and len(a) == 2:
        reset_password(a[1])
    elif a[:1] == ["seed-demo"] and len(a) == 2:
        seed_demo(a[1])
    else:
        print(__doc__)
