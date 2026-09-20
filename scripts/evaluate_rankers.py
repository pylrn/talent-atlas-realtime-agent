"""
Offline reranking benchmark for the seeded hiring-search dataset.

The benchmark keeps retrieval fixed: each query fetches the same broad RRF
candidate pool from Postgres/pgvector, then several lightweight scoring
formulas rerank that pool. This measures ranking logic without changing the
live API behavior.

Usage:
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python3 scripts/evaluate_rankers.py
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pipeline.database import close_pool, get_pool
from pipeline.embedder import get_embedder
from pipeline.search import HybridSearchEngine, SearchFilters


REPORT_DIR = Path("reports")
JSON_OUT = REPORT_DIR / "reranking_evaluation.json"
HTML_OUT = REPORT_DIR / "reranking_evaluation.html"

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+#-]*")
STOPWORDS = {
    "a", "about", "also", "an", "and", "are", "as", "at", "be", "been", "candidate",
    "candidates", "engineer", "experience", "experienced", "find", "for", "has",
    "have", "having", "in", "is", "me", "need", "of", "on", "or", "person",
    "profile", "senior", "someone", "that", "the", "their", "to", "want", "who",
    "with", "year", "years",
}


@dataclass(frozen=True)
class QuerySpec:
    id: str
    query: str
    expected_name: str
    expected_country: str
    expected_city: str
    reason: str
    country: str | None = None
    city: str | None = None
    skills: list[str] = field(default_factory=list)
    skills_match: str = "or"
    doc_types: list[str] = field(default_factory=list)
    min_years_exp: int | None = None


@dataclass(frozen=True)
class Formula:
    id: str
    name: str
    expression: str
    notes: str
    scorer: Callable[[dict[str, Any], dict[str, Any]], float]


QUERIES: list[QuerySpec] = [
    QuerySpec(
        "q01",
        "Candidate who designed a real-time chat system and is fluent in JavaScript.",
        "Liam Nguyen",
        "India",
        "Pune",
        "Best seed match combines a chat-system transcript with JavaScript skill.",
        country="India",
        doc_types=["transcript"],
    ),
    QuerySpec(
        "q02",
        "Backend engineer with Python, PostgreSQL, pgvector, FastAPI, semantic search, and ML infrastructure.",
        "Codex Test Candidate",
        "India",
        "Bangalore",
        "The custom resume explicitly mentions pgvector, semantic search, FastAPI, and ML infrastructure.",
        country="India",
        doc_types=["resume"],
    ),
    QuerySpec(
        "q03",
        "Python FastAPI developer with Azure and AWS experience for backend work.",
        "Maria O'Brien",
        "India",
        "Delhi",
        "Has Python, FastAPI, Azure, and AWS in the candidate skill profile.",
        country="India",
        skills=["python", "fastapi"],
        skills_match="and",
    ),
    QuerySpec(
        "q04",
        "Senior Hyderabad profile with PostgreSQL, SQL, machine learning, node.js, and Python.",
        "Aditi Wilson",
        "India",
        "Hyderabad",
        "The profile is a dense match across PostgreSQL, SQL, ML, node.js, and Python.",
        country="India",
        min_years_exp=8,
    ),
    QuerySpec(
        "q05",
        "Austin backend candidate using PostgreSQL, SQL, Redis, FastAPI, Docker, and MongoDB.",
        "Sara Ali",
        "USA",
        "Austin",
        "Strong structured skill match with the Austin location.",
        country="USA",
    ),
    QuerySpec(
        "q06",
        "Vancouver infrastructure profile with Docker, Go, GCP, SQL, PostgreSQL, and Elasticsearch.",
        "Fatima Sharma",
        "Canada",
        "Vancouver",
        "Best Canadian match for Docker, Go, GCP, SQL/PostgreSQL, and Elasticsearch.",
        country="Canada",
        min_years_exp=5,
    ),
    QuerySpec(
        "q07",
        "Munich senior platform engineer with SQL, Docker, PostgreSQL, Terraform, and TypeScript.",
        "Amit Patel",
        "Germany",
        "Munich",
        "A 15-year Munich profile matching the platform skill bundle.",
        country="Germany",
        min_years_exp=10,
    ),
    QuerySpec(
        "q08",
        "Edinburgh ML and NLP engineer with FastAPI, node.js, Kubernetes, and deep learning.",
        "Uma Santos",
        "UK",
        "Edinburgh",
        "Best UK match for the ML/NLP plus FastAPI/node.js combination.",
        country="UK",
        skills=["machine-learning", "nlp"],
        skills_match="or",
    ),
    QuerySpec(
        "q09",
        "Singapore data engineering profile with Python, Redis, React, Angular, TypeScript, and Vue.",
        "Omar Kumar",
        "Singapore",
        "Singapore",
        "Matches the broad front-end plus data-engineering/Python/Redis stack.",
        country="Singapore",
    ),
    QuerySpec(
        "q10",
        "Tokyo senior profile with PostgreSQL, computer vision, machine learning, NLP, and node.js.",
        "Raj Ali",
        "Japan",
        "Tokyo",
        "Best Japanese profile covering PostgreSQL, CV, ML/NLP, and node.js.",
        country="Japan",
        min_years_exp=8,
    ),
    QuerySpec(
        "q11",
        "Toronto cloud search engineer with Terraform, Elasticsearch, Go, AWS, and React.",
        "Omar Taylor",
        "Canada",
        "Toronto",
        "The Toronto profile has the exact Terraform/Elasticsearch/Go/AWS/React mix.",
        country="Canada",
        city="Toronto",
    ),
    QuerySpec(
        "q12",
        "Sydney candidate with FastAPI, Docker, MongoDB, Rust, AWS, GCP, and deep learning.",
        "Ananya Li",
        "Australia",
        "Sydney",
        "The Sydney candidate has the full stack named in the query.",
        country="Australia",
        city="Sydney",
    ),
    QuerySpec(
        "q13",
        "Montreal developer with TypeScript, node.js, GCP, FastAPI, MongoDB, Go, and security.",
        "Amit Ivanov",
        "Canada",
        "Montreal",
        "Only Montreal candidate with this exact broad profile.",
        country="Canada",
        city="Montreal",
    ),
    QuerySpec(
        "q14",
        "Hamburg backend profile with Rust, Elasticsearch, and FastAPI.",
        "Maria Ali",
        "Germany",
        "Hamburg",
        "Direct structured match on Rust, Elasticsearch, and FastAPI.",
        country="Germany",
        skills=["rust", "fastapi"],
        skills_match="and",
    ),
    QuerySpec(
        "q15",
        "Kyoto mobile backend engineer with Python, Java, Flutter, MongoDB, Rust, C++, and Go.",
        "Hiroshi Patel",
        "Japan",
        "Kyoto",
        "Best Kyoto match for the broad mobile/backend skill bundle.",
        country="Japan",
        city="Kyoto",
    ),
    QuerySpec(
        "q16",
        "Bristol profile with Redis, SQL, FastAPI, Elasticsearch, and Angular.",
        "Sara Wilson",
        "UK",
        "Bristol",
        "Exact Bristol match on Redis, SQL, FastAPI, Elasticsearch, and Angular.",
        country="UK",
        city="Bristol",
    ),
    QuerySpec(
        "q17",
        "New York engineer with Redis, Azure, Python, and Flutter.",
        "Wei Chen",
        "USA",
        "New York",
        "Direct New York profile match for Redis, Azure, Python, and Flutter.",
        country="USA",
        city="New York",
    ),
    QuerySpec(
        "q18",
        "San Francisco engineer with PostgreSQL, MongoDB, GCP, Go, React, CV, and Elasticsearch.",
        "Amit Cohen",
        "USA",
        "San Francisco",
        "Only San Francisco active profile with this mix.",
        country="USA",
        city="San Francisco",
    ),
    QuerySpec(
        "q19",
        "Singapore JavaScript engineer with Angular, Flutter, and Terraform.",
        "Chen Tanaka",
        "Singapore",
        "Singapore",
        "Structured skill profile is the clearest Singapore match.",
        country="Singapore",
        skills=["javascript", "terraform"],
        skills_match="and",
    ),
    QuerySpec(
        "q20",
        "Pune front-end platform candidate with Angular, Kubernetes, MongoDB, TypeScript, and computer vision.",
        "Raj Santos",
        "India",
        "Pune",
        "The Pune profile matches all named skills except the wording is spread across profile fields.",
        country="India",
        city="Pune",
    ),
    QuerySpec(
        "q21",
        "Mumbai backend security candidate with Rust, node.js, Flask, Python, FastAPI, and security.",
        "Elena Mueller",
        "India",
        "Mumbai",
        "Direct Mumbai profile match for backend/security skills.",
        country="India",
        city="Mumbai",
    ),
    QuerySpec(
        "q22",
        "Seattle senior candidate with Kubernetes, security, Angular, and SQL.",
        "Isha Kumar",
        "USA",
        "Seattle",
        "The Seattle profile is an exact match and has senior experience.",
        country="USA",
        city="Seattle",
        min_years_exp=8,
    ),
    QuerySpec(
        "q23",
        "Osaka mobile React candidate with security, AWS, Flutter, MongoDB, and mobile experience.",
        "Rahul Smith",
        "Japan",
        "Osaka",
        "Best Osaka profile for mobile/security/AWS/Flutter/MongoDB.",
        country="Japan",
        city="Osaka",
    ),
    QuerySpec(
        "q24",
        "Manchester JavaScript DevOps Vue computer vision Flutter candidate.",
        "Xin Kumar",
        "UK",
        "Manchester",
        "The Manchester profile matches every skill term.",
        country="UK",
        city="Manchester",
    ),
    QuerySpec(
        "q25",
        "Berlin cloud mobile candidate with Python, Azure, GCP, Flask, and mobile.",
        "Elena Santos",
        "Germany",
        "Berlin",
        "Clear Berlin match on Python, Azure, GCP, Flask, and mobile.",
        country="Germany",
        city="Berlin",
    ),
    QuerySpec(
        "q26",
        "Toronto ML search profile with Elasticsearch, NLP, machine learning, and data engineering.",
        "David Martinez",
        "Canada",
        "Toronto",
        "The active Toronto profile is the strongest semantic and skill match.",
        country="Canada",
        city="Toronto",
    ),
    QuerySpec(
        "q27",
        "Delhi DevOps cloud developer with GCP, TypeScript, Flutter, Python, and DevOps.",
        "Takeshi Kumar",
        "India",
        "Delhi",
        "The Delhi profile matches all named terms.",
        country="India",
        city="Delhi",
    ),
    QuerySpec(
        "q28",
        "Munich DevOps security candidate with Redis, Kubernetes, TypeScript, Flask, and security.",
        "Isha Li",
        "Germany",
        "Munich",
        "Best Munich match for DevOps, security, Redis, Kubernetes, TypeScript, and Flask.",
        country="Germany",
        city="Munich",
    ),
    QuerySpec(
        "q29",
        "Melbourne backend candidate with Redis, Java, FastAPI, AWS, and PostgreSQL.",
        "Maria Williams",
        "Australia",
        "Melbourne",
        "The Melbourne candidate has every listed skill.",
        country="Australia",
        city="Melbourne",
    ),
    QuerySpec(
        "q30",
        "Singapore candidate for rate limiter design with security, Java, and Terraform.",
        "Kavya Brown",
        "Singapore",
        "Singapore",
        "Combines a rate-limiter transcript with security, Java, and Terraform skills.",
        country="Singapore",
        doc_types=["transcript"],
    ),
]


def normalize_skill(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def tokens(value: Any, *, keep_stopwords: bool = False) -> list[str]:
    found = TOKEN_RE.findall(str(value or "").lower())
    if keep_stopwords:
        return found
    return [token for token in found if token not in STOPWORDS and len(token) >= 2]


def token_set(values: list[Any]) -> set[str]:
    result: set[str] = set()
    for value in values:
        if isinstance(value, list):
            result.update(token_set(value))
            continue
        for token in tokens(value):
            result.add(token)
            if token.endswith("s") and len(token) > 4:
                result.add(token[:-1])
    return result


def coverage(query_terms: set[str], values: list[Any]) -> float:
    if not query_terms:
        return 0.0
    terms = token_set(values)
    return len(query_terms & terms) / len(query_terms)


def phrase_score(query: str, values: list[Any]) -> float:
    query_tokens = tokens(query)
    if len(query_tokens) < 2:
        return 0.0
    text_tokens = tokens(" ".join(str(v or "") for v in values), keep_stopwords=False)
    text_bigrams = set(zip(text_tokens, text_tokens[1:]))
    query_bigrams = list(zip(query_tokens, query_tokens[1:]))
    if not query_bigrams:
        return 0.0
    return sum(1 for pair in query_bigrams if pair in text_bigrams) / len(query_bigrams)


def proximity_score(query_terms: set[str], values: list[Any], window: int = 10) -> float:
    if len(query_terms) < 2:
        return 0.0
    text_tokens = tokens(" ".join(str(v or "") for v in values), keep_stopwords=False)
    positions = [idx for idx, token in enumerate(text_tokens) if token in query_terms]
    if len(positions) < 2:
        return 0.0
    best_span = min(b - a + 1 for a, b in zip(positions, positions[1:]))
    if best_span > window:
        return 0.0
    return 1.0 - ((best_span - 2) / max(1, window - 2))


def explicit_skill_coverage(
    candidate: dict[str, Any],
    query: QuerySpec,
    known_skills: set[str],
) -> float:
    query_compact = normalize_skill(query.query)
    explicit = {
        skill
        for skill in known_skills
        if normalize_skill(skill) in query_compact or skill in query.skills
    }
    explicit.update(query.skills)
    if not explicit:
        return 0.0
    candidate_skills = {skill.lower() for skill in candidate["skills"]}
    return len({skill.lower() for skill in explicit} & candidate_skills) / len(explicit)


def doc_type_match(candidate: dict[str, Any], query: QuerySpec) -> float:
    if not query.doc_types:
        return 0.0
    doc_types = {chunk["doc_type"] for chunk in candidate["chunks"]}
    return len(set(query.doc_types) & doc_types) / len(query.doc_types)


def candidate_text_values(candidate: dict[str, Any]) -> list[Any]:
    values = [
        candidate["full_name"],
        candidate["city"],
        candidate["country"],
        candidate["skills"],
    ]
    for chunk in candidate["chunks"]:
        values.extend([chunk["content"], chunk["doc_type"], chunk["document_title"]])
    return values


def document_values(candidate: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    for chunk in candidate["chunks"]:
        values.extend([chunk["content"], chunk["doc_type"], chunk["document_title"]])
    return values


def _query_terms(query: str) -> set[str]:
    return token_set([query])


def _candidate_lexical_score(query_terms: set[str], candidate: dict[str, Any]) -> float:
    return coverage(query_terms, candidate_text_values(candidate))


def feature_pack(
    candidate: dict[str, Any],
    query: QuerySpec,
    known_skills: set[str],
) -> dict[str, float]:
    query_terms = token_set([query.query])
    sim = candidate["best_similarity"]
    rrf_scaled = min(1.0, (candidate["best_rrf"] or 0.0) / 0.0325)
    all_values = candidate_text_values(candidate)
    docs = document_values(candidate)
    return {
        "sim": sim,
        "rrf": rrf_scaled,
        "current_lex": _candidate_lexical_score(_query_terms(query.query), candidate),
        "skill": explicit_skill_coverage(candidate, query, known_skills),
        "profile": coverage(query_terms, [
            candidate["full_name"],
            candidate["city"],
            candidate["country"],
            candidate["skills"],
        ]),
        "doc": coverage(query_terms, docs),
        "all": coverage(query_terms, all_values),
        "phrase": phrase_score(query.query, all_values),
        "proximity": proximity_score(query_terms, all_values),
        "doc_type": doc_type_match(candidate, query),
        "exp": min(1.0, candidate.get("years_exp", 0) / 12.0),
    }


def build_formulas() -> list[Formula]:
    return [
        Formula(
            "semantic_only",
            "Semantic only",
            "score = best_vector_similarity",
            "Pure pgvector similarity over the retrieved pool. Lowest latency, weakest on exact skills.",
            lambda c, f: f["sim"],
        ),
        Formula(
            "rrf_only",
            "RRF only",
            "score = scaled_rrf",
            "Uses keyword+semantic Reciprocal Rank Fusion order, without profile-level evidence.",
            lambda c, f: f["rrf"],
        ),
        Formula(
            "current_hardcoded",
            "Old lexical blend",
            "score = sim + min(0.35, lexical_count*0.05) + raw_rrf*2",
            "The previous live scoring shape. The query-specific chat/system boost has been removed from the shared helper.",
            lambda c, f: f["sim"] + min(0.35, f["current_lex"] * 0.05) + ((c["best_rrf"] or 0.0) * 2.0),
        ),
        Formula(
            "balanced_lexical",
            "Balanced lexical",
            "score = 0.65*sim + 0.15*rrf + 0.10*skill + 0.10*doc",
            "No query-specific hack. Keeps semantic similarity dominant but rewards explicit skills and document term coverage.",
            lambda c, f: 0.65 * f["sim"] + 0.15 * f["rrf"] + 0.10 * f["skill"] + 0.10 * f["doc"],
        ),
        Formula(
            "skill_first",
            "Skill first",
            "score = 0.45*sim + 0.15*rrf + 0.30*skill + 0.07*profile + 0.03*exp",
            "Recruiter-style formula. Best when the query names concrete skills; riskier for vague semantic queries.",
            lambda c, f: (
                0.45 * f["sim"]
                + 0.15 * f["rrf"]
                + 0.30 * f["skill"]
                + 0.07 * f["profile"]
                + 0.03 * f["exp"]
            ),
        ),
        Formula(
            "phrase_proximity",
            "Phrase/proximity",
            "score = 0.55*sim + 0.15*rrf + 0.15*skill + 0.10*phrase + 0.05*proximity",
            "Generic phrase and nearby-term evidence, replacing the special chat/system hack.",
            lambda c, f: (
                0.55 * f["sim"]
                + 0.15 * f["rrf"]
                + 0.15 * f["skill"]
                + 0.10 * f["phrase"]
                + 0.05 * f["proximity"]
            ),
        ),
        Formula(
            "candidate_evidence",
            "Candidate evidence",
            "score = 0.50*sim + 0.12*rrf + 0.18*skill + 0.08*profile + 0.08*doc + 0.04*exp",
            "Balanced candidate-level evidence across profile fields, documents, skill match, and experience.",
            lambda c, f: (
                0.50 * f["sim"]
                + 0.12 * f["rrf"]
                + 0.18 * f["skill"]
                + 0.08 * f["profile"]
                + 0.08 * f["doc"]
                + 0.04 * f["exp"]
            ),
        ),
        Formula(
            "strict_recruiter",
            "Strict recruiter",
            "score = 0.38*sim + 0.10*rrf + 0.35*skill + 0.07*phrase + 0.05*doc_type + 0.05*exp",
            "Aggressively prioritizes explicit skill coverage and requested document type.",
            lambda c, f: (
                0.38 * f["sim"]
                + 0.10 * f["rrf"]
                + 0.35 * f["skill"]
                + 0.07 * f["phrase"]
                + 0.05 * f["doc_type"]
                + 0.05 * f["exp"]
            ),
        ),
    ]


def make_filters(query: QuerySpec) -> SearchFilters:
    return SearchFilters(
        country=query.country,
        city=query.city,
        skills=query.skills or None,
        skills_match=query.skills_match,  # type: ignore[arg-type]
        doc_types=query.doc_types or None,
        min_years_exp=query.min_years_exp,
    )


def group_rows(rows: list[Any], candidate_meta: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        cid = str(row["candidate_id"])
        distance = float(row["distance"])
        rrf = float(row["rrf_score"]) if row["rrf_score"] is not None else 0.0
        chunk = {
            "content": row["matched_chunk"],
            "distance": distance,
            "similarity": max(0.0, 1.0 - distance),
            "doc_type": row["doc_type"],
            "document_title": row["document_title"],
            "document_id": str(row["document_id"]),
            "rrf_score": rrf,
        }
        if cid not in candidates:
            meta = candidate_meta.get(cid, {})
            candidates[cid] = {
                "candidate_id": cid,
                "full_name": row["full_name"],
                "email": row["email"],
                "city": row["city"],
                "country": row["country"],
                "skills": list(row["skills"] or []),
                "years_exp": int(meta.get("years_exp", 0) or 0),
                "chunks": [],
                "best_similarity": 0.0,
                "best_rrf": 0.0,
            }
        candidates[cid]["chunks"].append(chunk)
        candidates[cid]["best_similarity"] = max(candidates[cid]["best_similarity"], chunk["similarity"])
        candidates[cid]["best_rrf"] = max(candidates[cid]["best_rrf"], rrf)
    return candidates


async def fetch_known_skills(pool: Any) -> set[str]:
    rows = await pool.fetch("SELECT DISTINCT unnest(skills) AS skill FROM candidates")
    return {row["skill"].lower() for row in rows}


async def fetch_candidate_meta(pool: Any) -> dict[str, dict[str, Any]]:
    rows = await pool.fetch("SELECT id, years_exp FROM candidates")
    return {
        str(row["id"]): {
            "years_exp": row["years_exp"],
        }
        for row in rows
    }


async def resolve_expected(pool: Any, query: QuerySpec) -> str:
    row = await pool.fetchrow(
        """
        SELECT id
        FROM candidates
        WHERE full_name = $1
          AND country = $2
          AND city = $3
          AND status = 'active'
        """,
        query.expected_name,
        query.expected_country,
        query.expected_city,
    )
    if not row:
        raise RuntimeError(f"Expected candidate not found for {query.id}: {query.expected_name}")
    return str(row["id"])


def evaluate_rank(
    ranked: list[dict[str, Any]],
    expected_id: str,
) -> dict[str, Any]:
    rank: int | None = None
    for idx, candidate in enumerate(ranked, start=1):
        if candidate["candidate_id"] == expected_id:
            rank = idx
            break
    if rank is None:
        return {"rank": None, "top1": 0.0, "hit3": 0.0, "mrr10": 0.0, "ndcg10": 0.0}
    return {
        "rank": rank,
        "top1": 1.0 if rank == 1 else 0.0,
        "hit3": 1.0 if rank <= 3 else 0.0,
        "mrr10": 1.0 / rank if rank <= 10 else 0.0,
        "ndcg10": 1.0 / math.log2(rank + 1) if rank <= 10 else 0.0,
    }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def summarize(results: list[dict[str, Any]], formulas: list[Formula]) -> list[dict[str, Any]]:
    summary = []
    for formula in formulas:
        rows = [row["formulas"][formula.id] for row in results]
        ranks = [row["rank"] if row["rank"] is not None else 999 for row in rows]
        summary.append({
            "formula_id": formula.id,
            "name": formula.name,
            "top1": round(sum(row["top1"] for row in rows) / len(rows), 4),
            "hit3": round(sum(row["hit3"] for row in rows) / len(rows), 4),
            "mrr10": round(sum(row["mrr10"] for row in rows) / len(rows), 4),
            "ndcg10": round(sum(row["ndcg10"] for row in rows) / len(rows), 4),
            "avg_rank": round(sum(ranks) / len(ranks), 2),
            "median_rank": round(statistics.median(ranks), 2),
            "rerank_p50_ms": round(percentile([row["rerank_ms"] for row in rows], 50), 4),
            "rerank_p95_ms": round(percentile([row["rerank_ms"] for row in rows], 95), 4),
        })
    return sorted(summary, key=lambda row: (-row["mrr10"], -row["top1"], row["avg_rank"]))


async def run_evaluation(export_langfuse: bool = False) -> dict[str, Any]:
    pool = await get_pool()
    embedder = get_embedder()
    engine = HybridSearchEngine(pool, embedder=embedder)
    known_skills = await fetch_known_skills(pool)
    candidate_meta = await fetch_candidate_meta(pool)
    formulas = build_formulas()

    results: list[dict[str, Any]] = []
    retrieval_latencies: list[float] = []
    
    lf = None
    run_name = None
    if export_langfuse:
        try:
            from langfuse import Langfuse
            lf = Langfuse()
            run_name = f"ranker-eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            print(f"Langfuse export enabled. Session: {run_name}")
        except ImportError:
            print("Warning: langfuse package not installed, skipping export.")
            export_langfuse = False

    for query in QUERIES:
        expected_id = await resolve_expected(pool, query)
        filters = make_filters(query)
        retrieval_start = time.perf_counter()
        embedding = (await embedder.embed([query.query]))[0]
        rows = await engine._search_rrf(query.query, embedding, filters, fetch_limit=300)
        retrieval_ms = (time.perf_counter() - retrieval_start) * 1000.0
        retrieval_latencies.append(retrieval_ms)

        candidates = group_rows(rows, candidate_meta)
        formula_results: dict[str, Any] = {}
        for formula in formulas:
            rerank_start = time.perf_counter()
            scored: list[dict[str, Any]] = []
            for candidate in candidates.values():
                features = feature_pack(candidate, query, known_skills)
                score = formula.scorer(candidate, features)
                scored.append({
                    **candidate,
                    "score": score,
                    "features": features,
                })
            scored.sort(key=lambda candidate: candidate["score"], reverse=True)
            rerank_ms = (time.perf_counter() - rerank_start) * 1000.0
            metrics = evaluate_rank(scored, expected_id)
            top = scored[0] if scored else None
            formula_results[formula.id] = {
                **metrics,
                "rerank_ms": rerank_ms,
                "top_candidate": top["full_name"] if top else None,
                "top_city": top["city"] if top else None,
                "top_country": top["country"] if top else None,
                "top_score": round(top["score"], 6) if top else 0.0,
                "expected_score": round(
                    next((item["score"] for item in scored if item["candidate_id"] == expected_id), 0.0),
                    6,
                ),
            }
            
            if export_langfuse and lf:
                try:
                    trace = lf.trace(
                        name=f"eval_{formula.id}",
                        session_id=run_name,
                        input={"query": query.query},
                        output={"top_candidate": top["full_name"] if top else None}
                    )
                    lf.create_dataset_run_item(
                        dataset_name="recruiter_golden_queries_v1",
                        dataset_item_id=query.id,
                        run_name=f"{run_name}_{formula.id}",
                        trace_id=trace.id
                    )
                    lf.score(trace_id=trace.id, name="TOP1", value=metrics["top1"])
                    lf.score(trace_id=trace.id, name="MRR10", value=metrics["mrr10"])
                    lf.score(trace_id=trace.id, name="NDCG10", value=metrics["ndcg10"])
                except Exception as exc:
                    pass

        results.append({
            "id": query.id,
            "query": query.query,
            "filters": {
                "country": query.country,
                "city": query.city,
                "skills": query.skills,
                "skills_match": query.skills_match,
                "doc_types": query.doc_types,
                "min_years_exp": query.min_years_exp,
            },
            "expected_candidate": query.expected_name,
            "expected_city": query.expected_city,
            "expected_country": query.expected_country,
            "reason": query.reason,
            "candidate_pool_size": len(candidates),
            "retrieval_ms": round(retrieval_ms, 3),
            "formulas": formula_results,
        })

    formula_summary = summarize(results, formulas)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": {
            "queries": len(QUERIES),
            "retrieval": "RRF semantic+keyword pool, fetch_limit=300",
            "labeling": "Manual labels over the current 100-candidate seeded database.",
            "retrieval_p50_ms": round(percentile(retrieval_latencies, 50), 3),
            "retrieval_p95_ms": round(percentile(retrieval_latencies, 95), 3),
        },
        "formulas": [
            {
                "id": formula.id,
                "name": formula.name,
                "expression": formula.expression,
                "notes": formula.notes,
            }
            for formula in formulas
        ],
        "summary": formula_summary,
        "queries": results,
    }


def bar_chart(rows: list[dict[str, Any]], key: str, *, percent: bool = False, lower_is_better: bool = False) -> str:
    width = 920
    row_h = 34
    label_w = 190
    chart_w = 620
    height = 30 + row_h * len(rows)
    values = [float(row[key]) for row in rows]
    max_value = max(values) if values else 1.0
    if percent:
        max_value = 1.0
    if lower_is_better and values:
        max_value = max(values)
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(key)} chart">'
    ]
    for idx, row in enumerate(rows):
        y = 24 + idx * row_h
        value = float(row[key])
        bar_w = 0 if max_value <= 0 else (value / max_value) * chart_w
        display = f"{value * 100:.1f}%" if percent else f"{value:.3f}"
        if key in {"avg_rank", "median_rank"}:
            display = f"{value:.2f}"
        fill = "#137c70" if not lower_is_better else "#345f92"
        parts.extend([
            f'<text x="0" y="{y + 18}" class="chart-label">{html.escape(row["name"])}</text>',
            f'<rect x="{label_w}" y="{y}" width="{chart_w}" height="22" rx="4" class="chart-bg" />',
            f'<rect x="{label_w}" y="{y}" width="{bar_w:.1f}" height="22" rx="4" fill="{fill}" />',
            f'<text x="{label_w + chart_w + 12}" y="{y + 17}" class="chart-value">{display}</text>',
        ])
    parts.append("</svg>")
    return "\n".join(parts)


def metric_table(summary: list[dict[str, Any]]) -> str:
    cells = []
    for row in summary:
        cells.append(
            "<tr>"
            f"<td>{html.escape(row['name'])}</td>"
            f"<td>{row['top1'] * 100:.1f}%</td>"
            f"<td>{row['hit3'] * 100:.1f}%</td>"
            f"<td>{row['mrr10']:.3f}</td>"
            f"<td>{row['ndcg10']:.3f}</td>"
            f"<td>{row['avg_rank']:.2f}</td>"
            f"<td>{row['rerank_p95_ms']:.4f} ms</td>"
            "</tr>"
        )
    return "\n".join(cells)


def formula_table(formulas: list[dict[str, str]]) -> str:
    rows = []
    for formula in formulas:
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(formula['name'])}</strong><br><code>{html.escape(formula['id'])}</code></td>"
            f"<td><code>{html.escape(formula['expression'])}</code></td>"
            f"<td>{html.escape(formula['notes'])}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def query_table(report: dict[str, Any]) -> str:
    ordered_formulas = [row["formula_id"] for row in report["summary"]]
    header = "".join(f"<th>{html.escape(fid)}</th>" for fid in ordered_formulas)
    rows = []
    for query in report["queries"]:
        ranks = []
        for fid in ordered_formulas:
            result = query["formulas"][fid]
            rank = result["rank"]
            cls = "good" if rank == 1 else "warn" if rank and rank <= 3 else "bad"
            rank_text = "miss" if rank is None else str(rank)
            ranks.append(f'<td class="{cls}">{rank_text}<br><small>{html.escape(result["top_candidate"] or "")}</small></td>')
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(query['id'])}</strong><br>{html.escape(query['query'])}"
            f"<br><small>{html.escape(json.dumps(query['filters']))}</small></td>"
            f"<td>{html.escape(query['expected_candidate'])}<br><small>{html.escape(query['reason'])}</small></td>"
            f"{''.join(ranks)}"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Query and filters</th><th>Expected result</th>"
        f"{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def render_html(report: dict[str, Any]) -> str:
    winner = report["summary"][0]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Reranking Evaluation</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #12202e; background: #f4f7fb; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 32px 24px 64px; }}
h1 {{ margin: 0 0 8px; font-size: 32px; }}
h2 {{ margin: 34px 0 12px; font-size: 22px; }}
p {{ line-height: 1.5; }}
.grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 20px 0; }}
.card {{ background: white; border: 1px solid #cdd9e5; border-radius: 8px; padding: 16px; box-shadow: 0 10px 25px rgba(17, 34, 51, 0.05); }}
.metric {{ color: #617287; font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }}
.value {{ font-size: 28px; font-weight: 800; margin-top: 6px; }}
table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #cdd9e5; border-radius: 8px; overflow: hidden; }}
th, td {{ padding: 10px 12px; border-bottom: 1px solid #e1e8f0; text-align: left; vertical-align: top; }}
th {{ background: #edf3f8; color: #475b70; font-size: 13px; }}
code {{ background: #edf3f8; padding: 2px 5px; border-radius: 4px; }}
small {{ color: #617287; }}
.chart {{ width: 100%; height: auto; background: white; border: 1px solid #cdd9e5; border-radius: 8px; padding: 12px; box-sizing: border-box; }}
.chart-label {{ font-size: 14px; fill: #26384a; font-weight: 700; }}
.chart-value {{ font-size: 13px; fill: #26384a; }}
.chart-bg {{ fill: #e8eff6; }}
.good {{ background: #edf8f4; }}
.warn {{ background: #fff8e6; }}
.bad {{ background: #fff0f0; }}
.note {{ background: #fff; border-left: 4px solid #137c70; padding: 12px 16px; }}
</style>
</head>
<body>
<main>
<h1>Reranking Evaluation</h1>
<p>Generated {html.escape(report["generated_at"])} over {report["dataset"]["queries"]} manually labeled query/filter examples from the current 100-candidate seed.</p>

<div class="grid">
  <div class="card"><div class="metric">Recommended formula</div><div class="value">{html.escape(winner["name"])}</div></div>
  <div class="card"><div class="metric">Top-1 accuracy</div><div class="value">{winner["top1"] * 100:.1f}%</div></div>
  <div class="card"><div class="metric">MRR@10</div><div class="value">{winner["mrr10"]:.3f}</div></div>
  <div class="card"><div class="metric">Retrieval p95</div><div class="value">{report["dataset"]["retrieval_p95_ms"]:.1f} ms</div></div>
</div>

<div class="note">
This is a small local benchmark, not proof of production quality. It is useful for choosing a low-latency default and catching obvious ranking regressions before testing on larger data.
</div>

<h2>Metric Summary</h2>
<table>
<thead><tr><th>Formula</th><th>Top-1</th><th>Hit@3</th><th>MRR@10</th><th>nDCG@10</th><th>Avg rank</th><th>Rerank p95</th></tr></thead>
<tbody>{metric_table(report["summary"])}</tbody>
</table>

<h2>Top-1 Accuracy</h2>
{bar_chart(report["summary"], "top1", percent=True)}

<h2>MRR@10</h2>
{bar_chart(report["summary"], "mrr10")}

<h2>Average Expected Rank</h2>
{bar_chart(report["summary"], "avg_rank", lower_is_better=True)}

<h2>Formula Definitions</h2>
<table>
<thead><tr><th>Formula</th><th>Expression</th><th>Notes</th></tr></thead>
<tbody>{formula_table(report["formulas"])}</tbody>
</table>

<h2>Per-query Ranks</h2>
{query_table(report)}

<h2>Recommendation</h2>
<p>Use <strong>{html.escape(winner["name"])}</strong> as the next lightweight default if it stays ahead after more labels. If a model reranker is enabled later, keep it optional and apply it only to the top 10-50 candidates from this cheap ranker.</p>
</main>
</body>
</html>
"""


async def main() -> None:
    export_langfuse = "--langfuse" in sys.argv
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = await run_evaluation(export_langfuse=export_langfuse)
    JSON_OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    HTML_OUT.write_text(render_html(report), encoding="utf-8")
    print(f"Wrote {JSON_OUT}")
    print(f"Wrote {HTML_OUT}")
    print(json.dumps(report["summary"], indent=2))
    await close_pool()
    if export_langfuse:
        from langfuse import Langfuse
        Langfuse().flush()


if __name__ == "__main__":
    asyncio.run(main())
