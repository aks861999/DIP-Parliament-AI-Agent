import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "services", "agent-service"))

import pytest
from fastapi.testclient import TestClient


@pytest.mark.unit
def test_health_before_startup_reports_starting(monkeypatch):


    import main as agent_service_main

    agent_service_main.state["graph"] = None
    client = TestClient(agent_service_main.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "starting"}


@pytest.mark.unit
def test_query_returns_503_when_agent_not_ready(monkeypatch):


    import main as agent_service_main

    agent_service_main.state["graph"] = None
    client = TestClient(agent_service_main.app)
    response = client.post("/query", json={"query": "who is Angela Merkel?", "thread_id": "t1"})
    assert response.status_code == 503
