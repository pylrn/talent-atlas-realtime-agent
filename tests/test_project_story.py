import re
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app


ROOT = Path(__file__).resolve().parents[1]
STORY_DIR = ROOT / "api" / "static" / "project-story"
HTML_FILE = STORY_DIR / "index.html"
DATA_FILE = STORY_DIR / "data.js"
CSS_FILE = STORY_DIR / "story.css"
JS_FILE = STORY_DIR / "story.js"


def test_journey_route_is_registered() -> None:
    routes = {route.path for route in app.routes}
    assert "/journey" in routes


def test_hosted_demo_auth_keeps_journey_public(monkeypatch) -> None:
    monkeypatch.setenv("APP_DEMO_USERNAME", "recruiter")
    monkeypatch.setenv("APP_DEMO_PASSWORD", "test-password")
    client = TestClient(app)

    assert client.get("/journey").status_code == 200
    assert client.get("/talent").status_code == 401
    assert client.get("/talent", auth=("recruiter", "test-password")).status_code == 200


def test_story_has_all_chapters_and_progressive_enhancement() -> None:
    html = HTML_FILE.read_text()
    chapter_ids = re.findall(r'<section class="chapter(?: [^"]+)?" id="([^"]+)"', html)

    assert chapter_ids == [
        "evolution",
        "hybrid",
        "configurable",
        "agent",
        "learning",
        "lessons",
    ]
    assert "<noscript>" in html
    assert 'aria-label="Story chapters"' in html
    assert 'id="readingProgress"' in html
    assert 'id="sourceDialog"' in html
    assert 'id="imageLightbox"' in html


def test_story_static_assets_exist() -> None:
    html = HTML_FILE.read_text()
    local_assets = set(
        re.findall(r'(?:src|href|data-lightbox-src)="(/static/project-story/[^"]+)"', html)
    )

    assert local_assets
    for url in local_assets:
        relative_path = url.removeprefix("/static/project-story/")
        assert (STORY_DIR / relative_path).is_file(), url


def test_inline_citations_resolve_to_source_ledger() -> None:
    html = HTML_FILE.read_text()
    data = DATA_FILE.read_text()
    citation_ids = set(re.findall(r'data-source="([^"]+)"', html))
    source_section = data.split("\n    sources: [", 1)[1].split("\n    ]", 1)[0]
    source_ids = set(re.findall(r'^\s+id: "([^"]+)",$', source_section, re.MULTILINE))

    assert citation_ids
    assert citation_ids <= source_ids


def test_rag_checklist_links_to_implemented_features() -> None:
    html = HTML_FILE.read_text()
    feature_targets = re.findall(r'class="feature-jump" href="#([^"]+)"', html)
    page_ids = set(re.findall(r'id="([^"]+)"', html))

    assert len(feature_targets) == 11
    assert len(feature_targets) == len(set(feature_targets))
    assert set(feature_targets) <= page_ids


def test_story_contains_expected_evidence_components() -> None:
    html = HTML_FILE.read_text()
    data = DATA_FILE.read_text()
    script = JS_FILE.read_text()
    styles = CSS_FILE.read_text()

    assert "I built the search engine before I built the copilot" in html
    assert "Turn a hiring request into an evidence-backed shortlist" in html
    assert "searches the meaning inside resumes" in html
    assert 'aria-label="Talent Atlas in four simple steps"' in html
    assert "What makes it a complete RAG workflow" in html
    assert "A hybrid RAG search system for recruiting" in html
    assert "What “hybrid RAG” means here" in html
    assert "Try the RAG Search Demo" in html
    assert "Try the Live Hybrid RAG Demo" in html
    assert "More about this" in html
    assert (
        'href="https://ubuntu.com/blog/hybrid-search-and-reranking-a-deeper-look-at-rag"'
        in html
    )
    assert "Top-1 from 35% to 28.75%" in html
    assert "PydanticAI" in html and "Provider SDKs are still used directly" in html
    assert "The instructions narrow the model’s job" in html
    assert "Only explicit must-haves belong in filters" in html
    assert "agent-prompt-code" in data
    assert "Reciprocal Rank Fusion" in html
    assert 'id="pipeline"' in html
    assert 'data-pipeline-tab="search"' in html
    assert 'data-pipeline-tab="agent"' in html
    assert 'data-pipeline-tab="learning"' in html
    assert "Complete search endpoint pipeline" in html
    assert 'id="search-endpoint"' in html
    assert "One endpoint, twenty deliberate steps" in html
    assert len(re.findall(r'class="endpoint-phases"', html)) == 1
    endpoint_section = html.split('<section class="endpoint-deep-dive"', 1)[1].split(
        '<div class="pipeline-map-heading">', 1
    )[0]
    assert len(re.findall(r"<li><strong>", endpoint_section)) == 20
    assert "Run dense retrieval" in endpoint_section
    assert "Run keyword retrieval" in endpoint_section
    assert "Run exact-skill retrieval" in endpoint_section
    assert "Return and observe" in endpoint_section
    assert "langfuse-search-spans.png" in endpoint_section
    assert "Planner gate" in html
    assert "Retrieve in parallel" in html
    assert "Turn chunks into ranked people" in html
    assert "One query, three views of the same answer" in html
    assert '"ranking_explanation"' in html
    assert "Open full recorded search" in html
    assert "30,767 candidates" in html
    assert "all-MiniLM-L6-v2" in html
    assert "cross-encoder/ms-marco-MiniLM-L-6-v2" in html
    assert "config_overrides" in html
    assert "JSON as the contract" in html
    assert "Langfuse turned" in html
    assert "langfuse-agent-loop.png" in html
    assert "langfuse-search-spans.png" in html
    assert "private trace identifier" in html
    assert "window.PROJECT_STORY" in data
    assert "strategy-benchmark" in data
    assert "activatePipeline" in script
    assert "renderSources" in script
    assert "prefers-reduced-motion" in styles


def test_story_bundle_has_no_obvious_runtime_secrets_or_private_trace_urls() -> None:
    text_files = [HTML_FILE, DATA_FILE, CSS_FILE, JS_FILE]
    combined = "\n".join(path.read_text() for path in text_files)
    forbidden_patterns = {
        "OpenAI-style secret": r"\bsk-[A-Za-z0-9_-]{20,}",
        "Groq secret": r"\bgsk_[A-Za-z0-9]{20,}",
        "Google API key": r"\bAIza[A-Za-z0-9_-]{20,}",
        "Postgres credential URL": r"postgres(?:ql)?://[^\s:@]+:[^\s@]+@",
        "Langfuse secret": r"\bsk-lf-[A-Za-z0-9-]{12,}",
        "Private Langfuse trace URL": r"https?://[^\s\"']*langfuse[^\s\"']*/trace/",
    }

    for label, pattern in forbidden_patterns.items():
        assert re.search(pattern, combined) is None, label
