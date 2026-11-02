import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from meety_ai.recording.schemas import SessionStart
from meety_ai.stt.context import generate_stt_context


# 테스트 이름: test_long_context_is_cut_at_line_boundary
# 목적: 800자를 넘는 생성 결과가 용어 중간에서 잘려 잘못된 표기가 들어가지 않도록 보호한다.
# 준비: teamId 없이 회의 정보만 있는 시작 메시지와 긴 응답을 내는 fake 모델.
# 기대 결과: 줄바꿈이 있으면 800자 안의 마지막 완전한 줄까지만, 없으면 800자로 자른다.
@pytest.mark.parametrize(
    ("generated", "expected"),
    [
        ("회의: 주간 회의\n용어: " + "가" * 900, "회의: 주간 회의"),
        ("가" * 900, "가" * 800),
    ],
)
async def test_long_context_is_cut_at_line_boundary(generated, expected):
    event = SessionStart.model_validate(
        {
            "type": "session.start",
            "requestId": "start-01",
            "meetingId": 1,
            "recordingSessionId": 1,
            "participantCount": 0,
            "participants": [],
            "title": "주간 회의",
        }
    )

    context = await generate_stt_context(
        event, model=FakeListChatModel(responses=[generated]), client=None
    )

    assert context == expected
