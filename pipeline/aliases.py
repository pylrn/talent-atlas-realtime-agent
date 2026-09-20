"""Deterministic skill and location normalisation.
Applied AFTER the LLM planner so results are consistent regardless of
which synonym the LLM chose."""

from __future__ import annotations

import re

SKILL_ALIASES: dict[str, str] = {
    # JavaScript ecosystem
    "js": "javascript",
    "javascript": "javascript",
    "ecmascript": "javascript",
    "es6": "javascript",
    "reactjs": "react",
    "react.js": "react",
    "react-native": "react-native",
    "nodejs": "node",
    "node.js": "node",
    "nextjs": "nextjs",
    "next.js": "nextjs",
    "vuejs": "vue",
    "vue.js": "vue",
    "angularjs": "angular",
    "typescript": "typescript",
    "ts": "typescript",
    "reduxjs": "redux",
    "react native": "react-native",
    # Python ecosystem
    "py": "python",
    "python3": "python",
    "django-rest": "django",
    "fastapi": "fastapi",
    "flask": "flask",
    # ML / AI
    "ml": "machine-learning",
    "machine learning": "machine-learning",
    "machinelearning": "machine-learning",
    "dl": "deep-learning",
    "deep learning": "deep-learning",
    "deeplearning": "deep-learning",
    "nlp": "nlp",
    "natural language processing": "nlp",
    "cv": "computer-vision",
    "computer vision": "computer-vision",
    "pytorch": "pytorch",
    "tensorflow": "tensorflow",
    "tf": "tensorflow",
    "sklearn": "scikit-learn",
    "scikit learn": "scikit-learn",
    "scikit-learn": "scikit-learn",
    "llm": "llm",
    "large language model": "llm",
    "genai": "generative-ai",
    "generative ai": "generative-ai",
    # DevOps / Cloud
    "k8s": "kubernetes",
    "kube": "kubernetes",
    "kubernetes": "kubernetes",
    "docker": "docker",
    "aws": "aws",
    "amazon web services": "aws",
    "gcp": "gcp",
    "google cloud": "gcp",
    "google cloud platform": "gcp",
    "azure": "azure",
    "microsoft azure": "azure",
    "ci/cd": "ci-cd",
    "cicd": "ci-cd",
    "terraform": "terraform",
    # Databases
    "postgres": "postgresql",
    "psql": "postgresql",
    "mysql": "mysql",
    "mongo": "mongodb",
    "elasticsearch": "elasticsearch",
    "elastic": "elasticsearch",
    "redis": "redis",
    # Mobile
    "ios": "ios",
    "android": "android",
    "swift": "swift",
    "kotlin": "kotlin",
    "flutter": "flutter",
    # General
    "restful": "rest-api",
    "rest": "rest-api",
    "graphql": "graphql",
    "sql": "sql",
    "nosql": "nosql",
    "c#": "csharp",
    "csharp": "csharp",
    "c++": "cpp",
    "cpp": "cpp",
    "golang": "go",
    "ruby on rails": "rails",
    "ror": "rails",
    "springboot": "spring-boot",
    "spring boot": "spring-boot",
    "project management": "project-management",
    "financial reporting": "financial-reporting",
    "cost accounting": "cost-accounting",
    "human resources": "human-resources",
    "data analysis": "data-analysis",
    "risk management": "risk-management",
    "market research": "market-research",
    "social media": "social-media",
    "3d modeling": "3d-modeling",
    "3d modelling": "3d-modeling",
    "auto cad": "autocad",
    "autocad": "autocad",
    "catia v5": "catia",
    "catia": "catia",
    "pro-e": "proe",
    "pro e": "proe",
    "ms office": "ms-office",
    "microsoft office": "ms-office",
    "power bi": "power-bi",
    "powerbi": "power-bi",
    "quick books": "quickbooks",
    "quickbooks": "quickbooks",
}

LOCATION_ALIASES: dict[str, str] = {
    # India
    "bengaluru": "bangalore",
    "bombay": "mumbai",
    "madras": "chennai",
    "calcutta": "kolkata",
    # UK
    "britain": "uk",
    "great britain": "uk",
    "england": "uk",
    "united kingdom": "uk",
    # US
    "united states": "us",
    "united states of america": "us",
    "usa": "us",
    "america": "us",
    "the states": "us",
    "bay area": "san francisco",
    "nyc": "new york",
    "new york city": "new york",
    "la": "los angeles",
    # Others
    "uae": "dubai",
    "sg": "singapore",
    "aus": "australia",
}


def normalize_skill(raw: str) -> str:
    """Lowercase, strip, apply alias."""
    cleaned = raw.strip().lower()
    cleaned = re.sub(r"\s+", "-", cleaned)       # "machine learning" → "machine-learning"
    return SKILL_ALIASES.get(cleaned, cleaned)


DISPLAY_SKILL_ALLOWLIST = {
    normalize_skill(skill)
    for skill in [
        "3d modeling", "accounting", "auditing", "autocad", "aws", "azure",
        "biology", "blueprint reading", "bootstrap", "branding", "budgeting",
        "case management", "catia", "chemistry", "client relations",
        "communication", "compliance", "computer vision", "conflict resolution",
        "content strategy", "contract law", "cost accounting", "counseling",
        "creativity", "critical thinking", "crm software", "css",
        "customer service", "c#", "c++", "data analysis", "data science",
        "data visualization", "database management", "deep learning",
        "diagnosis", "django", "docker", "excel", "fastapi", "finance",
        "financial reporting", "flask", "forecasting", "gaap", "gcp", "gis",
        "git", "graphql", "healthcare", "html", "human resources",
        "java", "javascript", "jira", "jquery", "kotlin", "kubernetes",
        "leadership", "legal research", "linux", "logistics",
        "machine learning", "market research", "marketing", "materials science",
        "matlab", "medical terminology", "mongodb", "motivation", "ms office",
        "mysql", "natural language processing", "negotiation", "networking",
        "node", "nutrition", "oracle", "patient care", "patient consultation",
        "payroll", "pharmaceuticals", "pharmacology", "photoshop",
        "postgresql", "power bi", "problem solving", "product development",
        "programming", "project management", "prototyping", "python",
        "pytorch", "quickbooks", "react", "react native", "recruiting",
        "redis", "redux", "regulatory compliance", "requirements gathering",
        "research", "research methodology", "rest api", "risk assessment",
        "risk management", "sales", "sap", "scikit-learn",
        "scientific writing", "seo", "social media", "sox", "spring boot",
        "sql", "statistics", "strength training", "surgical skills",
        "sustainability", "swift", "tableau", "team leadership",
        "team management", "teamwork", "tensorflow", "terraform", "testing",
        "time management", "troubleshooting", "typescript", "vue",
        "web design", "writing",
    ]
}


# Extra compact skills that are useful for deterministic query parsing even if
# they are not exposed in UI pickers.
EXTRA_KNOWN_SKILLS = {
    "python", "javascript", "react", "node", "java", "go", "rust",
    "sql", "postgresql", "mysql", "mongodb", "redis",
    "aws", "gcp", "azure", "kubernetes", "docker",
    "machine-learning", "deep-learning", "nlp", "tensorflow", "pytorch",
    "typescript", "angular", "vue", "django", "flask", "fastapi",
    "swift", "kotlin", "flutter", "ios", "android",
    "graphql", "rest-api", "spring-boot", "rails",
    "c", "cpp", "csharp", "scala", "ruby", "php", "r",
    "figma", "sketch", "css", "html", "sass",
    "data-science", "analytics", "tableau", "power-bi",
    "devops", "ci-cd", "terraform", "ansible",
}
KNOWN_SKILLS = DISPLAY_SKILL_ALLOWLIST | EXTRA_KNOWN_SKILLS


def is_clean_skill_option(raw: str) -> bool:
    """Return True for compact skill labels safe to expose in UI pickers."""
    return normalize_skill(raw) in DISPLAY_SKILL_ALLOWLIST


def clean_skill_list(skills: list[str]) -> list[str]:
    """Normalize, de-duplicate, and drop resume fragments masquerading as skills."""
    seen: set[str] = set()
    out: list[str] = []
    for skill in skills:
        normalized = normalize_skill(skill)
        if normalized and normalized not in seen and is_clean_skill_option(normalized):
            seen.add(normalized)
            out.append(normalized)
    return out


def normalize_location(raw: str) -> str:
    """Lowercase, strip, un-hyphenate, collapse whitespace, then alias.

    Cities/countries are stored in the DB with spaces (e.g. "New York", "USA").
    The LLM sometimes outputs hyphenated slugs (e.g. "new-york") because the
    skill rule in the planner prompt bleeds over. We normalise to the DB form
    here so SQL filters and feature-ranker location matches both line up.
    """
    cleaned = raw.strip().lower()
    cleaned = cleaned.replace("-", " ").replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return LOCATION_ALIASES.get(cleaned, cleaned)


def normalize_skill_list(skills: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in skills:
        n = normalize_skill(s)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out
