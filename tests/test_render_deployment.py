from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_render_blueprint_uses_free_plan_and_lightweight_models():
    manifest = (ROOT / "render.yaml").read_text()

    assert "plan: free" in manifest
    assert "dockerfilePath: ./deploy/render/Dockerfile" in manifest
    assert "LOCAL_EMBEDDING_BACKEND" in manifest
    assert "LOCAL_RERANKER_BACKEND" in manifest
    assert "EVICT_LOCAL_RERANKER_AFTER_USE" in manifest
    assert "value: fastembed" in manifest
    assert "healthCheckPath: /health" in manifest


def test_render_image_does_not_install_pytorch_stack():
    dockerfile = (ROOT / "deploy/render/Dockerfile").read_text()
    requirements = (ROOT / "deploy/render/requirements.txt").read_text()

    assert "requirements.txt" in dockerfile
    assert "fastembed" in requirements
    assert "pydantic-ai-slim" in requirements
    assert "python-multipart" in requirements
    assert "sentence-transformers" not in requirements
    assert "torch" not in requirements
    assert "uvicorn api.main:app" in dockerfile


def test_cloud_run_profile_reuses_the_lightweight_image_and_caps_scale():
    build = (ROOT / "deploy/cloudrun/cloudbuild.yaml").read_text()
    script = (ROOT / "deploy/cloudrun/deploy.sh").read_text()
    environment = (ROOT / "deploy/cloudrun/env.yaml").read_text()

    assert "deploy/render/Dockerfile" in build
    assert "--memory 1Gi" in script
    assert "--max 1" in script
    assert "--min 0" in script
    assert "--set-secrets" in script
    assert "EVICT_LOCAL_RERANKER_AFTER_USE" in environment


def test_pages_workflow_publishes_story_without_backend_dependency():
    workflow = (ROOT / ".github/workflows/deploy-journey.yml").read_text()

    assert "api/static/project-story" in workflow
    assert "actions/deploy-pages" in workflow
    assert "DEMO_BASE_URL" in workflow
