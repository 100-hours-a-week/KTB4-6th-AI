"""전역 AI 채팅의 핵심 사용자 흐름을 SSE 계약 수준에서 검증한다."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.chat_models import generate_from_stream
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from meety_ai import analysis
from meety_ai.settings import AnalysisSettings

DRAFT = {
    "title": "주간 회의",
    "purpose": "지난 결정 후속 논의",
    "note": None,
    "targetDurationMinutes": 30,
    "attendeeTeamMemberIds": [5],
    "customAttendeeNames": [],
    "scheduledAt": "2099-11-03T15:00:00",
}


class ScriptedModel(BaseChatModel):
    """실제 모델처럼 토큰 조각과 도구 호출 조각을 순서대로 스트리밍한다."""

    turns: list[list[AIMessageChunk]]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in self.turns.pop(0):
            generation = ChatGenerationChunk(message=chunk)
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=generation)
            yield generation

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return generate_from_stream(self._stream(messages, stop, run_manager, **kwargs))


def tool_call(name: str, args: dict, call_id: str) -> list[AIMessageChunk]:
    return [
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": name,
                    "args": json.dumps(args),
                    "id": call_id,
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )
    ]


def backend(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-Ai-Request-Id"] == "88"
    data = {
        "/internal/v1/qna/meetings/10/summaries": {
            "summary": {"summaryId": 1, "status": "COMPLETED", "content": "금요일 배포로 결정"}
        },
        "/internal/v1/assistant/team-members": {
            "members": [{"teamMemberId": 5, "displayName": "위글"}]
        },
    }[request.url.path]
    return httpx.Response(200, json={"success": True, "data": data, "error": None})


def parse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        analysis,
        "AnalysisSettings",
        lambda: AnalysisSettings(
            _env_file=None,
            OPENROUTER_API_KEY="test-key",
            INTERNAL_API_KEY="test-internal-key",
            MODAL_TOKEN_ID="test-modal-id",
            MODAL_TOKEN_SECRET="test-modal-secret",
        ),
    )
    monkeypatch.setattr(analysis, "load_dotenv", lambda: None)
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-modal-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "test-modal-secret")
    with TestClient(analysis.create_analysis_app()) as test_client:
        yield test_client


def test_question_reads_meeting_then_streams_answer_with_draft(client):
    # 조회·초안 도구를 거친 답변이 계약대로 스트리밍되고 완료되는 흐름을 보호한다.
    client.app.state.assistant_model = ScriptedModel(
        turns=[
            tool_call("get_meeting_summary", {"meeting_id": 10}, "1"),
            tool_call("propose_meeting_draft", {"draft": DRAFT}, "2"),
            [
                AIMessageChunk(content="지난 회의 후속으로 "),
                AIMessageChunk(content="주간 회의를 만들까요?"),
            ],
        ]
    )
    client.app.state.backend_client = httpx.AsyncClient(
        transport=httpx.MockTransport(backend), base_url="http://backend"
    )

    response = client.post(
        "/v1/assistant/chat",
        json={
            "aiRequestId": 88,
            "teamId": 1,
            "requesterTeamMemberId": 5,
            "question": "지난 회의 후속 회의 잡아줘",
            "recentMessages": [],
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_events(response.text)
    phases = [data["phase"] for name, data in events if name == "status"]
    assert phases == ["READING_MEETING", "PREPARING_MEETING"]
    delta_texts = [data["text"] for name, data in events if name == "delta"]
    assert delta_texts == ["지난 회의 후속으로 ", "주간 회의를 만들까요?"]
    deltas = "".join(delta_texts)
    assert events[-1] == (
        "completed",
        {
            "aiRequestId": 88,
            "answer": deltas,
            "referencedMeetingIds": [10],
            "meetingDraft": DRAFT,
        },
    )
