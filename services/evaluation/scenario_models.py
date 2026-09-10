from pydantic import BaseModel


class MockedScenario(BaseModel):
    question: str
    expected_tool: str | None = None
    expected_args_contains: dict | None = None


class LiveScenario(BaseModel):
    question: str
