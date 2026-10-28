import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from meety_ai.live import create_live_app
from meety_ai.recording.schemas import SessionStart, client_event_adapter


# Backend의 정상 JSON이 올바른 시작 이벤트로 해석되는지 확인한다.
def test_valid_session_start():
    raw = """{
        "type": "session.start",
        "requestId": "req-1",
        "meetingId": 42,
        "recordingSessionId": 88,
        "participantCount": 3,
        "participants": [
            {"teamMemberId": 10, "name": "김지우"},
            {"teamMemberId": 11, "name": "박서준"},
            {"teamMemberId": null, "name": "외부 디자이너"}
        ]
    }"""

    event = client_event_adapter.validate_json(raw)

    assert isinstance(event, SessionStart)
    assert event.request_id == "req-1"
    assert event.meeting_id == 42
    assert event.recording_session_id == 88
    assert event.participant_count == 3
    assert [participant.team_member_id for participant in event.participants] == [10, 11, None]
    assert [participant.name for participant in event.participants] == [
        "김지우",
        "박서준",
        "외부 디자이너",
    ]


# 제거된 디코더 제어 메시지를 거부한다.
def test_invalid_decoder_reset():
    raw = """{
        "type": "decoder.reset",
        "requestId": "reset-1",
        "payload": {"audioFormat": "webm_opus"}
    }"""

    with pytest.raises(ValidationError) as error:
        client_event_adapter.validate_json(raw)

    assert error.value.errors()[0]["type"] == "union_tag_invalid"


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


# 이슈 #77용: 숫자 문자열 입력 호환과 정수 ID 응답을 검증한다.
@pytest.mark.parametrize("meeting_id, recording_session_id", [("42", "88"), (42, 88), (0, 0)])
def test_recording_accepts_backend_numeric_string_ids_and_returns_integer_ids(
    meeting_id, recording_session_id, fake_speechmatics_client
):
    WEBSOCKET_URL = "/v1/live-meeting"

    start_message = {
        "type": "session.start",
        "requestId": "start-01",
        "meetingId": "42",
        "recordingSessionId": "88",
        "participantCount": 3,
        "participants": [
            {"teamMemberId": 10, "name": "김지우"},
            {"teamMemberId": 11, "name": "박서준"},
            {"teamMemberId": None, "name": "외부 디자이너"},
        ],
    }

    meta_message = {"type": "audio.meta", "payload": {"sequence": 0}}
    stop_message = {"type": "session.stop", "requestId": "stop-01"}

    app = create_live_app()

    with TestClient(app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(
                {
                    **start_message,
                    "meetingId": meeting_id,
                    "recordingSessionId": recording_session_id,
                }
            )
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"
            assert ready["requestId"] == "start-01"

            ws.send_json(meta_message)
            ws.send_bytes(b"\x01\x00" * 8000)
            assert ws.receive_json()["type"] == "audio.ack"
            fake_speechmatics_client[0].emit_transcript()
            transcript = ws.receive_json()
            assert transcript["type"] == "transcript.committed"
            assert transcript["payload"]["content"] == "안녕하세요."

            ws.send_json(stop_message)
            ended = ws.receive_json()
            assert ended["type"] == "session.ended"
            assert ended["requestId"] == "stop-01"
            assert ended["payload"]["lastSequence"] == 0

            for response in (ready, transcript, ended):
                assert type(response["meetingId"]) is int
                assert type(response["recordingSessionId"]) is int
                assert response["meetingId"] == int(meeting_id)
                assert response["recordingSessionId"] == int(recording_session_id)

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1000

        assert app.state.recording_connections == 0
        assert fake_speechmatics_client[0].closed
        assert fake_speechmatics_client[0].finished


# 테스트 이름: test_session_start_rejects_embedding_with_overflowing_norm
# 목적: 성분은 유한하지만 노름이 무한대로 넘치는 등록 벡터가 세션 전체를 실패시키던 버그를 막는다.
# 준비: 모든 성분이 1e200인 192차원 등록 벡터를 가진 참가자 한 명.
# 실행: client_event_adapter로 session.start를 검증한다.
# 기대 결과: 입력 형식 오류(ValidationError)로 거절된다.
# 실패 조건: 검증을 통과해 Modal 화자 판정에서 TypeError로 세션이 끝난다.
def test_session_start_rejects_embedding_with_overflowing_norm():
    raw = json.dumps(
        {
            "type": "session.start",
            "requestId": "req-1",
            "meetingId": 42,
            "recordingSessionId": 88,
            "participantCount": 1,
            "participants": [
                {
                    "teamMemberId": 10,
                    "name": "김지우",
                    "speakerEmbedding": {"modelVersion": "v1", "embedding": [1e200] * 192},
                }
            ],
        }
    )

    with pytest.raises(ValidationError):
        client_event_adapter.validate_json(raw)
