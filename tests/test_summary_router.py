from types import SimpleNamespace
from unittest.mock import AsyncMock

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


# 테스트 이름: test_summary_route_uses_segment_display_names
# 목적: Backend가 세그먼트별로 보낸 화자 표시 이름을 전사 프롬프트의 화자 이름으로 쓰는지 보호한다.
# 준비: speakers는 Backend처럼 빈 목록으로 둔다.
# 준비: 세그먼트는 이름 있음·이름 null·화자 미확인 순으로 준비한다.
# 준비: app.state.summary_chain을 "# 요약"을 반환하는 AsyncMock으로 대체한다.
# 실행: TestClient로 POST /v1/summary에 요청을 전달한다.
# 기대 결과: 200이며 체인 입력 transcript가 "홍길동", "화자 2", "미확인 화자" 순으로 표시한다.
# 실패 조건: displayName을 거부하거나 무시하고 speakers 목록에서 이름을 찾으면 실패해야 한다.


def test_summary_route_uses_segment_display_names():
    app = create_app("analysis", Settings(_env_file=None))
    invoke = AsyncMock(return_value="# 요약")
    app.state.summary_chain = SimpleNamespace(ainvoke=invoke)
    app.include_router(summary_router)
    segment = {"sequenceNumber": 0, "startedAtMs": 0, "endedAtMs": 1000}
    payload = {
        "requestId": "req-1",
        "meetingId": 42,
        "title": "주간 회의",
        "purpose": "",
        "note": "",
        "meetingStartedAt": "2026-09-23T10:00:00+09:00",
        "speakers": [],
        "segments": [
            {**segment, "segmentId": 1, "speakerId": 1, "displayName": "홍길동", "content": "A"},
            {**segment, "segmentId": 2, "speakerId": 2, "displayName": None, "content": "B"},
            {**segment, "segmentId": 3, "speakerId": None, "displayName": None, "content": "C"},
        ],
    }

    with TestClient(app) as client:
        response = client.post("/v1/summary", json=payload)

    assert response.status_code == 200
    transcript = invoke.await_args.args[0]["transcript"]
    assert transcript == "홍길동: A\n화자 2: B\n미확인 화자: C"
