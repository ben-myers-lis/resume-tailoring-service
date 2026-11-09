"""Input validation for the API."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

YM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class Role(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    start_date: str = Field(description="YYYY-MM")
    end_date: Optional[str] = Field(None, description="YYYY-MM, or null for a current role")
    location: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=5000,
                                 description="Optional: what the candidate did in this role, in their words")

    @field_validator("start_date", "end_date")
    @classmethod
    def _ym(cls, v):
        if v is not None and not YM.match(v):
            raise ValueError("use YYYY-MM, e.g. 2021-03")
        return v

    @model_validator(mode="after")
    def _order(self):
        today = date.today().strftime("%Y-%m")
        if self.start_date > today:
            raise ValueError("start_date is in the future")
        if self.end_date and (self.end_date < self.start_date or self.end_date > today):
            raise ValueError("end_date must be between start_date and today")
        return self


class Source(BaseModel):
    kind: Literal["CANDIDATE_STATEMENT", "CANDIDATE_DOCUMENT", "UNCONFIRMED"] = "CANDIDATE_STATEMENT"
    content: str = Field(min_length=1, max_length=20000)


class CandidateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: Optional[str] = Field(None, max_length=200)
    linkedin: Optional[str] = Field(None, max_length=300)
    headline: Optional[str] = Field(None, max_length=200)
    employment: list[Role] = Field(min_length=1, max_length=30, description="Most recent first")
    education: list[str] = Field(default_factory=list, max_length=10)
    certifications: list[str] = Field(default_factory=list, max_length=20)
    sources: list[Source] = Field(default_factory=list, max_length=20)


class JobIn(BaseModel):
    company: Optional[str] = Field(None, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    url: Optional[str] = Field(None, max_length=1000, description="Link to the job posting")
    text: str = Field(min_length=20, max_length=30000, description="Full job description")


class RunIn(BaseModel):
    candidate_id: str
    job_id: str
    force: bool = Field(False, description="Tailor even if the fit score is below the threshold")
    mode: Literal["APPLICATION", "ILLUSTRATIVE"] = "APPLICATION"


class BulkRunIn(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=500)
    job_ids: list[str] = Field(min_length=1, max_length=500)
    force: bool = False
    mode: Literal["APPLICATION", "ILLUSTRATIVE"] = "APPLICATION"

    @model_validator(mode="after")
    def _size(self):
        if len(self.candidate_ids) * len(self.job_ids) > 2000:
            raise ValueError("at most 2,000 runs per bulk request")
        return self


# ---------- accounts and team ----------
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PermissionName = Literal["resumes.tailor", "prompts.manage"]


def _email(v: str) -> str:
    v = v.strip()
    if not EMAIL.match(v):
        raise ValueError("enter a valid email address")
    return v


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


class SetupIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)
    _e = field_validator("email")(lambda cls, v: _email(v))


class InviteIn(BaseModel):
    email: str = Field(max_length=200)
    permissions: list[PermissionName] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list, max_length=1000)
    _e = field_validator("email")(lambda cls, v: _email(v))


class AcceptInviteIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(max_length=200)


class MemberUpdateIn(BaseModel):
    permissions: Optional[list[PermissionName]] = None
    active: Optional[bool] = None
    candidate_ids: Optional[list[str]] = Field(None, max_length=1000)


class AssigneesIn(BaseModel):
    user_ids: list[str] = Field(default_factory=list, max_length=200)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


# ---------- applications ----------
AppStatus = Literal["APPLIED", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"]
YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApplicationIn(BaseModel):
    candidate_id: str
    run_id: Optional[str] = Field(None, description="Tailoring run whose resume was submitted")
    job_id: Optional[str] = None
    company: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=200)
    job_url: Optional[str] = Field(None, max_length=1000)
    applied_on: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to today")
    status: AppStatus = "APPLIED"
    notes: Optional[str] = Field(None, max_length=2000)

    @field_validator("applied_on")
    @classmethod
    def _d(cls, v):
        if v is not None:
            if not YMD.match(v):
                raise ValueError("use YYYY-MM-DD")
            date.fromisoformat(v)
        return v


class ApplicationUpdateIn(BaseModel):
    status: Optional[AppStatus] = None
    notes: Optional[str] = Field(None, max_length=2000)
    job_url: Optional[str] = Field(None, max_length=1000)


# ---------- prompt studio ----------
class DraftIn(BaseModel):
    based_on: Optional[str] = Field(None, description="Version to copy; defaults to the active version")
    note: Optional[str] = Field(None, max_length=500)


class FilesUpdateIn(BaseModel):
    files: dict[str, str] = Field(description="Changed files only: {path: full new text}")
    note: Optional[str] = Field(None, max_length=500)


class ProposalIn(BaseModel):
    instruction: str = Field(min_length=5, max_length=6000)
    use_test_resumes: bool = True


class EditDecision(BaseModel):
    index: int = Field(ge=0)
    accept: bool
    replace: Optional[str] = Field(None, max_length=50000, description="Optional: your adjusted replacement text")


class ApplyProposalIn(BaseModel):
    decisions: list[EditDecision]


class TestCaseIn(BaseModel):
    candidate_id: str
    job_id: str
    note: Optional[str] = Field(None, max_length=300)


class TestRoundIn(BaseModel):
    case_ids: Optional[list[str]] = Field(None, max_length=50, description="Defaults to every saved test case")
    compare: bool = Field(True, description="Also run the active version for side-by-side comparison")


class PublishIn(BaseModel):
    confirm_protected: bool = False
    note: Optional[str] = Field(None, max_length=500)


class TailorIn(BaseModel):
    """Paste a job description (plain text) and tailor this candidate's resume for it."""
    jd_text: str = Field(min_length=40, max_length=30000, description="The job description, pasted as plain text")
    title: Optional[str] = Field(None, max_length=200, description="Optional; detected from the text if blank")
    company: Optional[str] = Field(None, max_length=200, description="Optional; detected from the text if blank")
    url: Optional[str] = Field(None, max_length=1000)
    force: bool = False


class JobUpdateIn(BaseModel):
    role: Optional[str] = Field(None, min_length=1, max_length=200)
    company: Optional[str] = Field(None, max_length=200)
    url: Optional[str] = Field(None, max_length=1000)


class DriveOptionsIn(BaseModel):
    auto_upload: Optional[bool] = None
    share_mode: Optional[Literal["anyone", "team"]] = None
    formats: Optional[list[Literal["pdf", "docx"]]] = Field(None, min_length=1, max_length=2)
    trash_on_delete: Optional[bool] = None
