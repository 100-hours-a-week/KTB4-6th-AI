"""WebSocket 라우터 통합 테스트 작성 계획.

실제 앱·라우터·스키마·세션을 사용하고 AudioDecoder만 대체한다.
"""

# 공통 준비
# - TestClient(create_live_app())로 실제 앱에 연결한다.
# - session 모듈의 AudioDecoder를 작은 가짜 디코더로 대체한다.
# - 가짜 디코더는 입력 청크·정리 여부를 기록하고 PCM 출력·실패를 제어한다.
# - 정상 start와 audio.meta 메시지만 공통 자료로 둔다.
# - close code는 receive_json()에서 발생하는 WebSocketDisconnect로 확인한다.
# - 검증 실패 시에도 클라이언트 컨텍스트를 종료해 연결을 정리한다.
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import meety_ai.recording.session as session_module
from meety_ai.live import create_live_app


@pytest.fixture
def fake_decoders(monkeypatch):
    instances = []

    class FakeDecoder:
        def __init__(self, on_pcm):
            self.on_pcm = on_pcm
            self.fed = []
            self.closed = False
            self.started = False
            instances.append(self)

        async def start(self):
            self.started = True

        async def feed(self, chunk):
            self.fed.append(chunk)
            await self.on_pcm(b"\0" * 16000)

        async def finish(self):
            # 종료 시 남은 PCM까지 합산되는지 확인한다.
            await self.on_pcm(b"\0" * 16000)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(session_module, "AudioDecoder", FakeDecoder)

    return instances


start_message = {
    "type": "session.start",
    "requestId": "start-01",
    "meetingId": "meeting-01",
    "recordingSessionId": "recording-01",
    "payload": {"audioFormat": "webm_opus"},
}

meta_message = {"type": "audio.meta", "payload": {"sequence": 0}}
stop_message = {"type": "session.stop", "requestId": "stop-01"}

# 1. test_recording_flow
# 목적: JSON·binary 수신부터 정상 응답·종료까지 실제 배선을 검증한다.
# 실행: start → ready → meta(0) → binary → stop → ended.
# 기대: ready의 요청 ID와 오디오 규격이 맞고 디코더에 원본 binary가 전달된다.
# 기대: ended의 lastSequence·audioDurationMs가 출력 PCM에 맞고 camelCase로 전송된다.
# 기대: close 1000 이후 디코더가 정리된다.


def test_recording_flow(fake_decoders):
    with TestClient(create_live_app()) as client:
        with client.websocket_connect("/v1/live-meeting") as ws:
            ws.send_json(start_message)
            ready = ws.receive_json()
            assert ready == {
                "type": "session.ready",
                "requestId": "start-01",
                "meetingId": "meeting-01",
                "recordingSessionId": "recording-01",
                "payload": {
                    "status": "READY",
                    "inputAudioFormat": "webm_opus",
                    "outputAudioFormat": "pcm_s16le",
                    "outputSampleRateHz": 16000,
                    "outputChannels": 1,
                },
            }

            ws.send_json(meta_message)
            ws.send_bytes(b"audio chunk")
            ws.send_json(stop_message)
            ended = ws.receive_json()
            assert ended == {
                "type": "session.ended",
                "requestId": "stop-01",
                "meetingId": "meeting-01",
                "recordingSessionId": "recording-01",
                "payload": {
                    "status": "ENDED",
                    "lastSequence": 0,
                    "audioDurationMs": 1000,
                },
            }

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1000

        assert len(fake_decoders) == 1
        assert fake_decoders[0].fed == [b"audio chunk"]
        assert fake_decoders[0].closed


# 2. test_invalid_json
# 목적: 잘못된 메시지가 세션 처리로 넘어가지 않도록 한다.
# 사례: 깨진 JSON, 미지원 이벤트, 잘못된 payload를 parametrize로 묶는다.
# 실행: 연결 후 잘못된 text를 전송한다.
# 기대: invalid_message 오류 → close 1008, 디코더 시작·입력 없이 정리된다.
# 기대: 식별할 수 없는 요청의 requestId는 생략된다.
# 주의: 필드별 상세 조합은 test_recording_schemas.py에서 검증한다.


@pytest.mark.parametrize(
    "bad_message",
    [
        pytest.param("{", id="malformed-json"),
        pytest.param('{"type": "unknown"}', id="unknown-event"),
        pytest.param(
            json.dumps({**start_message, "payload": {"audioFormat": "mp3"}}),
            id="invalid-payload",
        ),
    ],
)
def test_invalid_json(bad_message, fake_decoders):
    with TestClient(create_live_app()) as client:
        with client.websocket_connect("/v1/live-meeting") as ws:
            ws.send_text(bad_message)
            response = ws.receive_json()
            assert response["type"] == "error"
            assert response["payload"]["code"] == "invalid_message"
            assert response["payload"]["retryable"] is False
            assert "requestId" not in response

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1008

        assert len(fake_decoders) == 1
        assert not fake_decoders[0].started
        assert fake_decoders[0].fed == []
        assert fake_decoders[0].closed


# 3. test_audio_message_order
# 목적: 세션의 순서 오류가 WebSocket 오류 응답으로 전달되는지 확인한다.
# 준비: 정상적으로 시작한 세션.
# 사례: meta 없이 binary, meta 뒤 binary 대신 stop.
# 기대: unexpected_binary 또는 expected_binary → close 1008.
# 기대: 거부된 입력은 디코더에 전달되지 않으며 stop 오류에는 해당 requestId가 담긴다.
# 주의: 상세 상태 전이·sequence 연속성 조합은 세션 테스트에서 검증한다.


def test_audio_message_order(fake_decoders):
    with TestClient(create_live_app()) as client:
        with client.websocket_connect("/v1/live-meeting") as ws:
            ws.send_json(start_message)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"
            ws.send_bytes(b"audio chunk")

            response = ws.receive_json()
            assert response["type"] == "error"
            assert response["payload"]["code"] == "unexpected_binary"
            assert "requestId" not in response

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1008

        with client.websocket_connect("/v1/live-meeting") as ws:
            ws.send_json(start_message)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"
            ws.send_json(meta_message)
            ws.send_json(stop_message)
            response = ws.receive_json()

            assert response["type"] == "error"
            assert response["payload"]["code"] == "expected_binary"
            assert response["requestId"] == stop_message["requestId"]

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1008


# 4. test_message_size_limit
# 목적: text와 binary의 크기 상한 및 바이트 단위를 보호한다.
# 준비: text는 유효한 JSON과 공백으로 크기를 조절하고 다중 바이트 문자도 포함한다.
# 준비: binary는 start → meta 이후 전달한다.
# 사례: 각 상한과 정확히 같은 크기, 상한보다 1 byte 큰 크기.
# 기대: 상한 이내는 처리되고 초과는 message_too_large → close 1009.
# 기대: 초과 binary는 디코더에 전달되지 않는다.


# 5. test_decoder_failure
# 목적: 디코더 실패를 안전한 오류 응답과 자원 정리로 연결한다.
# 준비: feed에서 내부 상세 메시지를 가진 AudioDecodeError를 발생시키는 디코더.
# 실행: start → meta → binary.
# 기대: audio_decode_failed → close 1011, retryable은 false.
# 기대: 내부 오류 원문을 응답에 노출하지 않고 디코더를 정리한다.


# 6. test_disconnect_cleanup
# 목적: stop 없이 Backend 연결이 끊겨도 자원과 연결 슬롯을 반환한다.
# 준비: 연결 상한을 1로 설정하고 첫 연결에서 start → ready까지 진행한다.
# 실행: 첫 연결을 종료한 뒤 새 연결로 start → ready를 수행한다.
# 기대: 첫 디코더가 정리되고 새 연결이 허용된다.


# 7. test_connection_limit
# 목적: 한도 초과 연결을 수락하지 않고 기존 연결은 유지한다.
# 준비: 연결 상한을 1로 설정하고 첫 연결을 유지한다.
# 실행: 두 번째 연결을 시도한 뒤 첫 연결을 정상 종료하고 다시 연결한다.
# 기대: 두 번째 연결은 HTTP 429로 거절되고 디코더를 생성하지 않는다.
# 기대: 기존 연결은 정상 종료할 수 있고 종료 후 신규 연결이 허용된다.
# 도구: WebSocketDenialResponse로 HTTP 상태와 오류 본문을 확인한다.


# 8. test_cleanup_failure_releases_slot
# 목적: session.close() 실패에도 연결 슬롯 반환이 실행되는지 보호한다.
# 준비: 연결 상한은 1, 첫 디코더의 close만 실패하도록 설정한다.
# 실행: 첫 연결을 종료하고 예상된 정리 예외를 확인한 뒤 다시 연결한다.
# 기대: 새 연결이 허용된다. 카운터의 내부 값보다 외부 동작으로 확인한다.


# 9. test_binary_wait_timeout — 대기 제한 구현 후 작성
# 목적: meta만 보내고 binary를 보내지 않는 연결이 무기한 자원을 점유하지 않게 한다.
# 준비: 실제 라우터의 binary 대기 제한을 테스트용 짧은 값으로 바꾼다.
# 실행: start → meta 이후 binary를 보내지 않는다.
# 기대: processing_timeout → close 1011, 이전 요청 ID가 붙지 않고 자원이 정리된다.
# 주의: TimeoutError를 직접 발생시키면 실제 대기 제한을 검증할 수 없다.
# 주의: 제한이 제거되는 회귀에서도 테스트가 멈추지 않도록 별도의 실행 기한을 둔다.
