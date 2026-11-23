import os

os.environ["AGENTSCOPE_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

from app.main import app
from fastapi.testclient import TestClient

PAYLOAD = {
    "resourceSpans": [
        {
            "resource": {
                "attributes": [{"key": "service.name", "value": {"stringValue": "test-agent"}}]
            },
            "scopeSpans": [
                {
                    "spans": [
                        {
                            "traceId": "0af7651916cd43dd8448eb211c80319c",
                            "spanId": "b7ad6b7169203331",
                            "name": "research",
                            "kind": 1,
                            "startTimeUnixNano": "1700000000000000000",
                            "endTimeUnixNano": "1700000001000000000",
                            "attributes": [],
                        }
                    ]
                }
            ],
        }
    ]
}


def test_idempotent_otlp_ingestion() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/api/projects", json={"name": "Test", "service_identifier": "test"}
        ).json()
        headers = {"X-AgentScope-Key": project["ingestion_key"], "Content-Type": "application/json"}
        assert client.post("/v1/traces", json=PAYLOAD, headers=headers).status_code == 200
        assert client.post("/v1/traces", json=PAYLOAD, headers=headers).status_code == 200
        traces = client.get(f"/api/projects/{project['id']}/traces").json()["items"]
        assert len(traces) == 1
        detail = client.get(f"/api/projects/{project['id']}/traces/{traces[0]['trace_id']}").json()
        assert len(detail["spans"]) == 1
