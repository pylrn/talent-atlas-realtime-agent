import asyncio
from fastapi.testclient import TestClient
from api.main import app

with TestClient(app) as client:
    response = client.post("/agent/chat", json={
        "recruiter_id": "test-recruiter",
        "session_id": "test-session",
        "message": "Top data engineers with 5+ years exp",
        "context": {"query":"","filters":{},"result_ids":[]}
    })
    print("Status:", response.status_code)
    print("Body:", response.text)
