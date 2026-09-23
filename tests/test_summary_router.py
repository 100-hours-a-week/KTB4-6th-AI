from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from meety_ai.app import create_app
from meety_ai.settings import Settings
from meety_ai.summary.chain import create_summary_chain
from meety_ai.summary.router import summary_router


def test_summary_route_returns_markdown():
    app = create_app("analysis", Settings(_env_file=None))
    app.state.summary_chain = create_summary_chain(FakeListChatModel(responses=["# 요약"]))
    app.include_router(summary_router)
    payload = {
        "requestId": "req-1",
        "meetingId": 42,
        "title": "주간 회의",
        "purpose": "진행 상황 공유",
        "note": "다음 일정 확인",
        "meetingStartedAt": "2026-09-23T10:00:00+09:00",
        "speakers": [{"speakerId": 1, "teamMemberId": 7, "displayName": "홍길동"}],
        "segments": [
            {
                "segmentId": 1,
                "speakerId": 1,
                "sequenceNumber": 0,
                "content": "배포 일정을 금요일로 확정했습니다.",
                "startedAtMs": 0,
                "endedAtMs": 1800,
            }
        ],
    }

    with TestClient(app) as client:
        response = client.post("/v1/summary", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text == "# 요약"
