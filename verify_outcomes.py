import asyncio
import httpx
import uuid
import time

async def test_outcomes():
    # 1. Generate a random recruiter UUID
    recruiter_id = str(uuid.uuid4())
    print(f"Testing with Recruiter ID: {recruiter_id}")
    
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        # Wait for server to come up
        for _ in range(30):
            try:
                resp = await client.get("/admin/stats")
                if resp.status_code == 200:
                    break
            except Exception:
                pass
            print("Waiting for server...")
            await asyncio.sleep(1)

        # 2. Check the endpoint
        resp = await client.get(f"/outcomes?recruiter_id={recruiter_id}")
        print("Initial GET /outcomes:", resp.status_code, resp.json())
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

        # 3. Post an outcome
        resp2 = await client.post("/outcomes", json={
            "impression_id": str(uuid.uuid4()),
            "action": "shortlisted"
        })
        print("POST /outcomes with invalid impression_id:", resp2.status_code)
        assert resp2.status_code == 204
        
        print("Verification passed! Endpoint behaves correctly.")

if __name__ == "__main__":
    asyncio.run(test_outcomes())
