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
        "recordingSessionId": 88
    }"""

    event = client_event_adapter.validate_json(raw)

    assert isinstance(event, SessionStart)
    assert event.request_id == "req-1"
    assert event.meeting_id == 42
    assert event.recording_session_id == 88


# 지원하지 않는 녹음 형식의 스트림 시작 요청이 시스템 안으로 들어오는 것을 막는다.
def test_invalid_decoder_reset():
    raw = """{
        "type": "decoder.reset",
        "requestId": "reset-1",
        "payload": {"audioFormat": "mp3"}
    }"""

    with pytest.raises(ValidationError) as error:
        client_event_adapter.validate_json(raw)

    assert error.value.errors()[0]["loc"] == ("decoder.reset", "payload", "audioFormat")


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
    meeting_id, recording_session_id, fake_decoders, fake_speechmatics_client
):
    WEBSOCKET_URL = "/v1/live-meeting"

    start_message = {
        "type": "session.start",
        "requestId": "start-01",
        "meetingId": "42",
        "recordingSessionId": "88",
    }

    reset_message = {
        "type": "decoder.reset",
        "requestId": "reset-01",
        "payload": {"audioFormat": "webm_opus"},
    }

    WEBM_HEAD = b"\x1a\x45\xdf\xa3"

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

            ws.send_json(reset_message)
            assert ws.receive_json()["type"] == "decoder.ready"
            ws.send_json(meta_message)
            ws.send_bytes(WEBM_HEAD + b"compressed audio")
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
        assert len(fake_decoders) == 1
        assert fake_decoders[0].closed
        assert fake_speechmatics_client[0].closed
        assert fake_speechmatics_client[0].finished
