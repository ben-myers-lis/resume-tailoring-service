"""Fast pipeline (v2): the checks, the targeted fix, the result format, and Prompt Studio wiring.

Most tests use a scripted stand-in for the AI so each safety rule is exercised deliberately:
the model "writes" something unsupported and the test checks what the pipeline does with it.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from service import prompts as pr  # noqa: E402
from service.ai.mock import MockAI  # noqa: E402
from service.fast import FastContractError, NotApplicable, build_input, run_fast  # noqa: E402
from service.package import build_request  # noqa: E402

FILES = pr.package_files()
JD = {"id": "job1", "role": "Senior Data Engineer", "company": "Globex",
      "text": "We need a Senior Data Engineer with Python, SQL and Airflow. Experience with Kubernetes is a plus."}


def candidate(notes=("", ""), education=("BSc Computer Science, State University",)):
    return {"id": "c1", "name": "Jane Doe", "email": "jane@example.com", "linkedin": None, "headline": None,
            "profile_version": 1, "certifications": [], "sources": [], "education": list(education),
            "employment": [
                {"title": "Data Engineer", "company": "Acme", "start_date": "2020-01", "end_date": None,
                 "location": None, "notes": notes[0]},
                {"title": "Data Analyst", "company": "Initech", "start_date": "2016-06", "end_date": "2019-12",
                 "location": None, "notes": notes[1]}]}


def request_for(cand, mode="APPLICATION"):
    return build_request(run_id="run1", candidate=cand, job=JD, mode=mode, as_of=date(2026, 11, 1))


class Scripted:
    """Returns prepared outputs per step and records what each step was given."""

    def __init__(self, **steps):
        self.steps = {k.upper(): list(v) if isinstance(v, list) else [v] for k, v in steps.items()}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, *, instructions, input_data, output_schema):
        stage = next(ln.split(":", 1)[1].strip() for ln in instructions.splitlines() if ln.startswith("STAGE:"))
        self.calls.append((stage, input_data))
        queue = self.steps[stage]
        out = queue.pop(0) if len(queue) > 1 else queue[0]
        return out(input_data) if callable(out) else out

    def stages(self):
        return [s for s, _ in self.calls]

    def usage_report(self):
        return {"calls": [], "total": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                                       "seconds": 0, "cost_usd": 0}}


def blk(text, sources, highlights=()):
    return {"text": text, "sources": list(sources), "highlights": list(highlights)}


def draft(summary=None, skills=(), r1=(), r2=(), scope1=None):
    return {"summary": summary or blk("Data Engineer with 10+ years of experience across 2 roles.", ["R1", "R2"]),
            "skills": [{"items": list(skills)}] if skills else [],
            "roles": [{"role_id": "R1", "scope": scope1 or blk("", []), "bullets": list(r1)},
                      {"role_id": "R2", "scope": blk("", []), "bullets": list(r2)}],
            "gaps": ["Airflow"], "questions": ["Which orchestration tools have you used?"]}


NO_PROBLEMS = {"problems": []}


# ---------- with the mock AI (as in the app's default setup) ----------
def test_title_only_profile_with_mock():
    gen = MockAI()
    res = run_fast(request_for(candidate()), gen, FILES)
    assert res["pipeline"] == "fast" and res["delivery_status"] == "INCOMPLETE_EVIDENCE"
    assert [c["stage"] for c in gen.calls] == ["FAST_DRAFT", "FAST_CHECK"]          # no fix needed
    d = res["draft"]
    assert all(not r["bullets"] for r in d["roles"]) and d["skills"] == []
    assert "Data Engineer with 10+ years" in d["summary"]["text"]
    assert "Jane Doe" in res["resume_markdown"] and "Initech" in res["resume_markdown"]
    assert d["confirmation_items"] and set(res) >= {"draft", "assessment", "report", "resume_markdown"}


def test_statements_become_bullets_for_their_own_role():
    cand = candidate(notes=("Built Airflow pipelines in Python for 40 data sources.", ""))
    res = run_fast(request_for(cand), MockAI(), FILES)
    assert res["delivery_status"] == "READY"
    r1, r2 = res["draft"]["roles"]
    assert len(r1["bullets"]) == 1 and "40 data sources" in r1["bullets"][0]["text"] and not r2["bullets"]
    skills = [i["text"] for g in res["draft"]["skills"] for i in g["items"]]
    assert "Python" in skills and all(s.lower() in cand["employment"][0]["notes"].lower() for s in skills)


def test_input_never_contains_name_or_contacts():
    inp = build_input(request_for(candidate(notes=("Wrote SQL reports.", ""))))
    flat = repr(inp)
    assert "Jane Doe" not in flat and "jane@example.com" not in flat
    assert inp["statements"] == [{"source_id": "S1", "role_id": "R1", "text": "Wrote SQL reports."}]
    assert inp["total_years"] >= 10


# ---------- code checks ----------
def test_unsupported_skill_is_removed_without_an_extra_ai_call():
    gen = Scripted(fast_draft=draft(skills=[{"text": "Kubernetes", "sources": ["R1"]},
                                            {"text": "Data Engineering", "sources": ["R1"]}]),
                   fast_check=NO_PROBLEMS)
    res = run_fast(request_for(candidate()), gen, FILES)
    assert gen.stages() == ["FAST_DRAFT", "FAST_CHECK"]
    assert [i["text"] for g in res["draft"]["skills"] for i in g["items"]] == ["Data Engineering"]
    f = next(f for f in res["assessment"]["findings"] if "Kubernetes" in f["issue"])
    assert f["resolved"] and f["resolution"] == "Removed" and "Kubernetes" not in res["resume_markdown"]


def test_number_not_in_sources_is_sent_to_fix_and_rewritten():
    cand = candidate(notes=("Built Airflow pipelines in Python.", ""))
    gen = Scripted(fast_draft=draft(r1=[blk("Built Airflow pipelines that cut costs by 40%.", ["S1"])]),
                   fast_check=NO_PROBLEMS,
                   fast_fix={"fixes": [{"item_id": "R1.b1", "text": "Built Airflow pipelines in Python.",
                                        "sources": ["S1"], "highlights": ["Airflow"]}]})
    res = run_fast(request_for(cand), gen, FILES)
    assert gen.stages() == ["FAST_DRAFT", "FAST_CHECK", "FAST_FIX"]
    flagged = gen.calls[2][1]["flagged"]
    assert [x["item_id"] for x in flagged] == ["R1.b1"] and "40" in flagged[0]["problems"][0]["issue"]
    assert res["draft"]["roles"][0]["bullets"][0]["text"] == "Built Airflow pipelines in Python."
    assert res["delivery_status"] == "READY"
    assert any(f["resolution"] == "Rewritten" for f in res["assessment"]["findings"])


def test_evidence_from_another_role_is_removed_if_the_fix_cant_support_it():
    cand = candidate(notes=("", "Wrote SQL reports for finance."))
    gen = Scripted(fast_draft=draft(r1=[blk("Wrote SQL reports for finance.", ["S1"])]),   # S1 is R2's
                   fast_check=NO_PROBLEMS,
                   fast_fix={"fixes": [{"item_id": "R1.b1", "text": "", "sources": [], "highlights": []}]})
    res = run_fast(request_for(cand), gen, FILES)
    assert res["draft"]["roles"][0]["bullets"] == []
    assert any("another role" in f["issue"] and f["resolved"] for f in res["assessment"]["findings"])


def test_bullet_without_a_statement_is_removed_and_other_employer_names_are_caught():
    cand = candidate(notes=("Built dashboards.", ""))
    gen = Scripted(fast_draft=draft(r1=[blk("Led a platform migration.", ["R1"]),
                                        blk("Built dashboards later reused at Initech.", ["S1"])]),
                   fast_check=NO_PROBLEMS,
                   fast_fix={"fixes": [{"item_id": "R1.b2", "text": "Built dashboards.", "sources": ["S1"],
                                        "highlights": []}]})
    res = run_fast(request_for(cand), gen, FILES)
    assert [b["text"] for b in res["draft"]["roles"][0]["bullets"]] == ["Built dashboards."]
    issues = " | ".join(f["issue"] for f in res["assessment"]["findings"])
    assert "without any candidate statement" in issues and "Initech" in issues


def test_without_fix_rounds_flagged_items_are_removed():
    cand = candidate(notes=("Built Airflow pipelines.", ""))
    gen = Scripted(fast_draft=draft(r1=[blk("Built Airflow pipelines for 300 teams.", ["S1"])]),
                   fast_check=NO_PROBLEMS)
    res = run_fast(request_for(cand), gen, FILES, max_fix_rounds=0)
    assert "FAST_FIX" not in gen.stages() and res["draft"]["roles"][0]["bullets"] == []


# ---------- the quick AI check ----------
def test_ai_check_problem_on_summary_is_fixed():
    gen = Scripted(fast_draft=draft(), fast_check={"problems": [
        {"item_id": "summary", "severity": "MAJOR", "issue": "Claims leadership", "fix": "Remove 'led'"}]},
        fast_fix={"fixes": [{"item_id": "summary", "text": "Data Engineer with 10+ years of experience.",
                             "sources": ["R1", "R2"], "highlights": []}]})
    res = run_fast(request_for(candidate()), gen, FILES)
    assert res["draft"]["summary"]["text"] == "Data Engineer with 10+ years of experience."
    assert res["delivery_status"] == "INCOMPLETE_EVIDENCE"            # title-only: an outline, not "needs review"
    assert all(f["resolved"] for f in res["assessment"]["findings"])


def test_unfixed_blocker_withholds_the_resume():
    gen = Scripted(fast_draft=draft(), fast_check={"problems": [
        {"item_id": "summary", "severity": "BLOCKER", "issue": "Invents a PhD", "fix": "Remove it"}]},
        fast_fix={"fixes": []})
    res = run_fast(request_for(candidate()), gen, FILES)
    assert res["resume_markdown"] is None and res["delivery_status"] == "REVIEW_NEEDED"


def test_item_ids_stay_put_when_an_earlier_bullet_is_removed():
    """Regression: removing bullet 1 must not make a later step act on what used to be bullet 2."""
    cand = candidate(notes=("Built Airflow pipelines. Wrote SQL reports.", ""))
    gen = Scripted(
        fast_draft=draft(r1=[blk("Built Airflow pipelines for 900 teams.", ["S1"]),     # code: bad number
                             blk("Wrote SQL reports and led the data team.", ["S1"])]),  # AI: overstated
        fast_check={"problems": [{"item_id": "R1.b2", "severity": "MAJOR", "issue": "Claims leadership",
                                  "fix": "Drop 'led the data team'"}]},
        fast_fix={"fixes": [{"item_id": "R1.b1", "text": "", "sources": [], "highlights": []},
                            {"item_id": "R1.b2", "text": "Wrote SQL reports.", "sources": ["S1"], "highlights": []}]})
    res = run_fast(request_for(cand), gen, FILES)
    assert [b["text"] for b in res["draft"]["roles"][0]["bullets"]] == ["Wrote SQL reports."]


def test_invalid_highlights_are_dropped():
    cand = candidate(notes=("Built Airflow pipelines in Python.", ""))
    gen = Scripted(fast_draft=draft(r1=[blk("Built Airflow pipelines in Python.", ["S1"],
                                            ["Kafka", "built", "Airflow"])]),
                   fast_check=NO_PROBLEMS)
    res = run_fast(request_for(cand), gen, FILES)
    assert [h["phrase"] for h in res["draft"]["roles"][0]["bullets"][0]["highlights"]] == ["Airflow"]
    assert "**Airflow**" in res["resume_markdown"]


# ---------- contract ----------
def test_wrong_roles_are_retried_once_then_fail():
    bad = {**draft(), "roles": draft()["roles"][::-1]}
    gen = Scripted(fast_draft=[bad, draft()], fast_check=NO_PROBLEMS)
    run_fast(request_for(candidate()), gen, FILES)
    assert gen.stages()[:2] == ["FAST_DRAFT", "FAST_DRAFT"] and "fix_these_problems" in gen.calls[1][1]
    with pytest.raises(FastContractError):
        run_fast(request_for(candidate()), Scripted(fast_draft=bad, fast_check=NO_PROBLEMS), FILES)


def test_schema_violation_fails_the_run():
    with pytest.raises(FastContractError):
        run_fast(request_for(candidate()), Scripted(fast_draft={"summary": "oops"}), FILES)


def test_illustrative_mode_uses_the_full_pipeline():
    with pytest.raises(NotApplicable):
        run_fast(request_for(candidate(), mode="ILLUSTRATIVE"), MockAI(), FILES)


# ---------- prompt versions ----------
def test_prompt_version_files_and_checks():
    files = pr.package_files()
    assert pr.pipeline_of(files) == "full" and pr.structural_problems(files) == []
    assert pr.pipeline_of({**files, pr.PIPELINE_FILE: "fast\n"}) == "fast"
    assert pr.structural_problems({**files, pr.PIPELINE_FILE: "turbo"})
    assert pr.structural_problems({**files, "fast/10_draft.txt": "STAGE: DRAFT\nhello"})
    changed = pr.protected_changes(files, {**files, "fast/11_check.txt": files["fast/11_check.txt"] + "\nBe lenient."})
    assert [c["id"] for c in changed] == ["fast/11_check.txt"]
    old = {k: v for k, v in files.items() if not k.startswith(("fast/", "settings/"))}
    assert pr.with_defaults(old) == files and pr.pipeline_of(old) == "full"


def test_new_install_starts_on_v2_with_v1_restorable(tmp_path):
    from support import make_db, make_settings
    from service.pipeline import ensure_prompt_versions
    db = make_db(make_settings(db_path=tmp_path / "new.db", files_dir=tmp_path / "f"))
    ensure_prompt_versions(db)
    ensure_prompt_versions(db)                                   # a second start changes nothing
    versions = {v["number"]: v for v in db.list_prompt_versions()}
    assert {n: v["status"] for n, v in versions.items()} == {1: "ARCHIVED", 2: "ACTIVE"}
    assert pr.pipeline_of(db.active_prompt_version()["files"]) == "fast"
    assert pr.pipeline_of(db.get_prompt_version(versions[1]["id"])["files"]) == "full"


def test_existing_install_gets_v2_as_a_draft_only_once(tmp_path):
    """An installation that already made resumes keeps its active version until the admin publishes."""
    from support import make_db, make_settings
    from service.pipeline import ensure_prompt_versions
    from test_service import JOB, STRONG
    db = make_db(make_settings(db_path=tmp_path / "old.db", files_dir=tmp_path / "f"))
    old = {k: v for k, v in pr.package_files().items() if not k.startswith(("fast/", "settings/"))}
    v1 = db.create_prompt_version(old, "ACTIVE", None, None, "from an earlier release")
    u = db.create_user("o@example.com", "O", "x", "ADMIN", [])
    db.create_run(u["id"], db.create_candidate(u["id"], STRONG)["id"], db.create_job(u["id"], JOB)["id"],
                  True, "APPLICATION")
    ensure_prompt_versions(db)
    assert set(db.get_prompt_version(v1["id"])["files"]) == set(pr.EDITABLE)
    assert db.active_prompt_version()["id"] == v1["id"]
    drafts = [v for v in db.list_prompt_versions() if v["status"] == "DRAFT"]
    assert len(drafts) == 1 and pr.pipeline_of(db.get_prompt_version(drafts[0]["id"])["files"]) == "fast"
    db.delete_prompt_version(drafts[0]["id"])
    ensure_prompt_versions(db)                                   # a deleted v2 draft stays deleted
    assert not [v for v in db.list_prompt_versions() if v["status"] == "DRAFT"]


# ---------- through the app: live runs on v2, v1 still comparable ----------
def test_live_runs_use_v2_and_v1_can_be_compared_through_the_app(tmp_path):
    import threading
    import time

    import uvicorn

    from service.api import create_app
    from support import make_settings
    from test_prompts import wait_round
    from test_service import JOB, PW, STRONG, TITLE_ONLY, Client, free_port

    settings = make_settings(ai_provider="mock", db_path=tmp_path / "app.db", files_dir=tmp_path / "files",
                             min_fit=40, workers=3, mock_delay=0.01, make_pdf=True, pdf_engine="reportlab",
                             timezone="America/New_York", password_iterations=1000)
    app = create_app(settings)
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    try:
        a = Client(f"http://127.0.0.1:{port}")
        a.call("POST", "/api/auth/setup", {"name": "Admin", "email": "admin@example.com", "password": PW})
        strong = a.call("POST", "/api/candidates", STRONG)[1]["id"]
        title_only = a.call("POST", "/api/candidates", TITLE_ONLY)[1]["id"]
        job = a.call("POST", "/api/jobs", JOB)[1]["id"]
        for cid in (strong, title_only):
            assert a.call("POST", "/api/prompts/test-cases", {"candidate_id": cid, "job_id": job})[0] == 201

        # A new installation runs live resumes on the fast v2.
        _, home = a.call("GET", "/api/prompts")
        active = next(v for v in home["versions"] if v["status"] == "ACTIVE")
        v1 = next(v for v in home["versions"] if v["number"] == 1)
        assert active["pipeline"] == "fast" and v1["pipeline"] == "full" and v1["status"] == "ARCHIVED"
        _, run = a.call("POST", f"/api/candidates/{strong}/tailor", {"jd_text": JOB["text"], "title": JOB["role"]})
        done = a.wait(run["run"]["id"])
        assert done["status"] == "DONE" and done["prompt_version_id"] == active["id"]
        stored = app.state.db.get_run(done["id"])["result"]
        assert stored["pipeline"] == "fast" and "grade" not in stored            # live runs aren't graded
        code, pdf = a.call("GET", f"/api/runs/{done['id']}/resume.pdf", raw=True)
        assert code == 200 and pdf.startswith(b"%PDF")

        # A draft from v1 (full pipeline) can still be compared with the active v2, same grader for both.
        _, draft = a.call("POST", "/api/prompts/versions", {"based_on": v1["id"]})
        _, full = a.call("GET", f"/api/prompts/versions/{draft['id']}")
        assert full["pipeline"] == "full"
        _, rnd = a.call("POST", f"/api/prompts/versions/{draft['id']}/tests", {"compare": True})
        rnd = wait_round(a, rnd["id"])
        assert rnd["state"] == "PASSED"
        for case in rnd["cases"]:
            assert case["draft"]["pipeline"] == "full" and case["compare"]["pipeline"] == "fast"
            for side in (case["draft"], case["compare"]):
                assert side["grade"]["score"] >= 1 and side["grade"]["unsupported_claims"] == []
        assert rnd["summary"]["draft"]["runs"] == 2 and rnd["summary"]["compare"]["graded"] == 2
    finally:
        srv.should_exit = True
