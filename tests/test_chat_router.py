"""analysis 앱의 챗봇 HTTP 계약을 검증한다."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from meety_ai import analysis
from meety_ai.chat import router
from meety_ai.chat.schemas import ChatAnswer
from meety_ai.settings import AnalysisSettings


@pytest.fixture
def chat_payload():
    return {
        "teamId": 3,
        "meetingId": 42,
        "aiRequestId": 88,
        "question": "배포 일정은 언제인가요?",
        "conversationHistory": [{"role": "user", "content": "담당자도 정했나요?"}],
        "transcriptSegments": [
            {
                "segmentId": 0,
                "speakerDisplayName": None,
                "sequenceNumber": 0,
                "content": "금요일에 배포합니다.",
                "startedAtMs": 0,
                "endedAtMs": None,
            }
        ],
    }


@pytest.fixture
def chat_client(monkeypatch):
    generate = AsyncMock(
        return_value=ChatAnswer(
            answer="금요일입니다.",
            citations=[{"sourceType": "transcript", "meetingId": 42, "segmentId": 0}],
        )
    )
    monkeypatch.setattr(router, "generate_answer", generate)
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
    app = analysis.create_analysis_app()
    with TestClient(app) as client:
        yield client, generate


def test_chat_route_returns_answer_with_request_identifiers(chat_client, chat_payload):
    client, generate = chat_client

    response = client.post("/v1/chat", json=chat_payload)

    assert response.status_code == 200
    assert response.json() == {
        "aiRequestId": 88,
        "answer": "금요일입니다.",
        "citations": [{"sourceType": "transcript", "meetingId": 42, "segmentId": 0}],
    }
    generate.assert_awaited_once()
    call = generate.call_args
    payload = call.args[0] if call.args else call.kwargs["request"]
    assert payload.model_dump(mode="json", by_alias=True) == chat_payload
