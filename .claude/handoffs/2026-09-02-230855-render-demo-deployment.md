# Handoff: Live Demo Deployment on Render + Talent UI Routing

## Session Metadata
- Created: 2026-09-02 23:08:55
- Project: /Volumes/MAC/Projects_devolopment/hybrid search
- Branch: codex/free-demo-hosting
- Session duration: ~45m

### Recent Commits (for context)
  - 9b708b8 Add free Render demo deployment
  - bc29296 Document the full search endpoint pipeline
  - 250081d Fix hybrid RAG section alignment
  - e486286 Clarify hybrid RAG flow and demo calls to action
  - 83e8e3d Simplify journey introduction for broader audience

## Handoff Chain

- **Continues from**: [2026-06-09-190356-agent-copilot-improvements.md](./2026-06-09-190356-agent-copilot-improvements.md)
  - Previous title: Agent Copilot — UI Polish, Ranking Explanation Integration & Smart Suggestions
- **Supersedes**: None

## Current State Summary

The hybrid search project has been successfully deployed and is currently live on Render at `https://talent-atlas.onrender.com`. The deployment uses the Blueprint defined in `render.yaml` with the lightweight Docker container (`deploy/render/Dockerfile`) connecting to the 15k-candidate Supabase database and external LLM APIs (DeepSeek and Groq).

Both `/talent` (the recruiter copilot UI) and `/journey` (the project evolution journal) are active and returning HTTP 200. However, when visiting the root domain `https://talent-atlas.onrender.com`, FastAPI's root endpoint redirects to `/ui` (the admin search interface). The user noticed that only the search endpoint UI appeared initially and requested that the Talent UI be front and center.

## Important Context

- The live service URL is: `https://talent-atlas.onrender.com`.
- The Talent UI is currently served at `https://talent-atlas.onrender.com/talent`.
- The Admin UI is served at `https://talent-atlas.onrender.com/ui`.
- The Project Story is served at `https://talent-atlas.onrender.com/journey`.
- The reason the user said "only the search endpoint ui is there" is simply because the root `/` redirects to `/ui`. Pointing `/` to `/talent` will immediately solve this.
- Render is configured to auto-deploy changes pushed to branch `codex/free-demo-hosting`.

## Immediate Next Steps

1. **Update Root Route in `api/main.py`**: Change `root_ui_redirect()` from redirecting to `/ui` to redirecting to `/talent` (`RedirectResponse(url="/talent")`).
2. **Add Top Navigation Links**: In `api/static/talent.html` and `api/static/admin.html`, add clear links between Talent Atlas, Admin Search, and Project Story so visitors can seamlessly navigate all views.
3. **Commit and Push**: Stage and commit `api/main.py`, navigation updates, `.github/workflows/deploy-journey.yml`, and tests, then push to `origin/codex/free-demo-hosting`.
4. **Verify Redeployment**: Ensure Render automatically picks up the push and redeploys `https://talent-atlas.onrender.com`.

## Architecture Overview

- **Web Service & Hosting**: FastAPI hosted in a Docker container on Render Free Tier (`https://talent-atlas.onrender.com`).
- **Database**: Remote PostgreSQL + pgvector hosted on Supabase (15k candidates, ~362MB) connected via Supabase session pooler on port 5432.
- **Embedding & Reranker Engine**: Runs locally inside the container via `fastembed` (ONNX runtime for `all-MiniLM-L6-v2` embeddings and `ms-marco-MiniLM-L-6-v2` cross-encoder reranker). Models are pre-baked into the image to eliminate cold-start download latency. `EVICT_LOCAL_RERANKER_AFTER_USE=true` ensures peak memory stays below Render's 512MB RAM free tier ceiling.
- **LLM Integrations**: DeepSeek (`deepseek-chat`) for query planning / agent copilot, and Groq (`llama-3.1-8b-instant`) for fast insights.
- **Static Frontends**:
  - `/talent` (`api/static/talent.html` + `talent.js`, `talent.css`): Straatix Talent Atlas Recruiter Copilot UI.
  - `/ui` (`api/static/admin.html`): Hybrid Search admin and diagnostic interface.
  - `/journey` (`api/static/project-story/`): Static case study / project story.

## Critical Files

| File | Purpose | Relevance |
|------|---------|-----------|
| `api/main.py` | FastAPI application entrypoint | Controls routes including `/`, `/ui`, `/talent`, `/journey`, and search/agent endpoints |
| `render.yaml` | Render Blueprint configuration | Specifies Dockerfile, healthcheck (`/health`), and environment variable bindings |
| `deploy/render/Dockerfile` | Lightweight Python 3.11 container | Installs FastEmbed without PyTorch/CUDA, bakes ONNX models into container image |
| `deploy/render/requirements.txt` | Production dependencies for container | Pins minimal dependencies (`fastembed`, `pydantic-ai-slim`, etc.) |
| `api/static/talent.html` | Recruiter Talent Atlas UI | Main user-facing portfolio UI |
| `api/static/admin.html` | Search Admin UI | Search debug and database metrics dashboard |
| `.github/workflows/deploy-journey.yml` | GitHub Pages workflow | Optional decoupled static host for the project journey |

## Key Patterns Discovered

- The FastAPI app mounts static assets at `/static` using `_NoCacheStaticFiles` so browser clients don't cache stale JS/CSS.
- All embedding and reranking logic in `pipeline/embedder.py` and `pipeline/reranker.py` has a dedicated `fastembed` backend path that bypasses large PyTorch dependencies.
- Model eviction is triggered via `evict_reranker_if_needed()` in search to keep memory footprint lean.

## Work Completed

### Tasks Finished

- [x] Evaluated hosting options (Cloud Run vs. Render vs. Hugging Face Spaces) based on zero-card requirement and friction.
- [x] Identified and guided fixing the GitHub App permission blocking Render from accessing the private repository.
- [x] Retrieved required environment variable names and guided secure transfer of Supabase `DATABASE_URL`, `DEEPSEEK_API_KEY`, and `GROQ_API_KEY` into Render Blueprint.
- [x] Successfully deployed Blueprint service `talent-atlas` on Render.
- [x] Verified live service endpoints (`/health` HTTP 200, `/talent` HTTP 200, `/journey` HTTP 200).

## Files Modified

| File | Changes | Rationale |
|------|---------|-----------|
| `tests/test_render_deployment.py` | Added tests for cloud run and GitHub Pages workflow | Validates deployment manifests and workflow configurations |

## Decisions Made

| Decision | Options Considered | Rationale |
|----------|-------------------|-----------|
| Deploy on Render Free Tier | Google Cloud Run vs. Render Free Tier vs. HF Spaces | User preferred a truly free solution with no credit card requirement and minimal setup friction. |
| Keep database on Supabase Cloud | Local Docker DB vs. Supabase Free Tier | Supabase already contains the 15,000 candidate dataset and requires zero infrastructure maintenance. |
| Use FastEmbed ONNX runtime | PyTorch / sentence-transformers vs. FastEmbed | FastEmbed uses ~300MB RAM vs >1.5GB for PyTorch, allowing deployment on Render's 512MB free tier without OOM. |

## Assumptions Made

- The user wants the Talent Atlas recruiter copilot interface (`/talent`) to be the primary landing page for the deployment.
- The 3 secrets (`DATABASE_URL`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`) have already been configured in the Render Dashboard environment settings.

## Potential Gotchas

- Render free tier instances spin down after 15 minutes of inactivity. When asleep, the first request may take ~50 seconds to respond before the container warms up.
- Never commit the local `.env` file to git.

## Environment State

### Tools/Services Used

- Render (Web Service: `talent-atlas`, ID `srv-dac5rhn10e5c73bcf5d0`)
- Supabase (PostgreSQL + pgvector session pooler)
- GitHub (Repo `pylrn/hybrid-search`, branch `codex/free-demo-hosting`)

### Active Processes

- Render web service running `uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 1`

### Environment Variables

- `DATABASE_URL` (Set in Render secret env)
- `DEEPSEEK_API_KEY` (Set in Render secret env)
- `GROQ_API_KEY` (Set in Render secret env)
- `DB_POOL_MIN`, `DB_POOL_MAX`
- `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `LOCAL_EMBEDDING_BACKEND`
- `LOCAL_RERANKER_BACKEND`, `EVICT_LOCAL_RERANKER_AFTER_USE`, `RERANKER_MODEL`
- `LLM_PROVIDER`, `LLM_MODEL`, `FAST_LLM_PROVIDER`, `FAST_LLM_MODEL`, `QUALITY_LLM_PROVIDER`, `QUALITY_LLM_MODEL`, `INSIGHTS_LLM_PROVIDER`, `INSIGHTS_LLM_MODEL`

## Related Resources

- Render Dashboard: `https://dashboard.render.com/web/srv-dac5rhn10e5c73bcf5d0`
- Live Talent UI: `https://talent-atlas.onrender.com/talent`
- Live Journey Story: `https://talent-atlas.onrender.com/journey`
- Render Config: `render.yaml`
- Dockerfile: `deploy/render/Dockerfile`
