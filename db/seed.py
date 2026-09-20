"""
Seed the database with realistic test data.

Generates candidates with structured data and sample documents
(transcripts, certifications, bios) for testing the hybrid search pipeline.

Usage:
    python db/seed.py --candidates 100
"""

from __future__ import annotations

import asyncio
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from pipeline import settings
from pipeline.ingest import IngestionPipeline

# ─── Realistic data pools ─────────────────

FIRST_NAMES = [
    "Aarav", "Aditi", "Aisha", "Amit", "Ananya", "Arjun", "Chen", "David",
    "Elena", "Fatima", "Grace", "Hiroshi", "Isha", "James", "Kavya", "Liam",
    "Maria", "Neha", "Omar", "Priya", "Rahul", "Sara", "Takeshi", "Uma",
    "Victor", "Wei", "Xin", "Yuki", "Zara", "Michael", "Sophie", "Raj",
]

LAST_NAMES = [
    "Sharma", "Patel", "Kumar", "Singh", "Wang", "Chen", "Li", "Zhang",
    "Garcia", "Martinez", "Johnson", "Williams", "Brown", "Smith", "Tanaka",
    "Kim", "Nguyen", "Ali", "Hassan", "Cohen", "Mueller", "Dubois", "Santos",
    "O'Brien", "Ivanov", "Sato", "Park", "Wilson", "Taylor", "Anderson",
]

COUNTRIES = ["India", "USA", "UK", "Germany", "Japan", "Canada", "Australia", "Singapore"]

CITIES = {
    "India": ["Bangalore", "Mumbai", "Delhi", "Hyderabad", "Pune", "Chennai"],
    "USA": ["San Francisco", "New York", "Seattle", "Austin", "Boston", "Chicago"],
    "UK": ["London", "Manchester", "Edinburgh", "Cambridge", "Bristol"],
    "Germany": ["Berlin", "Munich", "Hamburg", "Frankfurt"],
    "Japan": ["Tokyo", "Osaka", "Kyoto"],
    "Canada": ["Toronto", "Vancouver", "Montreal"],
    "Australia": ["Sydney", "Melbourne", "Brisbane"],
    "Singapore": ["Singapore"],
}

SKILLS = [
    "python", "javascript", "typescript", "rust", "go", "java", "c++", "sql",
    "react", "vue", "angular", "svelte", "node.js", "fastapi", "django", "flask",
    "aws", "gcp", "azure", "kubernetes", "docker", "terraform",
    "postgresql", "mongodb", "redis", "elasticsearch",
    "machine-learning", "deep-learning", "nlp", "computer-vision",
    "data-engineering", "devops", "security", "mobile", "flutter", "react-native",
]

INTERESTS = [
    "open-source", "startups", "ai-research", "web3", "gaming", "education",
    "healthcare", "fintech", "climate-tech", "robotics", "ux-design",
    "mentoring", "public-speaking", "technical-writing", "competitive-programming",
]

# ─── Document templates ───────────────────

TRANSCRIPT_TEMPLATES = [
    """Interview Transcript — {name}

Interviewer: Tell me about your experience with {skill1}.
{name}: I've been working with {skill1} for about {years} years now. At my previous role at {company}, I built {project}. The main challenge was {challenge}, which we solved by {solution}.

Interviewer: How do you approach {topic}?
{name}: I believe in {approach}. In my last project, we {achievement}. The key insight was {insight}.

Interviewer: Where do you see yourself in 5 years?
{name}: I want to {goal}. I'm particularly excited about {excitement} and think there's huge potential in {potential_area}.""",

    """Technical Screen — {name}

Interviewer: Walk me through your system design for {system}.
{name}: I'd start with {component1} for the {purpose1}. Then use {component2} to handle {purpose2}. For scaling, I'd implement {scaling_strategy}. The database layer would use {db_choice} because {db_reason}.

Interviewer: What about failure scenarios?
{name}: Good question. I'd implement {resilience_pattern} for {failure_type}. We'd also need {monitoring} to catch {issue} early. At {company}, we had a similar situation where {incident_story}.""",
]

CERTIFICATION_TEMPLATES = [
    "Certified {cert_name} — Issued by {issuer}, {year}. Scored {score}% on the examination covering {topics}. Valid through {valid_year}.",
    "{cert_name} Professional Certification — {issuer}. Completed {hours} hours of training in {topics}. Project: {project}. Grade: {grade}.",
]

BIO_TEMPLATES = [
    """{name} is a {title} with {years} years of experience specializing in {specialization}. Currently based in {city}, {country}, they have worked at companies including {companies}. Their core expertise includes {skills_text}. {name} is passionate about {passion} and has {achievement}. In their free time, they enjoy {hobby} and contribute to {oss_project}.""",
]

# ─── Fill data ────────────────────────────

COMPANIES = ["TechCorp", "DataFlow", "CloudNine", "InnovateLab", "ScaleUp", "DeepMind", "QuantumLeap", "NeuralWorks", "ByteForge", "CodeCraft"]
PROJECTS = ["a real-time analytics dashboard", "a distributed task queue", "an ML inference pipeline", "a search engine", "a payment processing system", "a notification service", "a content recommendation engine"]
CHALLENGES = ["handling 10x traffic spikes", "reducing P99 latency below 50ms", "migrating a monolith to microservices", "ensuring data consistency across regions", "optimizing memory usage for large datasets"]
SOLUTIONS = ["implementing circuit breakers and load shedding", "using a CQRS pattern with event sourcing", "building a custom connection pool", "introducing sharded indexes", "adopting a streaming architecture"]
SYSTEMS = ["a URL shortener at scale", "a real-time chat system", "a rate limiter", "a distributed cache", "a feed ranking system", "a job scheduling platform"]
CERTS = ["AWS Solutions Architect", "Google Cloud Professional", "Kubernetes Administrator (CKA)", "Terraform Associate", "MongoDB Developer", "PostgreSQL Expert", "CISSP Security Professional", "Scrum Master (PSM)", "Azure Data Engineer"]
ISSUERS = ["Amazon Web Services", "Google Cloud", "CNCF", "HashiCorp", "MongoDB Inc.", "PostgreSQL Foundation", "ISC2", "Scrum.org", "Microsoft"]


def _random_transcript(name: str, skills: list[str]) -> str:
    template = random.choice(TRANSCRIPT_TEMPLATES)
    return template.format(
        name=name,
        skill1=random.choice(skills) if skills else "Python",
        years=random.randint(2, 10),
        company=random.choice(COMPANIES),
        project=random.choice(PROJECTS),
        challenge=random.choice(CHALLENGES),
        solution=random.choice(SOLUTIONS),
        topic="system design" if random.random() > 0.5 else "code quality",
        approach="iterative development with strong testing" if random.random() > 0.5 else "data-driven decision making",
        achievement="reduced deployment failures by 60%",
        insight="simplicity scales better than complexity",
        goal="lead a distributed engineering team",
        excitement="the intersection of AI and developer tools",
        potential_area="automated code review and testing",
        system=random.choice(SYSTEMS),
        component1="a load balancer",
        purpose1="distributing requests evenly",
        component2="a message queue (Kafka)",
        purpose2="async processing",
        scaling_strategy="horizontal auto-scaling with consistent hashing",
        db_choice="PostgreSQL" if random.random() > 0.3 else "MongoDB",
        db_reason="it handles both structured queries and JSONB for flexibility",
        resilience_pattern="retry with exponential backoff",
        failure_type="downstream service timeouts",
        monitoring="distributed tracing with OpenTelemetry",
        issue="cascading failures",
        incident_story="a database failover caused a 2-hour outage that we prevented from recurring by implementing read replicas",
    )


def _random_certification() -> tuple[str, str]:
    idx = random.randint(0, len(CERTS) - 1)
    cert = CERTS[idx]
    issuer = ISSUERS[idx] if idx < len(ISSUERS) else "Professional Institute"
    text = random.choice(CERTIFICATION_TEMPLATES).format(
        cert_name=cert,
        issuer=issuer,
        year=random.randint(2021, 2025),
        score=random.randint(75, 98),
        topics="cloud architecture, networking, and security best practices",
        valid_year=random.randint(2026, 2029),
        hours=random.randint(40, 200),
        project="a multi-region deployment on " + random.choice(["AWS", "GCP", "Azure"]),
        grade=random.choice(["A", "A+", "Distinction", "Pass with Merit"]),
    )
    return cert, text


def _random_bio(name: str, city: str, country: str, skills: list[str], years: int) -> str:
    return random.choice(BIO_TEMPLATES).format(
        name=name,
        title=random.choice(["Software Engineer", "Senior Developer", "Tech Lead", "Data Scientist", "ML Engineer", "DevOps Engineer"]),
        years=years,
        specialization=", ".join(random.sample(skills, min(3, len(skills)))) if skills else "software development",
        city=city,
        country=country,
        companies=", ".join(random.sample(COMPANIES, 3)),
        skills_text=", ".join(skills[:5]) if skills else "general software development",
        passion="building tools that make developers more productive",
        achievement="contributed to several major open-source projects",
        hobby=random.choice(["hiking", "photography", "playing chess", "building mechanical keyboards", "cooking"]),
        oss_project=random.choice(["React", "PostgreSQL", "Kubernetes", "FastAPI", "Linux"]) + " open-source community",
    )


async def seed(num_candidates: int = 100):
    """Generate and insert test candidates with documents."""
    pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=2, max_size=5)
    pipeline = IngestionPipeline(pool)

    print(f"Seeding {num_candidates} candidates...")

    for i in range(num_candidates):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        name = f"{first} {last}"
        country = random.choice(COUNTRIES)
        city = random.choice(CITIES[country])
        skills = random.sample(SKILLS, random.randint(3, 8))
        interests = random.sample(INTERESTS, random.randint(2, 5))
        years_exp = random.randint(0, 15)
        age = random.randint(21, 55)

        # Insert candidate
        row = await pool.fetchrow(
            """
            INSERT INTO candidates (full_name, email, age, location, city, country,
                                    interests, skills, years_exp, salary_min, salary_max, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING id
            """,
            name,
            f"{first.lower()}.{last.lower()}{random.randint(1,999)}@example.com",
            age,
            f"{city}, {country}",
            city,
            country,
            interests,
            skills,
            years_exp,
            random.randint(40, 120) * 1000,
            random.randint(80, 250) * 1000,
            random.choice(["active"] * 8 + ["archived"] + ["hired"]),  # 80% active
        )
        candidate_id = str(row["id"])

        # Generate 1-3 documents per candidate
        num_docs = random.randint(1, 3)
        for _ in range(num_docs):
            doc_type = random.choice(["transcript", "certification", "bio"])

            if doc_type == "transcript":
                text = _random_transcript(name, skills)
                title = "Interview Transcript"
            elif doc_type == "certification":
                cert_name, text = _random_certification()
                title = cert_name
            else:
                text = _random_bio(name, city, country, skills, years_exp)
                title = "Professional Bio"

            await pipeline.ingest(
                candidate_id=candidate_id,
                doc_type=doc_type,
                title=title,
                raw_text=text,
            )

        if (i + 1) % 10 == 0:
            print(f"  ✓ {i + 1}/{num_candidates} candidates seeded")

    # Print stats
    total_candidates = await pool.fetchval("SELECT COUNT(*) FROM candidates")
    total_docs = await pool.fetchval("SELECT COUNT(*) FROM candidate_documents")
    total_chunks = await pool.fetchval("SELECT COUNT(*) FROM document_chunks")

    print(f"\n{'='*50}")
    print("Seeding complete!")
    print(f"  Candidates: {total_candidates}")
    print(f"  Documents:  {total_docs}")
    print(f"  Chunks:     {total_chunks}")
    print(f"{'='*50}")

    await pool.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Seed database with test data")
    parser.add_argument("--candidates", type=int, default=100, help="Number of candidates to generate")
    args = parser.parse_args()
    asyncio.run(seed(args.candidates))
