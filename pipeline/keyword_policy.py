"""Policy gate for the keyword retrieval branch.

The historical code calls this branch "bm25", but it is PostgreSQL full-text
search ranked with ts_rank_cd. Broad terms can match thousands of chunks and
dominate latency, so this module decides when the branch is worth running.
When the backend is ParadeDB/pg_search, the true BM25 top-k index makes the
branch bounded enough that auto mode can run keyword retrieval more often.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from pipeline.aliases import KNOWN_SKILLS, normalize_skill
from pipeline.spec import CanonicalSearchSpec

KeywordAction = Literal["run", "skip", "defer"]

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")

_COMMON_TERMS = {
    "account", "accounts", "active", "admin", "administrator", "application",
    "applications", "background", "business", "candidate", "candidates",
    "client", "clients", "communication", "company", "customer", "data",
    "database", "department", "developer", "development", "digital",
    "engineer", "engineering", "experience", "experienced", "finance",
    "financial", "front", "good", "great", "handling", "hotel", "human",
    "information", "it", "leader", "leadership", "manager", "marketing",
    "operations", "people", "profile", "project", "projects", "quality",
    "resource", "resources", "role", "roles", "sales", "service",
    "services", "staff", "strong", "supervision", "support", "system",
    "systems", "team", "technology", "user", "web", "work",
}

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "for", "find", "has",
    "have", "in", "is", "me", "of", "or", "show", "the", "to", "with",
    "who",
}

_SHORT_SPECIFIC_TERMS = {
    "ai", "bi", "ce", "crm", "cto", "cfo", "etl", "erp", "gcp", "hr",
    "llm", "ml", "nlp", "qa", "s3", "sdr", "seo", "sre", "sql", "ui", "ux",
}

_DOMAIN_SPECIFIC_TERMS = {
    "apex", "api", "apis", "backend", "booking", "devops", "front-desk",
    "frontend", "full-stack", "hyperledger", "infrastructure", "j2ee",
    "postgres", "postgresql", "salesforce", "snowflake", "solidity",
    "spring", "terraform", "visualforce",
}


@dataclass(frozen=True)
class KeywordPolicyDecision:
    action: KeywordAction
    reason: str
    policy: str
    timeout_ms: int
    candidate_count: int | None = None
    terms: tuple[str, ...] = ()
    specific_terms: tuple[str, ...] = ()
    broad_terms: tuple[str, ...] = ()
    filter_strength: str = "broad"
    tail_risk: str = "low"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "policy": self.policy,
            "timeout_ms": self.timeout_ms,
            "candidate_count": self.candidate_count,
            "terms": list(self.terms),
            "specific_terms": list(self.specific_terms),
            "broad_terms": list(self.broad_terms),
            "filter_strength": self.filter_strength,
            "tail_risk": self.tail_risk,
        }


def decide_keyword_policy(
    spec: CanonicalSearchSpec,
    cfg: dict[str, Any],
    *,
    candidate_count: int | None = None,
) -> KeywordPolicyDecision:
    """Return whether keyword retrieval should run for this search.

    `candidate_count=None` means the count query has not returned yet. In that
    case the policy can immediately run/skip obvious cases, or return "defer"
    when it needs the actual filtered pool size.
    """
    policy = str(cfg.get("keyword_policy") or "auto").lower()
    backend = str(cfg.get("bm25_backend") or "fts").lower()
    timeout_ms = _coerce_timeout(cfg.get("keyword_timeout_ms", 800))
    terms = tuple(_keyword_terms(spec))
    specific_terms = tuple(t for t in terms if _is_specific_term(t))
    broad_terms = tuple(t for t in terms if t not in specific_terms)
    strength = _filter_strength(spec)
    tail_risk = _tail_risk(backend, strength, specific_terms, candidate_count, cfg)

    if not terms:
        return KeywordPolicyDecision(
            "skip", "no_keyword_terms", policy, timeout_ms,
            candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    if policy == "skip":
        return KeywordPolicyDecision(
            "skip", "caller_requested_skip", policy, timeout_ms,
            candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    if policy == "force":
        return KeywordPolicyDecision(
            "run", "caller_requested_force", policy, timeout_ms,
            candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    if backend == "paradedb":
        return KeywordPolicyDecision(
            "run", "paradedb_bounded_bm25", policy, timeout_ms,
            candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    if strength == "narrow":
        return KeywordPolicyDecision(
            "run", "narrow_hard_filters", policy, timeout_ms,
            candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    if specific_terms and strength == "medium":
        return KeywordPolicyDecision(
            "run", "specific_terms_with_medium_filters", policy, timeout_ms,
            candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    if candidate_count is None:
        return KeywordPolicyDecision(
            "defer", "needs_filtered_candidate_count", policy, timeout_ms,
            None, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    narrow_count = int(cfg.get("keyword_narrow_count") or 1000)
    tail_risk = _tail_risk(backend, strength, specific_terms, candidate_count, cfg)

    if candidate_count <= narrow_count:
        return KeywordPolicyDecision(
            "run", "small_filtered_pool", policy, timeout_ms,
            candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    if tail_risk == "high":
        return KeywordPolicyDecision(
            "skip", "high_tail_risk_fts", policy, timeout_ms,
            candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    if specific_terms:
        return KeywordPolicyDecision(
            "run", "specific_terms", policy, timeout_ms,
            candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
        )

    return KeywordPolicyDecision(
        "skip", "broad_pool_common_terms", policy, timeout_ms,
        candidate_count, terms, specific_terms, broad_terms, strength, tail_risk,
    )


def _tail_risk(
    backend: str,
    strength: str,
    specific_terms: tuple[str, ...],
    candidate_count: int | None,
    cfg: dict[str, Any],
) -> str:
    if backend == "paradedb" or strength == "narrow":
        return "low"
    if strength == "medium" or specific_terms:
        return "medium"
    if candidate_count is None:
        return "medium"
    broad_count = int(cfg.get("keyword_broad_count") or 3000)
    if candidate_count >= broad_count:
        return "high"
    return "medium"


def _keyword_terms(spec: CanonicalSearchSpec) -> list[str]:
    raw_terms = spec.lexical_terms or _TOKEN_RE.findall(spec.semantic_query.lower())
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_terms:
        term = raw.strip().lower()
        if term in _STOPWORDS or len(term) < 2:
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9+#.\-]*", term):
            continue
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out[:8]


def _is_specific_term(term: str) -> bool:
    if term in _COMMON_TERMS:
        return False
    if term in _SHORT_SPECIFIC_TERMS:
        return True
    if term in _DOMAIN_SPECIFIC_TERMS:
        return True
    if normalize_skill(term) in KNOWN_SKILLS:
        return True
    if re.fullmatch(r"benchmarkcase\d+", term):
        return False
    if any(ch in term for ch in "+#"):
        return True
    if any(ch.isdigit() for ch in term) and len(term) <= 8:
        return True
    return False


def _filter_strength(spec: CanonicalSearchSpec) -> str:
    must = spec.must
    hard_skill_count = len(must.skills or [])
    has_range = any(
        value is not None
        for value in (
            must.min_years_exp, must.max_years_exp,
            must.min_salary, must.max_salary,
        )
    )

    if must.city or hard_skill_count >= 2 or (hard_skill_count >= 1 and has_range):
        return "narrow"
    if must.applied_role or has_range or (hard_skill_count >= 1 and must.country):
        return "medium"
    if hard_skill_count >= 1:
        return "medium"
    return "broad"


def _coerce_timeout(value: Any) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = 800
    return max(100, min(timeout, 5000))
