import pytest

from meety_ai.recording.schemas import SessionStart
from meety_ai.stt.context import build_stt_context


# 테스트 이름: test_context_reserves_space_for_whole_terms
# 목적: 긴 회의 정보에도 용어 공간을 확보하고, 용어 중간 잘림과 중복을 막는다.
# 준비: 짧거나 900자인 제목과, 중복 용어 및 상한보다 긴 용어.
# 실행: 회의 정보와 용어를 하나의 STT 맥락으로 조합한다.
# 기대 결과: 전체가 800자 이내이고 완전한 용어 두 개만 한 번씩 들어간다.
# 실패 조건: 긴 제목 때문에 용어가 사라지거나 용어가 중간에서 잘린다.
@pytest.mark.parametrize("title", ["주간 회의", "가" * 900])
def test_context_reserves_space_for_whole_terms(title):
    event = SessionStart.model_validate(
        {
            "type": "session.start",
            "requestId": "start-01",
            "meetingId": 1,
            "recordingSessionId": 1,
            "participantCount": 0,
            "participants": [],
            "title": title,
        }
    )

    context = build_stt_context(event, ["스레드", "HikariCP", "스레드", "나" * 801])

    assert len(context) <= 800
    assert context.startswith("회의 제목: ")
    assert context.endswith("\n용어: 스레드, HikariCP")
    assert "나" not in context
