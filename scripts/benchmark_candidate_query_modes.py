"""Benchmark no-LLM vs quality mode on candidate-specific query probes.

The script samples real candidates from the current database, builds three
query styles from each resume/profile, then checks whether the expected
candidate appears in the top 1/5/10 for each search mode.

Usage:
    python scripts/benchmark_candidate_query_modes.py --sample-size 6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pipeline.database import create_pool
from pipeline.search import HybridSearchEngine


REPORT_DIR = Path("reports")
JSON_OUT = REPORT_DIR / "candidate_query_mode_benchmark.json"
MD_OUT = REPORT_DIR / "candidate_query_mode_benchmark.md"

_COMMON_SOFT_SKILLS = {
    "communication",
    "sales",
    "marketing",
    "social-media",
    "project-management",
    "human-resources",
    "teamwork",
    "leadership",
    "time-management",
    "problem-solving",
    "research",
    "writing",
}

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")
_STOPWORDS = {
    "about", "across", "also", "and", "are", "candidate", "contact", "email",
    "experience", "experienced", "floor", "from", "has", "have", "info",
    "linkedin", "montgomery", "phone", "profile", "resume", "resumekraft",
    "sample", "skills", "summary", "their", "this", "with", "work", "worked",
    "years",
}


@dataclass(frozen=True)
class CandidateProbe:
    candidate_id: str
    name: str
    role: str
    city: str | None
    country: str | None
    years_exp: int
    skills: list[str]
    resume_excerpt: str


@dataclass(frozen=True)
class QueryCase:
    case_id: str
    category: str
    candidate: CandidateProbe
    query: str
    filters: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass(frozen=True)
class CuratedProbeSpec:
    full_name: str
    profile_role: str
    obvious_query: str
    obvious_filters: dict[str, Any]
    less_obvious_query: str
    less_obvious_filters: dict[str, Any]
    vague_query: str
    vague_filters: dict[str, Any] = field(default_factory=dict)


CURATED_PROBES: tuple[CuratedProbeSpec, ...] = (
    CuratedProbeSpec(
        full_name="React Developer Candidate 12328",
        profile_role="Full-stack React developer",
        obvious_query="full-stack React TypeScript developer with Docker PostgreSQL web apps",
        obvious_filters={"city": "San Francisco", "country": "USA", "skills": ["react"]},
        less_obvious_query="developer with information technology services background building full-time web applications",
        less_obvious_filters={"country": "USA"},
        vague_query="someone who can build modern frontend product screens and connect them to backend data",
    ),
    CuratedProbeSpec(
        full_name="Java Developer Candidate 10757",
        profile_role="Java J2EE web application developer",
        obvious_query="Java J2EE Spring Boot web application developer with REST APIs",
        obvious_filters={"city": "Bangalore", "country": "India", "skills": ["java"], "min_years_exp": 5},
        less_obvious_query="engineer experienced across SDLC design development and maintenance of web based applications",
        less_obvious_filters={"country": "India"},
        vague_query="backend engineer for enterprise web applications with APIs and database work",
    ),
    CuratedProbeSpec(
        full_name="Sql Developer Candidate 12961",
        profile_role="Microsoft SQL Server data warehouse developer",
        obvious_query="Microsoft SQL Server TSQL SSIS SSRS data warehousing developer",
        obvious_filters={"city": "Hyderabad", "country": "India", "skills": ["sql"], "min_years_exp": 6},
        less_obvious_query="database developer doing performance tuning troubleshooting packages reports and stored procedures",
        less_obvious_filters={"country": "India"},
        vague_query="person who can make databases faster and build reporting pipelines",
    ),
    CuratedProbeSpec(
        full_name="Finance Candidate 4821",
        profile_role="Director of finance",
        obvious_query="director of finance with budgeting forecasting GAAP financial reporting",
        obvious_filters={"city": "New York", "country": "USA", "skills": ["finance"], "min_years_exp": 10},
        less_obvious_query="leader who aligns business initiatives with executives and builds high performance finance teams",
        less_obvious_filters={"country": "USA"},
        vague_query="senior person to manage company money planning controls and executive reporting",
    ),
    CuratedProbeSpec(
        full_name="Blockchain Candidate 886",
        profile_role="Blockchain developer",
        obvious_query="blockchain developer with Bitcoin Ethereum Solidity Hyperledger and Java",
        obvious_filters={"city": "Mumbai", "country": "India", "skills": ["java"], "min_years_exp": 6},
        less_obvious_query="engineer with brain computer interface cloud computing data analytics networking and server admin",
        less_obvious_filters={"country": "India"},
        vague_query="engineer who understands crypto ledgers smart contracts and decentralized systems",
    ),
    CuratedProbeSpec(
        full_name="Digital Media Candidate 4508",
        profile_role="Global digital marketing director",
        obvious_query="global digital marketing director with B2B marketing social media and cross functional teams",
        obvious_filters={"city": "Boston", "country": "USA", "skills": ["marketing"], "min_years_exp": 8},
        less_obvious_query="results oriented leader delivering innovation profitable measurable outcomes with collaborative teams",
        less_obvious_filters={"country": "USA"},
        vague_query="person to grow digital campaigns and coordinate marketing teams",
    ),
    CuratedProbeSpec(
        full_name="Systems Administrator Candidate 25141",
        profile_role="Salesforce administrator developer",
        obvious_query="Salesforce administrator developer with Visualforce Apex custom objects workflows",
        obvious_filters={"city": "Pune", "country": "India", "skills": ["java"], "min_years_exp": 3},
        less_obvious_query="CRM admin configuring formula fields validation rules approval processes page layouts and dashboards",
        less_obvious_filters={"country": "India"},
        vague_query="someone to configure CRM workflows dashboards user permissions and business automations",
    ),
)


def _role_from_title(title: str | None, raw_text: str | None) -> str:
    title = title or ""
    if title.lower().startswith("resume - "):
        return title[9:].strip() or "candidate"
    match = re.search(r"Applied Role:\s*([^:]+?)(?:\s+Source Dataset:|\s+Age:|\s+Estimated Years|\s+Skills:)", raw_text or "")
    if match:
        return match.group(1).strip()
    return title.strip() or "candidate"


def _resume_body(raw_text: str) -> str:
    marker = "Resume Text:"
    if marker in raw_text:
        raw_text = raw_text.split(marker, 1)[1]
    return " ".join(raw_text.split())


def _clean_phrase_terms(text: str, limit: int = 7) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(text.lower()):
        token = raw.strip(".-")
        if len(token) < 4 or token in _STOPWORDS or token.isdigit():
            continue
        if token not in seen:
            seen.add(token)
            terms.append(token)
        if len(terms) >= limit:
            break
    return terms


def _hard_skills(skills: list[str], limit: int = 2) -> list[str]:
    preferred = [s for s in skills if s and s not in _COMMON_SOFT_SKILLS]
    chosen = preferred or skills
    return list(dict.fromkeys(chosen))[:limit]


def _vague_intent(role: str, skills: list[str]) -> str:
    skill_set = {s.lower() for s in skills}
    role_l = role.lower()
    if skill_set & {"python", "java", "javascript", "typescript", "react", "node", "sql", "go", "docker"}:
        return "build reliable software, work across systems, and solve technical problems"
    if skill_set & {"aws", "gcp", "azure", "kubernetes", "terraform", "linux"}:
        return "keep infrastructure running and handle deployment or operations work"
    if skill_set & {"finance", "accounting", "financial-reporting", "cost-accounting", "risk-management"}:
        return "handle numbers, reporting, controls, and business operations carefully"
    if skill_set & {"sales", "marketing", "crm", "social-media", "market-research"}:
        return "grow customer relationships, manage outreach, and communicate well"
    if skill_set & {"human-resources", "recruiting", "payroll"} or "manager" in role_l:
        return "coordinate people, handle process, and keep teams organized"
    if skill_set & {"healthcare", "patient-care", "pharmaceuticals", "medical-terminology"}:
        return "support patients or customers in a regulated service environment"
    if skill_set & {"prototyping", "product-development", "autocad", "catia", "materials-science"}:
        return "turn technical ideas into prototypes and practical product work"
    return "handle complex projects, communicate clearly, and deliver reliable outcomes"


def build_cases(
    candidates: list[CandidateProbe],
    curated_specs: dict[str, CuratedProbeSpec] | None = None,
) -> list[QueryCase]:
    cases: list[QueryCase] = []
    for idx, candidate in enumerate(candidates, start=1):
        curated = (curated_specs or {}).get(candidate.name)
        if curated:
            cases.append(QueryCase(
                case_id=f"c{idx:02d}-obvious",
                category="obvious",
                candidate=candidate,
                query=curated.obvious_query,
                filters=curated.obvious_filters,
                rationale="Hand-built precise role/skill/location query from the candidate resume.",
            ))
            cases.append(QueryCase(
                case_id=f"c{idx:02d}-less-obvious",
                category="less_obvious",
                candidate=candidate,
                query=curated.less_obvious_query,
                filters=curated.less_obvious_filters,
                rationale="Hand-built query from distinctive resume evidence, with only light filtering.",
            ))
            cases.append(QueryCase(
                case_id=f"c{idx:02d}-vague",
                category="vague",
                candidate=candidate,
                query=curated.vague_query,
                filters=curated.vague_filters,
                rationale="Intent-level query where LLM planning has the best chance to add value.",
            ))
            continue

        hard = _hard_skills(candidate.skills)
        soft_terms = _clean_phrase_terms(candidate.resume_excerpt, limit=8)
        filters: dict[str, Any] = {}
        if hard:
            filters["skills"] = hard
        if candidate.city:
            filters["city"] = candidate.city
        if candidate.country:
            filters["country"] = candidate.country
        if candidate.years_exp >= 3:
            filters["min_years_exp"] = max(0, candidate.years_exp - 2)

        obvious_skill_text = " and ".join(hard) if hard else "relevant"
        cases.append(QueryCase(
            case_id=f"c{idx:02d}-obvious",
            category="obvious",
            candidate=candidate,
            query=f"{candidate.role} with {obvious_skill_text} experience",
            filters=filters,
            rationale="Exact role/skill/location/experience query. Agent should manually structure this and no-LLM should usually be enough.",
        ))

        less_filters: dict[str, Any] = {}
        if candidate.country:
            less_filters["country"] = candidate.country
        phrase = " ".join(soft_terms[:6]) or " ".join(candidate.skills[:4])
        cases.append(QueryCase(
            case_id=f"c{idx:02d}-less-obvious",
            category="less_obvious",
            candidate=candidate,
            query=f"{candidate.role} profile involving {phrase}",
            filters=less_filters,
            rationale="Uses resume-ish text and role context, with only light explicit filtering.",
        ))

        cases.append(QueryCase(
            case_id=f"c{idx:02d}-vague",
            category="vague",
            candidate=candidate,
            query=f"someone who can {_vague_intent(candidate.role, candidate.skills)}",
            filters={},
            rationale="Conversational/vague query. This is where LLM planning should have the best chance to help.",
        ))
    return cases


async def load_curated_candidates(pool, sample_size: int) -> tuple[list[CandidateProbe], dict[str, CuratedProbeSpec]]:
    specs = list(CURATED_PROBES)[:sample_size]
    names = [spec.full_name for spec in specs]
    rows = await pool.fetch(
        """
        SELECT
            c.id::text AS candidate_id,
            c.full_name,
            c.city,
            c.country,
            c.years_exp,
            c.skills,
            cd.title,
            cd.raw_text
        FROM candidates c
        JOIN candidate_documents cd
          ON cd.candidate_id = c.id
         AND cd.doc_type = 'resume'
        WHERE c.full_name = ANY($1::text[])
        ORDER BY array_position($1::text[], c.full_name)
        """,
        names,
    )
    by_name = {spec.full_name: spec for spec in specs}
    candidates: list[CandidateProbe] = []
    for row in rows:
        raw = row["raw_text"] or ""
        spec = by_name[row["full_name"]]
        candidates.append(CandidateProbe(
            candidate_id=row["candidate_id"],
            name=row["full_name"],
            role=spec.profile_role,
            city=row["city"],
            country=row["country"],
            years_exp=int(row["years_exp"] or 0),
            skills=list(row["skills"] or []),
            resume_excerpt=_resume_body(raw)[:900],
        ))
    if len(candidates) != len(specs):
        found = {candidate.name for candidate in candidates}
        missing = [name for name in names if name not in found]
        raise RuntimeError(f"Missing curated candidates: {', '.join(missing)}")
    return candidates, by_name


async def sample_candidates(pool, sample_size: int, seed: str) -> list[CandidateProbe]:
    rows = await pool.fetch(
        """
        WITH ranked AS (
            SELECT
                c.id::text AS candidate_id,
                c.full_name,
                c.city,
                c.country,
                c.years_exp,
                c.skills,
                cd.title,
                cd.raw_text,
                row_number() OVER (
                    PARTITION BY cd.title
                    ORDER BY md5(c.id::text || $2)
                ) AS role_rank
            FROM candidates c
            JOIN candidate_documents cd
              ON cd.candidate_id = c.id
             AND cd.doc_type = 'resume'
            WHERE c.status = 'active'
              AND c.city IS NOT NULL
              AND c.country IS NOT NULL
              AND array_length(c.skills, 1) >= 4
              AND length(cd.raw_text) > 300
        )
        SELECT *
        FROM ranked
        WHERE role_rank = 1
        ORDER BY md5(candidate_id || $2)
        LIMIT $1
        """,
        sample_size,
        seed,
    )
    candidates: list[CandidateProbe] = []
    for row in rows:
        raw = row["raw_text"] or ""
        candidates.append(CandidateProbe(
            candidate_id=row["candidate_id"],
            name=row["full_name"],
            role=_role_from_title(row["title"], raw),
            city=row["city"],
            country=row["country"],
            years_exp=int(row["years_exp"] or 0),
            skills=list(row["skills"] or []),
            resume_excerpt=_resume_body(raw)[:900],
        ))
    return candidates


def _rank_metrics(results: list[Any], expected_id: str) -> dict[str, Any]:
    rank = None
    top_names: list[str] = []
    for idx, result in enumerate(results, start=1):
        top_names.append(getattr(result, "full_name", "") or getattr(result, "candidate_id", "")[:8])
        if str(getattr(result, "candidate_id", "")) == expected_id:
            rank = idx
    if rank is None:
        return {
            "rank": None,
            "top1": 0,
            "hit5": 0,
            "hit10": 0,
            "mrr10": 0.0,
            "top_names": top_names[:5],
        }
    return {
        "rank": rank,
        "top1": 1 if rank == 1 else 0,
        "hit5": 1 if rank <= 5 else 0,
        "hit10": 1 if rank <= 10 else 0,
        "mrr10": (1.0 / rank) if rank <= 10 else 0.0,
        "top_names": top_names[:5],
    }


async def run_case(engine: HybridSearchEngine, case: QueryCase, mode: str, top_k: int) -> dict[str, Any]:
    started = time.perf_counter()
    resp = await engine.smart_search(
        query=case.query,
        explicit_filters=case.filters or None,
        mode=mode,
        top_k=top_k,
        recruiter_id=None,
        config_overrides={
            "use_cache": False,
            "use_impression_logging": False,
        },
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics = _rank_metrics(resp.results, case.candidate.candidate_id)
    spec = getattr(resp, "spec", None)
    must = getattr(spec, "must", None)
    should = getattr(spec, "should", None)
    return {
        "mode": mode,
        "elapsed_ms": round(elapsed_ms, 2),
        "phase_timings": getattr(resp, "phase_timings", {}),
        "total_candidates_scanned": getattr(resp, "total_candidates_scanned", 0),
        "result_count": len(resp.results),
        "clarify": getattr(resp, "clarify", None),
        **metrics,
        "planner": {
            "used_fallback": bool(getattr(spec, "used_fallback", False)),
            "confidence": getattr(spec, "confidence", None),
            "semantic_query": getattr(spec, "semantic_query", ""),
            "lexical_terms": list(getattr(spec, "lexical_terms", []) or []),
            "must_skills": list(getattr(must, "skills", []) or []),
            "should_skills": list(getattr(should, "skills", []) or []),
            "should_themes": list(getattr(should, "themes", []) or []),
        },
    }


def summarize(rows: list[dict[str, Any]], modes: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {"overall": {}, "by_category": {}, "mode_deltas": []}
    for mode in modes:
        mode_rows = [row["runs"][mode] for row in rows]
        count = len(mode_rows) or 1
        ranks = [item["rank"] if item["rank"] is not None else 999 for item in mode_rows]
        summary["overall"][mode] = {
            "top1": round(sum(item["top1"] for item in mode_rows) / count, 3),
            "hit5": round(sum(item["hit5"] for item in mode_rows) / count, 3),
            "hit10": round(sum(item["hit10"] for item in mode_rows) / count, 3),
            "mrr10": round(sum(item["mrr10"] for item in mode_rows) / count, 3),
            "median_rank_or_999": round(statistics.median(ranks), 2),
            "p50_ms": round(statistics.median(item["elapsed_ms"] for item in mode_rows), 2),
            "avg_ms": round(statistics.mean(item["elapsed_ms"] for item in mode_rows), 2),
        }

    categories = sorted({row["category"] for row in rows})
    for category in categories:
        summary["by_category"][category] = {}
        category_rows = [row for row in rows if row["category"] == category]
        for mode in modes:
            mode_rows = [row["runs"][mode] for row in category_rows]
            count = len(mode_rows) or 1
            summary["by_category"][category][mode] = {
                "top1": round(sum(item["top1"] for item in mode_rows) / count, 3),
                "hit5": round(sum(item["hit5"] for item in mode_rows) / count, 3),
                "hit10": round(sum(item["hit10"] for item in mode_rows) / count, 3),
                "mrr10": round(sum(item["mrr10"] for item in mode_rows) / count, 3),
                "avg_ms": round(statistics.mean(item["elapsed_ms"] for item in mode_rows), 2),
            }

    if "no-llm" in modes and "quality" in modes:
        for row in rows:
            no_rank = row["runs"]["no-llm"]["rank"] or 999
            quality_rank = row["runs"]["quality"]["rank"] or 999
            if no_rank != quality_rank:
                summary["mode_deltas"].append({
                    "case_id": row["case_id"],
                    "category": row["category"],
                    "candidate": row["candidate"]["name"],
                    "query": row["query"],
                    "no_llm_rank": None if no_rank == 999 else no_rank,
                    "quality_rank": None if quality_rank == 999 else quality_rank,
                    "delta": no_rank - quality_rank,
                    "quality_added_skills": sorted(
                        set(row["runs"]["quality"]["planner"]["must_skills"] + row["runs"]["quality"]["planner"]["should_skills"])
                        - set(row["runs"]["no-llm"]["planner"]["must_skills"] + row["runs"]["no-llm"]["planner"]["should_skills"])
                    ),
                })
        summary["mode_deltas"].sort(key=lambda item: item["delta"], reverse=True)
    return summary


def write_markdown(payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    lines = [
        "# Candidate Query Mode Benchmark",
        "",
        f"- Candidate source: {payload['candidate_source']}",
        f"- Candidates sampled: {len(payload['candidates'])}",
        f"- Query cases: {len(payload['cases'])}",
        f"- Modes: {', '.join(payload['modes'])}",
        f"- Top-k searched: {payload['top_k']}",
        "- Planner cache disabled for measured runs",
        "- Impression logging disabled for measured runs",
        "",
        "## Overall",
        "",
        "| mode | top1 | hit5 | hit10 | mrr10 | median rank/999 | avg ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for mode, stats in summary["overall"].items():
        lines.append(
            f"| {mode} | {stats['top1']} | {stats['hit5']} | {stats['hit10']} | "
            f"{stats['mrr10']} | {stats['median_rank_or_999']} | {stats['avg_ms']} |"
        )

    lines.extend(["", "## By Category", ""])
    for category, mode_stats in summary["by_category"].items():
        lines.extend([
            f"### {category}",
            "",
            "| mode | top1 | hit5 | hit10 | mrr10 | avg ms |",
            "|---|---:|---:|---:|---:|---:|",
        ])
        for mode, stats in mode_stats.items():
            lines.append(
                f"| {mode} | {stats['top1']} | {stats['hit5']} | {stats['hit10']} | "
                f"{stats['mrr10']} | {stats['avg_ms']} |"
            )
        lines.append("")

    if summary.get("mode_deltas"):
        lines.extend([
            "## Biggest Rank Deltas",
            "",
            "| case | category | candidate | no-LLM | quality | delta | quality added skills | query |",
            "|---|---|---|---:|---:|---:|---|---|",
        ])
        for item in summary["mode_deltas"][:12]:
            lines.append(
                f"| {item['case_id']} | {item['category']} | {item['candidate']} | "
                f"{item['no_llm_rank'] or '-'} | {item['quality_rank'] or '-'} | "
                f"{item['delta']} | {', '.join(item['quality_added_skills']) or '-'} | "
                f"{item['query']} |"
            )

    lines.extend(["", "## Case Details", ""])
    for row in payload["cases"]:
        lines.append(f"### {row['case_id']} - {row['category']} - {row['candidate']['name']}")
        lines.append("")
        lines.append(f"Query: `{row['query']}`")
        lines.append("")
        lines.append(f"Filters: `{json.dumps(row['filters'], sort_keys=True)}`")
        lines.append("")
        for mode in payload["modes"]:
            run = row["runs"][mode]
            lines.append(
                f"- {mode}: rank={run['rank'] or '-'}, hit5={run['hit5']}, "
                f"results={run.get('result_count', 0)}, {run['elapsed_ms']}ms, "
                f"top5={', '.join(run['top_names'])}"
            )
            if run.get("clarify"):
                lines.append(f"  - clarify: {run['clarify']}")
        lines.append("")

    MD_OUT.write_text("\n".join(lines), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=6)
    parser.add_argument("--seed", default="candidate-mode-benchmark-v1")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--modes", default="no-llm,quality")
    parser.add_argument(
        "--random",
        action="store_true",
        help="Use random candidates and generated queries instead of curated resume probes.",
    )
    args = parser.parse_args()

    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    REPORT_DIR.mkdir(exist_ok=True)

    pool = await create_pool()
    try:
        curated_specs: dict[str, CuratedProbeSpec] | None = None
        candidate_source = "random"
        if args.random:
            candidates = await sample_candidates(pool, args.sample_size, args.seed)
        else:
            candidates, curated_specs = await load_curated_candidates(pool, args.sample_size)
            candidate_source = "curated"
        cases = build_cases(candidates, curated_specs)
        engine = HybridSearchEngine(pool)

        # Load/compile local models before measured cases where possible.
        await engine.embedder.embed(["warm up search embeddings"])

        rows: list[dict[str, Any]] = []
        total_runs = len(cases) * len(modes)
        run_idx = 0
        for case in cases:
            print(f"\n[{case.case_id}] {case.category} -> {case.candidate.name}")
            print(f"  query: {case.query}")
            print(f"  filters: {json.dumps(case.filters, sort_keys=True)}")
            runs: dict[str, Any] = {}
            for mode in modes:
                run_idx += 1
                print(f"  ({run_idx}/{total_runs}) {mode} ...", flush=True)
                try:
                    runs[mode] = await run_case(engine, case, mode, args.top_k)
                    print(
                        f"    rank={runs[mode]['rank'] or '-'} "
                        f"hit5={runs[mode]['hit5']} "
                        f"{runs[mode]['elapsed_ms']}ms"
                    )
                except Exception as exc:
                    print(f"    ERROR: {exc}")
                    runs[mode] = {
                        "mode": mode,
                        "elapsed_ms": None,
                        "rank": None,
                        "top1": 0,
                        "hit5": 0,
                        "hit10": 0,
                        "mrr10": 0.0,
                        "top_names": [],
                        "error": str(exc),
                        "planner": {},
                    }

            rows.append({
                "case_id": case.case_id,
                "category": case.category,
                "candidate": asdict(case.candidate),
                "query": case.query,
                "filters": case.filters,
                "rationale": case.rationale,
                "runs": runs,
            })

        payload = {
            "sample_size": args.sample_size,
            "seed": args.seed,
            "candidate_source": candidate_source,
            "top_k": args.top_k,
            "modes": modes,
            "candidates": [asdict(candidate) for candidate in candidates],
            "cases": rows,
            "summary": summarize(rows, modes),
        }
        JSON_OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        write_markdown(payload)

        print("\nSummary")
        print(json.dumps(payload["summary"]["overall"], indent=2))
        print(f"\nWrote {JSON_OUT}")
        print(f"Wrote {MD_OUT}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
