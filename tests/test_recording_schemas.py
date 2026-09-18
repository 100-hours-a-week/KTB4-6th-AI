import json

import pytest
from pydantic import ValidationError

from meety_ai.recording.schemas import SessionStart, client_event_adapter


# Backend의 정상 JSON이 올바른 시작 이벤트로 해석되는지 확인한다.
def test_valid_session_start():
    raw = """{
        "type": "session.start",
        "requestId": "req-1",
        "meetingId": "meeting-42",
        "recordingSessionId": "recording-88",
        "payload": {"audioFormat": "webm_opus"}
    }"""

    event = client_event_adapter.validate_json(raw)

    assert isinstance(event, SessionStart)
    assert event.request_id == "req-1"
    assert event.meeting_id == "meeting-42"
    assert event.recording_session_id == "recording-88"


# 잘못된 시작 요청이 시스템 안으로 들어오는 것을 막는다.
def test_invalid_session_start():
    raw = """{
        "type": "session.start",
        "requestId": "req-1",
        "meetingId": "meeting-42",
        "recordingSessionId": "recording-88",
        "payload": {"audioFormat": "mp3"}
    }"""

    with pytest.raises(ValidationError):
        client_event_adapter.validate_json(raw)


# sequence의 정수·범위 계약을 보호한다.
@pytest.mark.parametrize("bad_sequence", [-1, "0", True])
def test_invalid_sequence_type_or_range(bad_sequence):
    raw = json.dumps(
        {
            "type": "audio.meta",
            "payload": {
                "sequence": bad_sequence,
            },
        }
    )

    with pytest.raises(ValidationError) as error:
        client_event_adapter.validate_json(raw)

    assert error.value.errors()[0]["loc"] == ("audio.meta", "payload", "sequence")
