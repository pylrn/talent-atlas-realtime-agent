from fastapi.testclient import TestClient

from api.main import app


def main() -> None:
    """Run this manual API diagnostic with the application's lifespan active."""
    with TestClient(app) as client:
        response = client.post(
            "/agent/chat",
            json={
                "recruiter_id": "test-recruiter",
                "session_id": "test-session",
                "message": "Top data engineers with 5+ years exp",
                "context": {"query": "", "filters": {}, "result_ids": []},
            },
        )
    print("Status:", response.status_code)
    print("Body:", response.text)


if __name__ == "__main__":
    main()
