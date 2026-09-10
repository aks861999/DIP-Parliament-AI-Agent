# evaluation

Offline/CI-only evaluation harness for testing the agent's behavior against mocked and live scenarios.

## What this service is responsible for

- **Mocked evaluation**: Runs the agent against a fixture stub (`stub_mcp_server.py`), no external API keys needed
- **Live API evaluation**: Hits the real DIP API to verify integration works
- **Scoring**: Measures tool-selection accuracy and grounding score

## Key components

| File | Responsibility |
|------|----------------|
| `evaluate_end_to_end.py` | Main evaluation runner for mocked scenarios |
| `evaluate_live_api.py` | Live DIP API smoke test |
| `stub_mcp_server.py` | Fixture MCP server that returns predetermined responses |
| `scenario_models.py` | Pydantic models for test scenarios |
| `scenarios_mocked.jsonl` | Mocked test scenarios (JSONL) |
| `scenarios_live.jsonl` | Live API test scenarios (JSONL) |

## Engineering Decisions

### Two-tier CI evaluation gate

The CI pipeline runs two separate evaluation jobs:

**1. `agent-eval-gate` (BLOCKING)**
- Runs against the mocked stub (`stub_mcp_server.py`)
- No DIP_API_KEY needed
- Tests agent behavior: tool selection + deterministic grounding
- Fails the build if accuracy falls below threshold

**2. `live-api-check` (NON-BLOCKING, continue-on-error)**
- Hits the real DIP API via the real `mcp_server.py`
- Needs DIP_API_KEY as a repo secret
- Fails ONLY on transport/HTTP errors, never on response content

**What this separation achieves:** "Did the agent's logic regress?" is decoupled from "Is the live upstream currently unreliable?" A DIP API outage shouldn't block merges, but a logic regression should.

**The tradeoff:** Two separate jobs to maintain. Worth it for the resilience gain.

---

### Stub MCP server allows deterministic testing

`stub_mcp_server.py` implements the same MCP tool interface but returns hardcoded responses:

```python
@mcp.tool(description="...")
async def get_person_info(name: str) -> PersonInfoResult:
    if "merkel" in name.lower():
        return PersonInfoResult(person={...}, ...)
    # ... other scenarios
```

**What this enables:** Unit tests and CI runs without needing a real DIP_API_KEY. The agent's reasoning logic can be tested in isolation.

**What the alternative would have been:** Run against the live API in CI. This would require a secrets injection and would fail when DIP is down, even if the agent logic is correct.

**The tradeoff:** The stub must be manually updated when tool contracts change. This is a minor maintenance cost.

---

### Scenario format: JSONL with structured expectations

Each scenario in `scenarios_mocked.jsonl` is one line:

```json
{"question": "Who is Angela Merkel?", "expected_tool": "get_person_info", "expected_args_contains": {"name": "Angela Merkel"}}
```

**What this enables:** Easy to add new test cases, just append a line. Easy to parse in bulk with standard JSON tools.

**What the alternative would have been:** JSON array or YAML. JSONL was chosen for simplicity and streaming-read compatibility.
