"""Generate title-only test requests (fictional candidates and companies).

    python test_cases/make_cases.py      # writes test_cases/case_*.json and validates each
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.runner import validate_request  # noqa: E402

AS_OF = "2026-11-08"
OUT = Path(__file__).resolve().parent

JDS = {
    "senior_mle": ("Lumen Health AI", "Senior Machine Learning Engineer",
        "We are hiring a Senior Machine Learning Engineer to build and deploy production ML models for clinical "
        "risk prediction. Required: 5+ years of software or ML engineering experience; Python; PyTorch or "
        "TensorFlow; experience deploying models to production; SQL; cloud experience (AWS or GCP). Preferred: "
        "MLOps tooling (MLflow, Kubeflow), feature stores, healthcare data experience. Responsibilities: own "
        "model training and evaluation pipelines, partner with clinicians and product managers, mentor engineers."),
    "llm_app": ("Brightpath Software", "LLM Application Engineer",
        "Build LLM-powered product features. Required: 3+ years of software engineering; Python or TypeScript; "
        "experience building applications on LLM APIs; retrieval-augmented generation (RAG); prompt design and "
        "evaluation; REST APIs. Preferred: vector databases (pgvector, Pinecone), agent frameworks, "
        "observability for LLM systems. Responsibilities: ship features end to end, design evaluation suites, "
        "work with product and design."),
    "staff_mle": ("Orbital Robotics", "Staff Machine Learning Engineer",
        "Required: 8+ years of ML engineering experience including technical leadership of ML teams; deep "
        "expertise in PyTorch; distributed training on GPU clusters; production model serving at scale. "
        "Preferred: computer vision, robotics, CUDA. Responsibilities: set technical direction for perception "
        "models, lead a team of 6 engineers, drive architecture reviews."),
    "ai_platform": ("Northwind Commerce", "AI Platform Engineer",
        "Build the internal platform our ML and LLM teams use. Required: 5+ years of backend or infrastructure "
        "engineering; Python or Go; Kubernetes; CI/CD; cloud (AWS preferred). Preferred: model serving (vLLM, "
        "Triton, KServe), MLOps, GPU scheduling, Terraform. Responsibilities: deploy and operate model-serving "
        "infrastructure, build developer tooling, ensure reliability and cost efficiency."),
    "mle_mid": ("Cascade Insurance", "Machine Learning Engineer",
        "Required: 3+ years in data science, analytics engineering, or ML; Python; SQL; experience training "
        "and evaluating ML models; experience with scikit-learn or similar. Preferred: experience deploying "
        "models, Airflow, insurance or financial data. Responsibilities: build pricing and claims models, "
        "productionize models with engineering, communicate results to business stakeholders."),
    "applied_scientist": ("Helix Research Labs", "Senior Applied Scientist, NLP",
        "Required: PhD in Computer Science, Machine Learning, or related field; 4+ years of applied research in "
        "NLP or LLMs; publications at top venues (ACL, NeurIPS, ICML); PyTorch; experience fine-tuning large "
        "language models. Responsibilities: lead research on model alignment and evaluation, publish, and "
        "transfer research into products."),
    "ai_eng_lead": ("Meridian Logistics", "Lead AI Engineer",
        "Required: 6+ years of software engineering, including 2+ years building AI or ML systems; experience "
        "leading engineers; Python; cloud deployment; LLM integration. Preferred: agentic workflows, "
        "logistics or supply-chain domain, stakeholder management. Responsibilities: lead a team of 4, own "
        "the AI roadmap for route optimization and operations tooling."),
    "junior_ai": ("Sparrow Labs", "Junior AI Engineer",
        "Required: bachelor's degree in computer science or related field, or equivalent experience; "
        "Python; familiarity with machine learning fundamentals; eagerness to learn LLM application "
        "development. Preferred: internship experience, cloud certifications, Git. Responsibilities: build "
        "and test LLM features under senior guidance, write evaluation scripts, document work."),
}


def role(rid, employer, title, start, end, current=False):
    return {"role_id": rid, "employer": employer, "official_title": title, "start_date": start,
            "end_date": None if current else end, "is_current": current,
            "location": None, "location_type": "UNKNOWN"}


CASES = [
    # (case id, expectation, candidate name, employment, education, certifications, jd key)
    ("01_strong_mle", "Strong match: 7 yrs ML titles vs Senior MLE",
     "Avery Lindqvist",
     [role("R1", "Quantia Analytics", "Senior Machine Learning Engineer", "2022-03", None, True),
      role("R2", "Quantia Analytics", "Machine Learning Engineer", "2020-01", "2022-02"),
      role("R3", "Tidewater Systems", "Software Engineer", "2017-06", "2019-12")],
     ["M.S. Computer Science, State University, 2017"], [], "senior_mle"),
    ("02_strong_llm", "Strong match: SWE then AI Engineer vs LLM App Engineer",
     "Jordan Okafor",
     [role("R1", "Pinecrest Labs", "AI Engineer", "2024-02", None, True),
      role("R2", "Pinecrest Labs", "Software Engineer II", "2021-07", "2024-01"),
      role("R3", "Ridgeway Digital", "Software Engineer", "2019-08", "2021-06")],
     ["B.S. Computer Engineering, Tech Institute, 2019"], [], "llm_app"),
    ("03_stretch_staff", "Stretch: 4 yrs data science vs Staff MLE (8+ yrs, leadership)",
     "Priya Ramanathan",
     [role("R1", "Coastal Retail Group", "Data Scientist", "2022-09", None, True),
      role("R2", "Coastal Retail Group", "Associate Data Scientist", "2021-06", "2022-08")],
     ["M.S. Statistics, Lakeside University, 2021"], [], "staff_mle"),
    ("04_adjacent_platform", "Adjacent: 8 yrs backend/SRE vs AI Platform Engineer",
     "Marcus Feld",
     [role("R1", "Granite Payments", "Senior Site Reliability Engineer", "2022-04", None, True),
      role("R2", "Granite Payments", "Backend Engineer", "2019-01", "2022-03"),
      role("R3", "Bluefin Media", "Junior Backend Developer", "2017-02", "2018-12")],
     ["B.S. Information Systems, Metro College, 2016"], ["AWS Certified Solutions Architect - Associate"],
     "ai_platform"),
    ("05_transferable_analyst", "Transferable stretch: analytics titles vs mid MLE",
     "Sofia Marchetti",
     [role("R1", "Harborview Insurance", "Senior Data Analyst", "2023-01", None, True),
      role("R2", "Harborview Insurance", "Data Analyst", "2020-10", "2022-12")],
     ["B.A. Economics, Riverside University, 2020"], [], "mle_mid"),
    ("06_clear_mismatch", "Clear mismatch: accounting titles vs PhD Applied Scientist",
     "Daniel Whitcombe",
     [role("R1", "Ashford & Lane LLP", "Senior Accountant", "2021-05", None, True),
      role("R2", "Ashford & Lane LLP", "Staff Accountant", "2018-07", "2021-04")],
     ["B.S. Accounting, Central State University, 2018"], ["Certified Public Accountant (CPA)"],
     "applied_scientist"),
    ("07_overlap_gap", "Overlapping roles and a gap: tests conservative tenure counting",
     "Lena Kowalczyk",
     [role("R1", "Freelance", "AI Consultant", "2024-06", None, True),
      role("R2", "Orchard Health", "Engineering Manager, ML", "2022-01", "2024-03"),
      role("R3", "Orchard Health", "Senior Software Engineer", "2019-03", "2021-12"),
      role("R4", "Kite Mobility", "Software Engineer (part-time)", "2018-09", "2019-06")],
     ["B.S. Computer Science, Northern University, 2018"], [], "ai_eng_lead"),
    ("08_junior", "Junior: internship plus 1 yr vs Junior AI Engineer",
     "Ethan Brooks",
     [role("R1", "Maplegrove Software", "Associate Software Engineer", "2025-07", None, True),
      role("R2", "Maplegrove Software", "Software Engineering Intern", "2024-06", "2024-08")],
     ["B.S. Computer Science, Valley State University, 2025"],
     ["Google Cloud Certified - Associate Cloud Engineer"], "junior_ai"),
]


def build(case):
    cid, expectation, name, employment, education, certs, jd_key = case
    company, title, text = JDS[jd_key]
    slug = name.lower().replace(" ", ".")
    return {
        "package_version": "1.0.0",
        "run_id": f"title-only-{cid}",
        "run_type": "NEW_JD",
        "as_of_date": AS_OF,
        "candidate": {
            "candidate_id": f"cand-{cid}",
            "profile_version": "title-only-1",
            "name": name,
            "headline": None,
            "contacts": [{"kind": "EMAIL", "value": f"{slug}@example.com"},
                         {"kind": "LINKEDIN", "value": f"linkedin.com/in/{slug.replace('.', '-')}-example"}],
            "employment": employment,
            "education": education,
            "certifications": certs,
        },
        "sources": [],          # title-only: no work-history statements
        "jd": {"jd_id": f"jd-{jd_key}", "company": company, "role": title, "text": text},
        "external_context": [],
        "options": {
            "mode": "APPLICATION",
            "metric_policy": "EVIDENCED_ONLY",
            "hypothetical_metrics_authorized": False,
            "illustrative_scenario": None,
            "output_mode": "RESUME_AND_REPORT",
            "formats": ["MARKDOWN"],
            "max_repair_rounds": 1,
            "keyword_category_ids": [],
            "bullet_ceilings": [],
            "max_pages": 2,
        },
        "revision": None,
    }, expectation


if __name__ == "__main__":
    index = []
    for case in CASES:
        req, expectation = build(case)
        validate_request(req)
        path = OUT / f"case_{case[0]}.json"
        path.write_text(json.dumps(req, indent=2, ensure_ascii=False))
        index.append({"file": path.name, "candidate": req["candidate"]["name"],
                      "job": f'{req["jd"]["role"]} at {req["jd"]["company"]}', "expectation": expectation})
        print(f"ok  {path.name:34s} {expectation}")
    (OUT / "INDEX.json").write_text(json.dumps(index, indent=2))
