import asyncio
from api.main import app, SearchRequest
from fastapi.testclient import TestClient

with TestClient(app) as client:
    try:
        response = client.post("/search", json={"query": "machine learning engineer"})
        print(f"Status: {response.status_code}")
        print(response.text)
    except Exception as e:
        import traceback
        traceback.print_exc()
