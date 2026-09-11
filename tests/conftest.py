"""
tests/conftest.py

Centralized dummy values for every env var that services/agent-service/main.py
and services/mcp-server/mcp_server.py require at MODULE IMPORT TIME via
_get_required_env(). Both modules now fail fast on missing config (by
design), which means any test that does `import main` or `import mcp_server`
needs all of these present, regardless of what that specific test is
actually verifying.

Centralizing here means adding a new _get_required_env() call to either
module only requires one new line here, not hunting down every test file
that happens to import it.

Real values already present in the environment (e.g. real REDIS_URL/
DATABASE_URL from a CI service container) are preserved; only variables
NOT already set get one of these dummies substituted.
"""
import os

import pytest

_REQUIRED_ENV_DEFAULTS = {
    # services/agent-service/main.py
    "LLM_API_KEY": "test-value",
    "LLM_MODEL": "test-model",
    "LLM_BASE_URL": "http://test.invalid/v1",
    "LANGFUSE_PUBLIC_KEY": "test-value",
    "LANGFUSE_SECRET_KEY": "test-value",
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "LLM_REQUESTS_PER_MINUTE": "100",
    "LLM_MAX_RETRIES": "3",
    "ENABLE_REFLECTION": "true",
    "ENABLE_HALLUCINATION_GUARD": "true",
    "MAX_REFLECTION_ITERATIONS": "3",
    "FAITHFULNESS_THRESHOLD": "0.8",
    "COMPLETENESS_THRESHOLD": "0.7",
    "AGENT_QUERY_TIMEOUT_SECONDS": "180",
    # services/mcp-server/mcp_server.py
    "LOG_LEVEL": "INFO",
    "DIP_API_BASE_URL": "https://search.dip.bundestag.de/api/v1",
    "DIP_API_KEY": "test-dip-key",
    "MCP_CACHE_TTL_SECONDS": "1800",
    "REDIS_URL": "redis://localhost:6379/1",
    "PREWARM_ON_STARTUP": "false",
    "NAME_DIRECTORY_TTL_SECONDS": "86400",
    "MCP_TRANSPORT": "stdio",
    "APP_SHARED_SECRET": "test-value"
}


@pytest.fixture(autouse=True)
def _default_required_env(monkeypatch):
    for key, value in _REQUIRED_ENV_DEFAULTS.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
