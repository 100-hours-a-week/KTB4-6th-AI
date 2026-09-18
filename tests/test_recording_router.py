import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from meety_ai.live import create_live_app

WEBSOCKET_URL = "/v1/live-meeting"
CHUNK_SIZE = 16 * 1024

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
        with client.websocket_connect(WEBSOCKET_URL) as ws:
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
        with client.websocket_connect(WEBSOCKET_URL) as ws:
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
        with client.websocket_connect(WEBSOCKET_URL) as ws:
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

        with client.websocket_connect(WEBSOCKET_URL) as ws:
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


# #13: 두 연결의 격리와 개별 종료


# 테스트 이름: test_connections_are_isolated_on_disconnect
# 목적: 한 연결의 상태 변경·연결 해제가 다른 연결의 음성 처리에 영향을 주지 않는다.
# 준비: 기존 fake_decoders, 서로 다른 회의·녹음 ID와 구분 가능한 bytes를 가진 A·B.
# 실행: A·B 연결을 함께 열고 각각 start → ready → meta(0)/binary를 수행한다.
# 실행: A를 pause한 상태에서 B의 meta(1)/binary를 처리한다.
# 실행: A를 stop 없이 끊고, B에서 meta(2)/binary → stop → ended까지 진행한다.
# 기대 결과: A 정리 후에도 B는 입력을 처리하고 자기 ID와 lastSequence=2로 정상 종료한다.
# 기대 결과: 두 디코더의 입력 목록에 각 연결의 bytes만 순서대로 들어 있다.
# 기대 결과: A 연결 종료 시 A만 정리되고, B는 자기 종료 시 정리된다.
# 실패 조건: 상태·ID·sequence·오디오가 섞이거나 A 종료로 B 처리도 중단된다.
# 주의: A의 정리가 완료된 뒤 B를 검사하며, 정리 대기에는 실행 기한을 둔다.
# 핵심 API: TestClient.websocket_connect, send_json, send_bytes, receive_json.
def test_connections_are_isolated_on_disconnect(fake_decoders):
    with TestClient(create_live_app()) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws_b:
            with client.websocket_connect(WEBSOCKET_URL) as ws_a:
                ws_a.send_json(
                    {
                        "type": "session.start",
                        "requestId": "start-01",
                        "meetingId": "meeting-01",
                        "recordingSessionId": "recording-01",
                        "payload": {"audioFormat": "webm_opus"},
                    }
                )
                ready_a = ws_a.receive_json()
                assert ready_a == {
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

                ws_b.send_json(
                    {
                        "type": "session.start",
                        "requestId": "start-02",
                        "meetingId": "meeting-02",
                        "recordingSessionId": "recording-02",
                        "payload": {"audioFormat": "webm_opus"},
                    }
                )
                ready_b = ws_b.receive_json()
                assert ready_b == {
                    "type": "session.ready",
                    "requestId": "start-02",
                    "meetingId": "meeting-02",
                    "recordingSessionId": "recording-02",
                    "payload": {
                        "status": "READY",
                        "inputAudioFormat": "webm_opus",
                        "outputAudioFormat": "pcm_s16le",
                        "outputSampleRateHz": 16000,
                        "outputChannels": 1,
                    },
                }

                ws_a.send_json(meta_message)
                ws_b.send_json(meta_message)

                ws_a.send_bytes(b"chunk a")
                ws_b.send_bytes(b"chunk b")

                ws_a.send_json({"type": "session.pause", "requestId": "pause-01"})
                paused_a = ws_a.receive_json()
                assert paused_a == {
                    "type": "session.paused",
                    "requestId": "pause-01",
                    "payload": {"status": "PAUSED"},
                }

                ws_b.send_json({"type": "audio.meta", "payload": {"sequence": 1}})
                ws_b.send_bytes(b"chunk b1")

            # A는 stop 없이 끊긴 상태 (with문 바깥)
            decoder_b, decoder_a = fake_decoders
            assert decoder_a.fed == [b"chunk a"]
            assert decoder_a.closed
            assert not decoder_b.closed

            ws_b.send_json({"type": "audio.meta", "payload": {"sequence": 2}})
            ws_b.send_bytes(b"chunk b2")
            ws_b.send_json(stop_message)
            ended_b = ws_b.receive_json()
            assert ended_b == {
                "type": "session.ended",
                "requestId": "stop-01",
                "meetingId": "meeting-02",
                "recordingSessionId": "recording-02",
                "payload": {
                    "status": "ENDED",
                    "lastSequence": 2,
                    "audioDurationMs": 2000,
                },
            }

            with pytest.raises(WebSocketDisconnect) as error:
                ws_b.receive_json()
            assert error.value.code == 1000

        assert decoder_b.fed == [b"chunk b", b"chunk b1", b"chunk b2"]
        assert decoder_b.closed


# #10: 실제 ffmpeg까지 연결한 실시간 수신·변환 흐름


# 테스트 이름: test_live_meeting_with_real_ffmpeg
# 목적: WebSocket 수신 → 세션 → 실제 ffmpeg 디코딩 → 종료 응답까지의 실제 배선을 확인한다.
# 준비: fake_decoders를 받지 않아 실제 AudioDecoder를 사용한다.
# 준비: conftest.py의 audio_samples["webm_opus"] 입력(10초). ffmpeg가 없으면 skip.
# 실행: start → ready → 입력을 16KB씩 나눠 meta(n)/binary 반복 → stop → ended.
# 기대 결과: ended의 lastSequence가 마지막 청크 번호이고 audioDurationMs가 10000이다.
# 기대 결과: close 1000으로 종료된다.
# 실패 조건: 실제 디코더 호출 순서나 PCM 합산이 어긋나 오류·멈춤·잘못된 길이가 나온다.
# 주의: TestClient의 receive_json에는 기한이 없으므로 응답이 없는 메시지 뒤에 호출하지 않는다.
# 핵심 API: audio_samples, TestClient.websocket_connect, send_json, send_bytes, receive_json.
def test_live_meeting_with_real_ffmpeg(audio_samples):
    encoded, reference_pcm = audio_samples["webm_opus"]
    with TestClient(create_live_app()) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"

            chunks = [encoded[i : i + CHUNK_SIZE] for i in range(0, len(encoded), CHUNK_SIZE)]
            for i, chunk in enumerate(chunks):
                ws.send_json({"type": "audio.meta", "payload": {"sequence": i}})
                ws.send_bytes(chunk)

            ws.send_json(stop_message)
            ended = ws.receive_json()

            assert ended["payload"]["lastSequence"] == len(chunks) - 1
            assert ended["payload"]["audioDurationMs"] == len(reference_pcm) // 32

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1000
