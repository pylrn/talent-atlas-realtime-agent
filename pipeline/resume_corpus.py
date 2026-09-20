"""Transform higher-quality public resume corpora into this app's import shape.

The two supported sources are:
  - ahmedheakl/resume-atlas: Category/Text rows from cleaned real resumes.
  - InferencePrince555/Resume-Dataset: instruction/Resume_test rows.

Both return the same TransformedRecruitmentRow shape used by the existing
recruitment dataset importer so DB loading can stay shared.
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.aliases import clean_skill_list, normalize_skill
from pipeline.recruitment_dataset import TransformedRecruitmentRow


EMAIL_DOMAIN = "resume-corpus.local"

ROLE_ALIASES = {
    "dotnet developer": ".NET Developer",
    "devops": "DevOps Engineer",
    "information-technology": "IT Specialist",
}

CITY_COUNTRY = {
    "pune": ("Pune", "India"),
    "mumbai": ("Mumbai", "India"),
    "hyderabad": ("Hyderabad", "India"),
    "bangalore": ("Bangalore", "India"),
    "bengaluru": ("Bangalore", "India"),
    "delhi": ("Delhi", "India"),
    "chennai": ("Chennai", "India"),
    "new york": ("New York", "USA"),
    "san francisco": ("San Francisco", "USA"),
    "seattle": ("Seattle", "USA"),
    "austin": ("Austin", "USA"),
    "boston": ("Boston", "USA"),
    "london": ("London", "UK"),
    "berlin": ("Berlin", "Germany"),
    "toronto": ("Toronto", "Canada"),
}

ROLE_CITY_DEFAULTS = {
    "software": ("San Francisco", "USA"),
    "developer": ("Bangalore", "India"),
    "engineer": ("Seattle", "USA"),
    "data": ("Bangalore", "India"),
    "accountant": ("New York", "USA"),
    "finance": ("New York", "USA"),
    "hr": ("London", "UK"),
}

KNOWN_SKILLS = [
    "machine learning",
    "deep learning",
    "natural language processing",
    "computer vision",
    "financial reporting",
    "cost accounting",
    "human resources",
    "project management",
    "data analysis",
    "risk management",
    "market research",
    "social media",
    "3d modeling",
    "autocad",
    "catia",
    "ms office",
    "microsoft office",
    "power bi",
    "quickbooks",
    "oracle",
    "jira",
    "bootstrap",
    "redux",
    "jquery",
    "matlab",
    "sap",
    "tableau",
    "photoshop",
    "spring boot",
    "rest api",
    "node.js",
    "javascript",
    "typescript",
    "postgresql",
    "kubernetes",
    "tensorflow",
    "scikit-learn",
    "react native",
    "react",
    "django",
    "fastapi",
    "flask",
    "python",
    "java",
    "c++",
    "c#",
    "sql",
    "mysql",
    "mongodb",
    "redis",
    "docker",
    "aws",
    "gcp",
    "azure",
    "git",
    "linux",
    "pytorch",
    "node",
    "angular",
    "vue",
    "html",
    "css",
    "graphql",
    "terraform",
    "accounting",
    "auditing",
    "finance",
    "gaap",
    "sox",
    "cpa",
    "payroll",
    "budgeting",
    "forecasting",
    "recruiting",
    "marketing",
    "sales",
    "communication",
]


def transform_resume_atlas_row(
    row: dict[str, Any],
    row_number: int,
) -> TransformedRecruitmentRow | None:
    """Transform one ahmedheakl/resume-atlas row."""
    role = _canonical_role(row.get("Category"))
    text = _clean_text(row.get("Text"))
    if not role or len(text) < 200:
        return None
    return _build_transformed_row(
        source_prefix="atlas",
        source_dataset="ahmedheakl/resume-atlas",
        row_number=row_number,
        role=role,
        resume_text=text,
        text_is_lowercased=True,
    )


def transform_resume_inference_row(
    row: dict[str, Any],
    row_number: int,
) -> TransformedRecruitmentRow | None:
    """Transform one InferencePrince555/Resume-Dataset row."""
    resume_text = _clean_text(row.get("Resume_test"))
    if len(resume_text) < 200:
        return None
    role = _role_from_instruction(row.get("instruction")) or _title_case_role(
        resume_text.split(" Professional Summary", 1)[0]
    )
    role = _canonical_role(role)
    if not role:
        return None
    resume_text = _strip_template_leakage(resume_text)
    return _build_transformed_row(
        source_prefix="inference",
        source_dataset="InferencePrince555/Resume-Dataset",
        row_number=row_number,
        role=role,
        resume_text=resume_text,
        text_is_lowercased=False,
    )


def _build_transformed_row(
    *,
    source_prefix: str,
    source_dataset: str,
    row_number: int,
    role: str,
    resume_text: str,
    text_is_lowercased: bool,
) -> TransformedRecruitmentRow:
    skills = _extract_skills(resume_text)
    years_exp = _estimate_years_exp(resume_text)
    city, country, location_synthetic = _extract_location(resume_text, role)
    salary_min, salary_max = _salary_for(role, years_exp, country)
    candidate_name = f"{role} Candidate {row_number}"
    email = f"{source_prefix}-{row_number}@{EMAIL_DOMAIN}"
    age = _age_for(years_exp)
    raw_text = _build_resume_document(
        candidate_name=candidate_name,
        role=role,
        source_dataset=source_prefix_to_label(source_prefix),
        skills=skills,
        years_exp=years_exp,
        resume_text=resume_text,
    )

    candidate = {
        "full_name": candidate_name,
        "email": email,
        "age": age,
        "location": f"{city}, {country}" if city and country else None,
        "city": city,
        "country": country,
        "interests": [],
        "skills": skills,
        "years_exp": years_exp,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "status": "active",
    }
    metadata = {
        "source": f"public-resume-corpus:{source_dataset}",
        "source_dataset": source_dataset,
        "source_id": f"{source_prefix}:{row_number}",
        "job_role": role,
        "text_is_lowercased": text_is_lowercased,
        "location_synthetic": location_synthetic,
        "salary_synthetic": True,
    }
    document = {
        "doc_type": "resume",
        "title": f"Resume - {role}",
        "raw_text": raw_text,
        "metadata": metadata,
    }
    eval_case = {
        "source": f"public-resume-corpus:{source_dataset}",
        "row_number": row_number,
        "query": _default_eval_query(role, skills),
        "expected_candidate_email": email,
        "expected_candidate_name": candidate_name,
        "job_role": role,
        "is_best_match": True,
    }
    return TransformedRecruitmentRow(candidate=candidate, document=document, eval_case=eval_case)


def source_prefix_to_label(source_prefix: str) -> str:
    if source_prefix == "atlas":
        return "resume-atlas"
    if source_prefix == "inference":
        return "inference-resume-dataset"
    return source_prefix


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.lower() in {"none", "null", "nan"}:
        return ""
    text = re.sub(r"[\x00-\x09\x0b-\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _canonical_role(value: Any) -> str:
    role = _clean_text(value)
    if not role:
        return ""
    role = role.replace("_", " ").replace("-", " ")
    role = re.sub(r"\s+", " ", role).strip()
    key = role.lower()
    if key in ROLE_ALIASES:
        return ROLE_ALIASES[key]
    return _title_case_role(role)


def _title_case_role(value: str) -> str:
    words = []
    for word in _clean_text(value).split():
        lower = word.lower()
        if lower in {"hr", "qa", "ui", "ux", "dba", "seo", "ml", "ai"}:
            words.append(lower.upper())
        elif lower in {"ios"}:
            words.append("iOS")
        elif lower in {"devops"}:
            words.append("DevOps")
        else:
            words.append(word[:1].upper() + word[1:].lower())
    return " ".join(words)


def _role_from_instruction(value: Any) -> str:
    instruction = _clean_text(value)
    match = re.search(r"generate a resume for an? (.+?) job\b", instruction, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _strip_template_leakage(text: str) -> str:
    text = re.sub(r"\bCompany Name\s+City\s+State\b", "[Company]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCompany Name\b", "[Company]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCity\s*,?\s*STATE\b", "[Location]", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _extract_skills(text: str) -> list[str]:
    lowered = text.lower()
    found: list[tuple[int, str]] = []
    for skill in KNOWN_SKILLS:
        pattern = r"(?<![a-z0-9+#.])" + re.escape(skill.lower()) + r"(?![a-z0-9+#])"
        match = re.search(pattern, lowered)
        if match:
            found.append((match.start(), normalize_skill(skill)))

    if not found:
        block = _extract_skills_block(text)
        for raw in re.split(r",|;|\||/|\band\b", block, flags=re.IGNORECASE):
            skill = normalize_skill(raw.strip(" .:-"))
            if skill:
                found.append((len(found), skill))

    ordered = [skill for _, skill in sorted(found, key=lambda item: item[0])]
    return clean_skill_list(ordered)[:16]


def _extract_skills_block(text: str) -> str:
    match = re.search(
        r"\b(?:skills|technical skills|technologies)\b\s*[:\-]?\s*(.{0,500})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    block = match.group(1)
    return re.split(
        r"\b(?:experience|education|work history|professional experience|projects)\b",
        block,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]


def _estimate_years_exp(text: str) -> int:
    lowered = text.lower()
    explicit_matches = [
        int(match.group(1))
        for match in re.finditer(
            r"(\d{1,2})\+?\s+years?\b(?:\W+\w+){0,8}?\W+experience",
            lowered,
        )
    ]
    if explicit_matches:
        return min(40, max(explicit_matches))

    spans: list[int] = []
    for match in re.finditer(
        r"\b((?:19|20)\d{2})\b\s*(?:-|to|–|—)\s*\b((?:19|20)\d{2}|present|current)\b",
        lowered,
    ):
        start = int(match.group(1))
        end_text = match.group(2)
        end = 2026 if end_text in {"present", "current"} else int(end_text)
        if 1970 <= start <= end <= 2026:
            spans.append(end - start)
    if spans:
        return min(40, max(spans))

    if "senior" in lowered or "lead " in lowered:
        return 8
    if "mid level" in lowered or "intermediate" in lowered:
        return 4
    if "entry level" in lowered or "fresher" in lowered or "junior" in lowered:
        return 1
    return 0


def _extract_location(text: str, role: str) -> tuple[str | None, str | None, bool]:
    lowered = text.lower()
    for needle, location in CITY_COUNTRY.items():
        if re.search(r"(?<![a-z])" + re.escape(needle) + r"(?![a-z])", lowered):
            return location[0], location[1], False

    for token, location in ROLE_CITY_DEFAULTS.items():
        if token in role.lower():
            return location[0], location[1], True
    return "New York", "USA", True


def _salary_for(role: str, years_exp: int, country: str | None) -> tuple[int, int]:
    role_lower = role.lower()
    if any(token in role_lower for token in ["software", "developer", "engineer", "data", "devops"]):
        base = 70_000
    elif any(token in role_lower for token in ["finance", "accountant", "manager"]):
        base = 60_000
    else:
        base = 50_000

    country_factor = {
        "USA": 1.25,
        "UK": 1.05,
        "Germany": 1.0,
        "Canada": 0.95,
        "India": 0.45,
    }.get(country or "", 1.0)
    seniority = min(years_exp, 15) * 4_000
    salary_min = int((base + seniority) * country_factor)
    salary_min = max(30_000, min(220_000, round(salary_min / 1000) * 1000))
    salary_max = max(salary_min + 15_000, int(salary_min * 1.35))
    salary_max = min(280_000, round(salary_max / 1000) * 1000)
    return salary_min, salary_max


def _age_for(years_exp: int) -> int:
    return min(65, max(21, 24 + years_exp))


def _build_resume_document(
    *,
    candidate_name: str,
    role: str,
    source_dataset: str,
    skills: list[str],
    years_exp: int,
    resume_text: str,
) -> str:
    parts = [
        f"Structured Resume - {candidate_name}",
        f"Applied Role: {role}",
        f"Source Dataset: {source_dataset}",
        f"Estimated Years of Experience: {years_exp}",
    ]
    if skills:
        parts.append("Skills: " + ", ".join(skills))
    parts.append("Resume Text:\n" + resume_text)
    return "\n\n".join(parts)


def _default_eval_query(role: str, skills: list[str]) -> str:
    if skills:
        return f"{role} with {' '.join(skills[:5])}"
    return role
