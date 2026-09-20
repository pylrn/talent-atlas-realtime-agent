import asyncio
from api.main import app
from pipeline.search import HybridSearchEngine
from pipeline.database import get_pool
from pipeline.embedder import get_embedder

async def main():
    pool = await get_pool()
    embedder = get_embedder()
    engine = HybridSearchEngine(pool, embedder=embedder, reranker_cache={})
    
    # Run a test search
    print("Running smart search...")
    resp = await engine.smart_search(
        query="software engineer",
        recruiter_id="00000000-0000-0000-0000-000000000000"
    )
    print("Personalization applied:", getattr(resp, "personalization_applied", False))
    print("Found candidates:", len(resp.results))
    print("Done")

if __name__ == "__main__":
    asyncio.run(main())
