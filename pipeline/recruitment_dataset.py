"""
Transformer for the Kaggle recruitment dataset.

The source dataset is applicant/job-match oriented, not a direct copy of this
project's database schema. These helpers convert each row into:
  - one candidate-like structured record
  - one resume document suitable for ingestion/chunking
  - one evaluation query from the job description and match label
"""

from __future__ import annotations

import re
import random
from dataclasses import dataclass
from typing import Any


DATASET_URL = "https://www.kaggle.com/api/v1/datasets/download/surendra365/recruitement-dataset"
DATASET_FILENAME = "job_applicant_dataset.csv"
FALLBACK_EMAIL_DOMAIN = "kaggle-recruitment.local"
TRANSFORM_VERSION = "recruitment-dataset/v2"

SYNTHETIC_LOCATIONS = {
    "India": ["Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Pune", "Chennai"],
    "USA": ["San Francisco", "New York", "Seattle", "Austin", "Boston", "Chicago"],
    "UK": ["London", "Manchester", "Edinburgh", "Cambridge", "Bristol"],
    "Germany": ["Berlin", "Munich", "Hamburg", "Frankfurt"],
    "Japan": ["Tokyo", "Osaka", "Kyoto"],
    "Canada": ["Toronto", "Vancouver", "Montreal"],
    "Australia": ["Sydney", "Melbourne", "Brisbane"],
    "Singapore": ["Singapore"],
}


@dataclass(frozen=True)
class TransformedRecruitmentRow:
    candidate: dict[str, Any]
    document: dict[str, Any]
    eval_case: dict[str, Any]


def transform_recruitment_row(
    row: dict[str, str],
    row_number: int,
) -> TransformedRecruitmentRow:
    """Convert one Kaggle row into candidate/document/eval records."""
    applicant_name = _clean_text(row.get("Job Applicant Name")) or f"Candidate {row_number}"
    role = _clean_text(row.get("Job Roles"))
    resume = _clean_text(row.get("Resume"))
    job_description = _clean_text(row.get("Job Description"))
    age = _parse_age(row.get("Age"))
    skills = _extract_skills(resume)
    years_exp = _estimate_years_exp(resume)
    city, country = _synthetic_location(row_number)
    salary_min, salary_max = _synthetic_salary(row_number, years_exp)

    display_name = f"{applicant_name} - Application {row_number}"
    email = f"applicant-{row_number}@{FALLBACK_EMAIL_DOMAIN}"
    title_subject = role or applicant_name

    raw_text = _build_resume_document(
        candidate_name=display_name,
        role=role,
        age=age,
        skills=skills,
        years_exp=years_exp,
        resume=resume,
    )

    candidate = {
        "full_name": display_name,
        "email": email,
        "age": age,
        "location": f"{city}, {country}",
        "city": city,
        "country": country,
        "interests": [],
        "skills": skills,
        "years_exp": years_exp,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "status": "active",
    }
    document = {
        "doc_type": "resume",
        "title": f"Resume - {title_subject}",
        "raw_text": raw_text,
        "metadata": {
            "source": "kaggle:surendra365/recruitement-dataset",
            "row_number": row_number,
            "job_role": role,
        },
    }
    eval_case = {
        "source": "kaggle:surendra365/recruitement-dataset",
        "row_number": row_number,
        "query": job_description,
        "expected_candidate_email": email,
        "expected_candidate_name": display_name,
        "job_role": role,
        "is_best_match": _parse_best_match(row.get("Best Match")),
    }
    return TransformedRecruitmentRow(candidate=candidate, document=document, eval_case=eval_case)


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x09\x0b-\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_age(value: Any) -> int | None:
    try:
        age = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if 16 <= age <= 100:
        return age
    return None


def _parse_best_match(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _extract_skills(resume: str) -> list[str]:
    match = re.search(
        r"\bproficient in\s+(.*?)(?:,\s*with\b|\s+with\b|\.|$)",
        resume,
        flags=re.IGNORECASE,
    )
    if not match:
        return []

    skill_text = match.group(1)
    raw_skills = re.split(r",|\band\b", skill_text, flags=re.IGNORECASE)
    skills: list[str] = []
    seen: set[str] = set()
    for raw_skill in raw_skills:
        skill = _normalize_skill(raw_skill)
        if not skill or skill in seen:
            continue
        skills.append(skill)
        seen.add(skill)
        if len(skills) >= 12:
            break
    return skills


def _normalize_skill(value: str) -> str:
    skill = _clean_text(value).lower()
    skill = re.sub(r"^[^\w+#.]+|[^\w+#.]+$", "", skill)
    skill = re.sub(r"\s+", " ", skill)
    if len(skill) < 2 or len(skill) > 60:
        return ""
    return skill


def _estimate_years_exp(resume: str) -> int:
    lower = resume.lower()
    if "senior-level" in lower:
        return 8
    if "mid-level" in lower:
        return 4
    if "entry-level" in lower:
        return 1
    match = re.search(r"(\d+)\+?\s+years?\s+(?:of\s+)?experience", lower)
    if match:
        return min(40, int(match.group(1)))
    return 0


def _synthetic_location(row_number: int) -> tuple[str, str]:
    """Stable synthetic location for rows where the source has no location."""
    rng = random.Random(row_number * 7919)
    country = rng.choice(list(SYNTHETIC_LOCATIONS.keys()))
    city = rng.choice(SYNTHETIC_LOCATIONS[country])
    return city, country


def _synthetic_salary(row_number: int, years_exp: int) -> tuple[int, int]:
    """Stable broad salary range in USD-like annual units."""
    rng = random.Random(row_number * 15485863)
    salary_min = (45 + min(years_exp, 15) * 7 + rng.randint(0, 30)) * 1000
    salary_min = min(160_000, max(45_000, salary_min))
    salary_max = salary_min + rng.randint(20_000, 75_000)
    salary_max = min(260_000, max(salary_min + 10_000, salary_max))
    return salary_min, salary_max


def _build_resume_document(
    candidate_name: str,
    role: str,
    age: int | None,
    skills: list[str],
    years_exp: int,
    resume: str,
) -> str:
    parts = [
        f"Structured Resume - {candidate_name}",
    ]
    if role:
        parts.append(f"Applied Role: {role}")
    if age is not None:
        parts.append(f"Age: {age}")
    parts.append(f"Estimated Years of Experience: {years_exp}")
    if skills:
        parts.append("Skills: " + ", ".join(skills))
    if resume:
        parts.append("Resume Text:\n" + resume)
    return "\n\n".join(parts)
