import asyncio
import json
from datetime import datetime, timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

import meety_ai.recording.router as router_module
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


def test_missing_provider_key_rejects_connection():
    app = create_live_app()
    app.state.settings.speechmatics_api_key = None

    with TestClient(app) as client:
        with pytest.raises(WebSocketDenialResponse) as error:
            with client.websocket_connect(WEBSOCKET_URL):
                pass

    assert error.value.status_code == 503
    assert error.value.json() == {"error": "service_unavailable"}
    assert app.state.recording_connections == 0


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


# #18: 녹음 종료 전 확정 전사 전달
# 테스트 이름: test_committed_transcript_arrives_before_stop
# 목적: 추가 입력이나 stop 없이도 공급자 결과가 Backend에 전달되는지 확인한다.
# 준비: 기존 fake_decoders를 재사용하고 Speechmatics 외부 통신만 대체한다.
# 준비: 공급자 대역은 전달받은 PCM을 기록하고, 입력 처리와 별개로 확정 결과를 내보낸다.
# 준비: 0.1~0.4초의 한 문장 "안녕하세요."에 해당하는 확정 결과를 사용한다.
# 준비: 공급자 연결 API와 monkeypatch 대상은 연동 코드의 경계를 정한 뒤 지정한다.
# 실행: start → ready → meta(0) → binary 전송 후 공급자 대역에서 결과를 발생시킨다.
# 실행: stop이나 추가 음성을 보내지 않은 상태에서 transcript.committed를 수신한다.
# 기대 결과: 공급자에는 압축 binary가 아닌 fake_decoders의 PCM이 전달된다.
# 기대 결과: meetingId와 recordingSessionId가 시작 요청과 일치한다.
# 기대 결과: payload는 sequenceNumber=0, content="안녕하세요.",
# 기대 결과: startedAtMs=100, endedAtMs=400이며 recognizedAt은 UTC 시각이다.
# 기대 결과: 화자 필드가 없고, 전사 수신 시점까지 session.ended가 오지 않는다.
# 실패 조건: stop 또는 다음 입력까지 결과가 보류되거나 PCM·ID·내용·시간이 잘못 전달된다.
# 정리: 성공·실패 모두 Backend 연결을 닫고 공급자 작업과 디코더를 정리한다.
# 주의: receive_json에는 자체 timeout이 없다. 실행 시 테스트 프로세스에 기한을 두고,
# 주의: 기한 초과 시 실패로 처리하고 프로세스를 종료한다. 스레드 대기만 중단하지 않는다.
# 핵심 API: TestClient.websocket_connect, send_json, send_bytes, receive_json, monkeypatch.
# 다음 한 단계: 실패를 확인한 뒤 녹음 세션과 STT 연결 수명주기를 연결한다.
def test_committed_transcript_arrives_before_stop(fake_decoders, fake_speechmatics_client):
    with TestClient(create_live_app()) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"

            ws.send_json(meta_message)
            ws.send_bytes(b"compressed audio")

            # STT 연결이 없다면 receive_json()에서 무한 대기하므로 먼저 배선을 확인한다.
            assert len(fake_speechmatics_client) == 1
            fake_speechmatics_client[0].emit_transcript()

            response = ws.receive_json()
            assert response["type"] == "transcript.committed"
            assert response["meetingId"] == "meeting-01"
            assert response["recordingSessionId"] == "recording-01"

            payload = response["payload"]
            assert payload["sequenceNumber"] == 0
            assert payload["content"] == "안녕하세요."
            assert payload["startedAtMs"] == 100
            assert payload["endedAtMs"] == 400
            assert "speaker" not in payload

            recognized_at = datetime.fromisoformat(payload["recognizedAt"].replace("Z", "+00:00"))
            assert recognized_at.utcoffset() == timedelta(0)

        stt_client = fake_speechmatics_client[0]
        assert stt_client.api_key == "test-api-key"
        assert stt_client.fed == [b"\0" * 16000]
        assert not stt_client.finished
        assert stt_client.closed


# #18: 입력 대기 중 STT 연결 단절
# 테스트 이름: test_provider_disconnect_before_next_audio
# 목적: 공급자 단절 후 들어온 다음 오디오가 처리되지 않도록 한다.
# 준비: 기존 fake_decoders와 fake_speechmatics_client를 재사용한다.
# 준비: 공급자 대역에 연결 단절을 발생시키는 동작을 추가한다. 호출자에게 즉시 raise하는
# 준비: 방식이 아니라, 실제 클라이언트와 같은 오류 전달 경로로 라우터에 단절을 알린다.
# 준비: on_error 콜백으로 공급자 단절을 알리고, 라우터가 이 콜백을 연결했는지 확인한다.
# 실행: start → ready를 받은 뒤 공급자 연결 단절을 발생시킨다.
# 실행: 수신 루프를 깨우기 위해 다음 audio.meta를 보내고 error 이벤트와 종료를 기다린다.
# 기대 결과: error.payload.code는 provider_unavailable, retryable은 true이다.
# 기대 결과: 특정 Backend 요청의 실패가 아니므로 requestId는 생략한다.
# 기대 결과: session.ended 없이 WebSocketDisconnect.code=1011로 연결이 종료된다.
# 기대 결과: 세션 처리 종료 후 공급자와 디코더가 닫히고 recording_connections가 0이다.
# 기대 결과: 세션 핸들러와 전사 전송 작업이 종료된다. SDK 내부 작업 정리는 별도 검증한다.
# 실패 조건: audio.meta가 세션에 반영되거나 다른 원인의 오류로 종료되거나,
# 실패 조건: 정상 종료 이벤트를 보내거나 연결·작업·연결 수가 정리되지 않으면 실패한다.
# 주의: 임의의 sleep 대신 준비 완료 응답과 종료 신호를 기준으로 검사한다.
# 주의: TestClient.receive_json에는 자체 timeout이 없으므로 테스트 프로세스에 10초
# 주의: 실행 기한을 둔다. 초과 시 실패로 처리하고 프로세스를 종료하여 무한 대기를 막는다.
# 정리: 성공·실패 모두 WebSocket과 TestClient를 닫는다. 작업 확인은 해당 세션 대상으로
# 정리: 한정하고 TestClient 자체 작업까지 모두 종료됐다고 검사하지 않는다.
# 핵심 API: TestClient.websocket_connect, receive_json, pytest.raises(WebSocketDisconnect).
# 다음 한 단계: SpeechmaticsClient에 on_error 경로를 만들고 라우터의 입력 대기와 연결한다.
def test_provider_disconnect_before_next_audio(
    fake_decoders, fake_speechmatics_client, monkeypatch
):
    app = create_live_app()
    session_finished = Event()
    sender_tasks = []
    original_sender = router_module.send_transcripts

    async def tracked_sender(*args, **kwargs):
        sender_tasks.append(asyncio.current_task())
        await original_sender(*args, **kwargs)

    async def observed_app(scope, receive, send):
        try:
            await app(scope, receive, send)
        finally:
            if scope["type"] == "websocket":
                session_finished.set()

    monkeypatch.setattr(router_module, "send_transcripts", tracked_sender)

    with TestClient(observed_app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"
            provider = fake_speechmatics_client[0]
            assert not provider.closed
            assert fake_decoders[0].started
            assert app.state.recording_connections == 1
            assert callable(provider.on_error), "라우터가 STT 오류 콜백을 연결해야 합니다."

            # 공급자 오류를 저장한 뒤 다음 입력으로 수신 대기를 깨운다.
            client.portal.call(provider.emit_disconnect)
            ws.send_json(meta_message)

            # 다음 입력은 수신 대기만 깨우며 세션에서 처리되면 안 된다.
            assert session_finished.wait(3), "공급자 단절 후 세션이 종료되지 않았습니다."
            response = ws.receive_json()
            assert response["type"] == "error"
            assert response["payload"]["code"] == "provider_unavailable"
            assert response["payload"]["retryable"] is True
            assert "requestId" not in response
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1011

            assert provider.closed
            assert not provider.finished
            assert provider.fed == []
            assert fake_decoders[0].fed == []
            assert fake_decoders[0].closed
            assert app.state.recording_connections == 0
            assert sender_tasks and all(task.done() for task in sender_tasks)


# 테스트 이름: test_final_transcript_precedes_session_ended
# 목적: 종료 직전의 전사와 문장부호 없는 잔여 문장이 유실되지 않도록 한다.
# 준비: 기존 공급자 대역의 finish 중 마지막 확정 단어를 전달하도록 구성한다.
# 실행: start → ready → stop 이후 Backend가 받는 이벤트 순서를 확인한다.
# 기대 결과: 마지막 transcript.committed → session.ended → close 1000 순서이며,
# 기대 결과: 세션 처리 종료 후 공급자·디코더가 닫히고 연결 수가 0이다.
# 실패 조건: 종료 응답이 전사보다 앞서거나 잔여 문장이 누락되면 실패한다.
def test_final_transcript_precedes_session_ended(fake_decoders, fake_speechmatics_client):
    app = create_live_app()
    with TestClient(app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            assert ws.receive_json()["type"] == "session.ready"

            provider = fake_speechmatics_client[0]

            async def finish_with_final_transcript():
                provider.on_transcript(
                    {
                        "message": "AddTranscript",
                        "results": [
                            {
                                "type": "word",
                                "start_time": 1.0,
                                "end_time": 1.5,
                                "alternatives": [{"content": "마지막", "speaker": "S1"}],
                            }
                        ],
                    }
                )
                provider.finished = True

            provider.finish = finish_with_final_transcript
            ws.send_json(stop_message)

            transcript = ws.receive_json()
            ended = ws.receive_json()
            assert transcript["type"] == "transcript.committed"
            assert transcript["payload"]["content"] == "마지막"
            assert ended["type"] == "session.ended"

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1000

        assert provider.finished
        assert provider.closed
        assert fake_decoders[0].closed
        assert app.state.recording_connections == 0


# 테스트 이름: test_stop_timeout_cleans_up_session
# 목적: 공급자가 종료에 응답하지 않아도 세션과 전송 작업이 남지 않도록 한다.
# 준비: 공급자 finish가 대기하도록 하고 STT_STOP_TIMEOUT을 짧게 설정한다.
# 실행: start → ready → stop 후 세션 핸들러의 종료를 기한 내 기다린다.
# 기대 결과: processing_timeout → close 1011이며 관련 작업·연결 수가 정리된다.
# 실패 조건: session.ended가 오거나 세션이 계속 대기하면 실패한다.
def test_stop_timeout_cleans_up_session(fake_decoders, fake_speechmatics_client, monkeypatch):
    app = create_live_app()
    monkeypatch.setattr(router_module, "STT_STOP_TIMEOUT", 0.01)

    with TestClient(app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            assert ws.receive_json()["type"] == "session.ready"

            provider = fake_speechmatics_client[0]

            async def never_finish():
                await asyncio.Event().wait()

            provider.finish = never_finish
            ws.send_json(stop_message)

            response = ws.receive_json()
            assert response["type"] == "error"
            assert response["requestId"] == "stop-01"
            assert response["payload"]["code"] == "processing_timeout"
            assert response["payload"]["retryable"] is False
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1011

        assert provider.closed
        assert fake_decoders[0].closed
        assert app.state.recording_connections == 0


# 테스트 이름: test_transcript_send_failure_stops_receiving
# 목적: 전송 작업의 실패 후 들어온 다음 오디오가 처리되지 않도록 한다.
# 준비: 전사 전송 경계에서 TimeoutError를 발생시킨다.
# 실행: start → ready → 전송 실패 확인 → 다음 audio.meta를 보낸다.
# 기대 결과: 공급자·디코더·세션 작업이 정리되고 연결 수가 0이다.
# 실패 조건: 다음 audio.meta가 처리되거나 클라이언트 연결 해제까지 정리가 보류되면 실패한다.
# 핵심 API: 기존 observed_app·Event 종료 관찰 방식, client.portal.call, monkeypatch.
# 다음 한 단계: 마지막 전사와 session.ended의 수신 순서를 검증하는 첫 사례부터 구현한다.
# 주의: 모든 사례는 프로세스 실행 기한을 두고 성공·실패 모두 TestClient를 닫는다.
def test_transcript_send_failure_stops_receiving(
    fake_decoders, fake_speechmatics_client, monkeypatch
):
    app = create_live_app()
    sender_failed = Event()

    async def failing_sender(*args, **kwargs):
        sender_failed.set()
        raise TimeoutError

    monkeypatch.setattr(router_module, "send_transcripts", failing_sender)

    with TestClient(app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            assert ws.receive_json()["type"] == "session.ready"
            assert sender_failed.wait(1), "전사 전송 작업이 시작되지 않았습니다."

            ws.send_json(meta_message)
            response = ws.receive_json()
            assert response["type"] == "error"
            assert response["payload"]["code"] == "processing_timeout"
            assert response["payload"]["retryable"] is False
            assert "requestId" not in response
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1011

        provider = fake_speechmatics_client[0]
        assert provider.closed
        assert fake_decoders[0].fed == []
        assert fake_decoders[0].closed
        assert app.state.recording_connections == 0
