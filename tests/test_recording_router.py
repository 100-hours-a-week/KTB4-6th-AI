import asyncio
import json
from datetime import datetime, timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

import meety_ai.recording.connection as connection_module
from meety_ai.live import create_live_app
from meety_ai.stt.schemas import STTTranscript

WEBSOCKET_URL = "/v1/live-meeting"
CHUNK_SIZE = 16 * 1024

start_message = {
    "type": "session.start",
    "requestId": "start-01",
    "meetingId": 1,
    "recordingSessionId": 1,
    "participantCount": 0,
    "participants": [],
}

meta_message = {"type": "audio.meta", "payload": {"sequence": 0}}
stop_message = {"type": "session.stop", "requestId": "stop-01"}


def test_recording_connection_limit_from_environment(monkeypatch, fake_speechmatics_client):
    monkeypatch.setenv("MEETY_MAX_RECORDING_CONNECTIONS", "1")
    app = create_live_app()

    with TestClient(app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            assert ws.receive_json()["type"] == "session.ready"

            with pytest.raises(WebSocketDenialResponse) as error:
                with client.websocket_connect(WEBSOCKET_URL):
                    pytest.fail("상한을 초과한 연결이 허용됐습니다.")

            assert error.value.status_code == 429
            assert error.value.json() == {"error": "capacity_exceeded"}
            assert app.state.recording_connections == 1
            assert len(fake_speechmatics_client) == 1

        assert app.state.recording_connections == 0
        assert fake_speechmatics_client[0].closed

        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            assert ws.receive_json()["type"] == "session.ready"
            assert app.state.recording_connections == 1

        assert app.state.recording_connections == 0
        assert fake_speechmatics_client[1].closed


def test_missing_provider_key_prevents_live_app_start(monkeypatch, tmp_path):
    monkeypatch.delenv("MODAL_PROXY_TOKEN_SECRET")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as error:
        create_live_app()

    assert error.value.errors(include_input=False)[0]["loc"] == ("MODAL_PROXY_TOKEN_SECRET",)


# 테스트 이름: test_session_start_accepts_korean_meeting_context
# 목적: 허용 길이의 한글 회의 문맥이 기존 4KB 제한으로 거부되는 회귀를 막는다.
# 준비: 제목·목적·비고가 각각 900자인 시작 메시지와 기존 공급자 대역.
# 실행: 실제 WebSocket으로 시작 메시지를 보내고 정상 종료한다.
# 기대 결과: session.ready와 session.ended를 받고 공급자와 연결이 정리된다.
# 실패 조건: 스키마가 허용한 문맥 때문에 녹음을 시작하지 못한다.
def test_session_start_accepts_korean_meeting_context(fake_speechmatics_client):
    app = create_live_app()
    with TestClient(app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(
                {**start_message, "title": "가" * 900, "purpose": "나" * 900, "notes": "다" * 900}
            )
            assert ws.receive_json()["type"] == "session.ready"
            ws.send_json(stop_message)
            assert ws.receive_json()["type"] == "session.ended"
        assert fake_speechmatics_client[0].finished
        assert fake_speechmatics_client[0].closed
        assert app.state.recording_connections == 0


def test_recording_flow(fake_speechmatics_client):
    with TestClient(create_live_app()) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            ready = ws.receive_json()
            assert ready == {
                "type": "session.ready",
                "requestId": "start-01",
                "meetingId": 1,
                "recordingSessionId": 1,
                "payload": {
                    "status": "READY",
                    "outputAudioFormat": "pcm_s16le",
                    "outputSampleRateHz": 16000,
                    "outputChannels": 1,
                },
            }

            ws.send_json(meta_message)
            ws.send_bytes(b"\x01\x00" * 16000)
            assert ws.receive_json()["type"] == "audio.ack"
            ws.send_json(stop_message)
            ended = ws.receive_json()
            assert ended == {
                "type": "session.ended",
                "requestId": "stop-01",
                "meetingId": 1,
                "recordingSessionId": 1,
                "payload": {
                    "status": "ENDED",
                    "lastSequence": 0,
                    "audioDurationMs": 1000,
                },
            }

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1000

        assert len(fake_speechmatics_client) == 1
        assert fake_speechmatics_client[0].fed == [b"\x01\x00" * 16000]
        assert fake_speechmatics_client[0].closed


# 2. test_invalid_json
# 목적: 잘못된 메시지가 세션 처리로 넘어가지 않도록 한다.
# 사례: 깨진 JSON, 미지원 이벤트, 잘못된 payload를 parametrize로 묶는다.
# 실행: 연결 후 잘못된 text를 전송한다.
# 기대: invalid_message 오류 → close 1008, 공급자 시작·입력 없이 정리된다.
# 기대: 식별할 수 없는 요청의 requestId는 생략된다.
# 주의: 필드별 상세 조합은 test_recording_schemas.py에서 검증한다.
@pytest.mark.parametrize(
    "bad_message",
    [
        pytest.param("{", id="malformed-json"),
        pytest.param('{"type": "unknown"}', id="unknown-event"),
        pytest.param(
            json.dumps({"type": "audio.meta", "payload": {"sequence": -1}}),
            id="invalid-payload",
        ),
    ],
)
def test_invalid_json(bad_message, fake_speechmatics_client):
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

        assert fake_speechmatics_client[0].fed == []


# 테스트 이름: test_audio_message_order
# 목적: 순서가 잘못되거나 샘플 경계가 깨진 입력은 STT로 전달하지 않는다.
# 준비: 정상적으로 시작한 세션과 가짜 STT 클라이언트.
# 실행: 사례별로 meta 없이 binary, meta 뒤 stop, 잘못된 PCM 청크를 전송한다.
# 기대 결과: 해당 오류와 종료 코드를 반환하고 STT 입력은 없다.
# 실패 조건: 거부된 청크가 STT로 전달되거나 잘못된 코드로 종료된다.
@pytest.mark.parametrize(
    "case, code, close_code",
    [
        ("unexpected_binary", "unexpected_binary", 1008),
        ("expected_binary", "expected_binary", 1008),
        ("odd_pcm", "invalid_message", 1008),
        ("empty_pcm", "invalid_message", 1008),
        ("oversized_pcm", "message_too_large", 1009),
    ],
)
def test_audio_message_order(case, code, close_code, fake_speechmatics_client):
    with TestClient(create_live_app()) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            assert ws.receive_json()["type"] == "session.ready"
            if case != "unexpected_binary":
                ws.send_json(meta_message)
            if case == "expected_binary":
                ws.send_json(stop_message)
            else:
                chunk = b"\x00\x00"
                if case == "odd_pcm":
                    chunk = b"\x00"
                elif case == "empty_pcm":
                    chunk = b""
                elif case == "oversized_pcm":
                    chunk = b"\x00\x00" * (128 * 1024 + 1)
                ws.send_bytes(chunk)

            response = ws.receive_json()
            assert response["type"] == "error"
            assert response["payload"]["code"] == code
            if case == "expected_binary":
                assert response["requestId"] == stop_message["requestId"]
            else:
                assert "requestId" not in response
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == close_code

        assert fake_speechmatics_client[0].fed == []
        assert fake_speechmatics_client[0].closed


# #13: 두 연결의 격리와 개별 종료


# 테스트 이름: test_connections_are_isolated_on_disconnect
# 목적: 한 연결의 상태 변경·연결 해제가 다른 연결의 음성 처리에 영향을 주지 않는다.
# 준비: 가짜 STT 클라이언트와 서로 다른 회의·녹음 ID·PCM을 가진 A·B.
# 실행: A·B 연결을 함께 열고 각각 start → ready → meta(0)/binary를 수행한다.
# 실행: A를 pause한 상태에서 B의 meta(1)/binary를 처리한다.
# 실행: A를 stop 없이 끊고, B에서 meta(2)/binary → stop → ended까지 진행한다.
# 기대 결과: A 정리 후에도 B는 입력을 처리하고 자기 ID와 lastSequence=2로 정상 종료한다.
# 기대 결과: 두 공급자의 입력 목록에 각 연결의 bytes만 순서대로 들어 있다.
# 기대 결과: A 연결 종료 시 A만 정리되고, B는 자기 종료 시 정리된다.
# 실패 조건: 상태·ID·sequence·오디오가 섞이거나 A 종료로 B 처리도 중단된다.
# 주의: A의 정리가 완료된 뒤 B를 검사하며, 정리 대기에는 실행 기한을 둔다.
# 핵심 API: TestClient.websocket_connect, send_json, send_bytes, receive_json.
def test_connections_are_isolated_on_disconnect(fake_speechmatics_client):
    with TestClient(create_live_app()) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws_b:
            with client.websocket_connect(WEBSOCKET_URL) as ws_a:
                ws_a.send_json(
                    {
                        "type": "session.start",
                        "requestId": "start-01",
                        "meetingId": 1,
                        "recordingSessionId": 1,
                        "participantCount": 0,
                        "participants": [],
                    }
                )
                ready_a = ws_a.receive_json()
                assert ready_a == {
                    "type": "session.ready",
                    "requestId": "start-01",
                    "meetingId": 1,
                    "recordingSessionId": 1,
                    "payload": {
                        "status": "READY",
                        "outputAudioFormat": "pcm_s16le",
                        "outputSampleRateHz": 16000,
                        "outputChannels": 1,
                    },
                }

                ws_b.send_json(
                    {
                        "type": "session.start",
                        "requestId": "start-02",
                        "meetingId": 2,
                        "recordingSessionId": 2,
                        "participantCount": 0,
                        "participants": [],
                    }
                )
                ready_b = ws_b.receive_json()
                assert ready_b == {
                    "type": "session.ready",
                    "requestId": "start-02",
                    "meetingId": 2,
                    "recordingSessionId": 2,
                    "payload": {
                        "status": "READY",
                        "outputAudioFormat": "pcm_s16le",
                        "outputSampleRateHz": 16000,
                        "outputChannels": 1,
                    },
                }

                ws_a.send_json(meta_message)
                ws_b.send_json(meta_message)

                ws_a.send_bytes(b"\x01\x00" * 8000)
                ws_b.send_bytes(b"\x02\x00" * 8000)
                assert ws_a.receive_json()["type"] == "audio.ack"
                assert ws_b.receive_json()["type"] == "audio.ack"

                ws_a.send_json({"type": "session.pause", "requestId": "pause-01"})
                paused_a = ws_a.receive_json()
                assert paused_a == {
                    "type": "session.paused",
                    "requestId": "pause-01",
                    "payload": {"status": "PAUSED"},
                }

                ws_b.send_json({"type": "audio.meta", "payload": {"sequence": 1}})
                ws_b.send_bytes(b"\x03\x00" * 8000)
                assert ws_b.receive_json()["type"] == "audio.ack"

            # A는 stop 없이 끊긴 상태 (with문 바깥)
            provider_b, provider_a = fake_speechmatics_client
            assert provider_a.fed == [b"\x01\x00" * 8000]
            assert provider_a.closed
            assert not provider_b.closed

            ws_b.send_json({"type": "audio.meta", "payload": {"sequence": 2}})
            ws_b.send_bytes(b"\x04\x00" * 8000)
            assert ws_b.receive_json()["type"] == "audio.ack"
            ws_b.send_json(stop_message)
            ended_b = ws_b.receive_json()
            assert ended_b == {
                "type": "session.ended",
                "requestId": "stop-01",
                "meetingId": 2,
                "recordingSessionId": 2,
                "payload": {
                    "status": "ENDED",
                    "lastSequence": 2,
                    "audioDurationMs": 1500,
                },
            }

            with pytest.raises(WebSocketDisconnect) as error:
                ws_b.receive_json()
            assert error.value.code == 1000

        assert provider_b.fed == [b"\x02\x00" * 8000, b"\x03\x00" * 8000, b"\x04\x00" * 8000]
        assert provider_b.closed


# 테스트 이름: test_live_meeting_with_pcm
# 목적: PCM이 여러 청크로 나뉘어도 원본 그대로 STT에 전달되고 길이가 합산되는지 확인한다.
# 준비: 16kHz mono s16le PCM 10초와 가짜 STT 클라이언트.
# 실행: start → ready → meta(n)/binary 반복 → stop → ended.
# 기대 결과: 전달된 PCM이 원본과 같고 마지막 sequence와 오디오 길이가 일치한다.
# 실패 조건: 청크가 유실·변형되거나 합산 길이가 달라진다.
def test_live_meeting_with_pcm(fake_speechmatics_client):
    pcm = b"\x01\x00" * 160000
    chunks = [pcm[i : i + CHUNK_SIZE] for i in range(0, len(pcm), CHUNK_SIZE)]
    with TestClient(create_live_app()) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            assert ws.receive_json()["type"] == "session.ready"

            for i, chunk in enumerate(chunks):
                ws.send_json({"type": "audio.meta", "payload": {"sequence": i}})
                ws.send_bytes(chunk)
                assert ws.receive_json()["payload"]["sequence"] == i

            ws.send_json(stop_message)
            ended = ws.receive_json()
            assert ended["payload"]["lastSequence"] == len(chunks) - 1
            assert ended["payload"]["audioDurationMs"] == 10000
            assert b"".join(fake_speechmatics_client[0].fed) == pcm

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1000


# #18: 녹음 종료 전 확정 전사 전달
# 테스트 이름: test_committed_transcript_arrives_before_stop
# 목적: 추가 입력이나 stop 없이도 공급자 결과가 Backend에 전달되는지 확인한다.
# 준비: Speechmatics 외부 통신만 대체한다.
# 준비: 공급자 대역은 전달받은 PCM을 기록하고, 입력 처리와 별개로 확정 결과를 내보낸다.
# 준비: 0.1~0.4초의 한 문장 "안녕하세요."에 해당하는 확정 결과를 사용한다.
# 준비: 공급자 연결 API와 monkeypatch 대상은 연동 코드의 경계를 정한 뒤 지정한다.
# 실행: start → ready → meta(0) → binary 전송 후 공급자 대역에서 결과를 발생시킨다.
# 실행: stop이나 추가 음성을 보내지 않은 상태에서 transcript.committed를 수신한다.
# 기대 결과: 공급자에는 수신한 PCM binary가 그대로 전달된다.
# 기대 결과: meetingId와 recordingSessionId가 시작 요청과 일치한다.
# 기대 결과: payload는 sequenceNumber=0, content="안녕하세요.",
# 기대 결과: startedAtMs=100, endedAtMs=400이며 recognizedAt은 UTC 시각이다.
# 기대 결과: speakerId=0이고, 전사 수신 시점까지 session.ended가 오지 않는다.
# 실패 조건: stop 또는 다음 입력까지 결과가 보류되거나 PCM·ID·내용·시간이 잘못 전달된다.
# 정리: 성공·실패 모두 Backend 연결을 닫고 공급자 연결과 작업을 정리한다.
# 주의: receive_json에는 자체 timeout이 없다. 실행 시 테스트 프로세스에 기한을 두고,
# 주의: 기한 초과 시 실패로 처리하고 프로세스를 종료한다. 스레드 대기만 중단하지 않는다.
# 핵심 API: TestClient.websocket_connect, send_json, send_bytes, receive_json, monkeypatch.
# 다음 한 단계: 실패를 확인한 뒤 녹음 세션과 STT 연결 수명주기를 연결한다.
def test_committed_transcript_arrives_before_stop(fake_speechmatics_client):
    with TestClient(create_live_app()) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"

            ws.send_json(meta_message)
            ws.send_bytes(b"\x01\x00" * 8000)
            assert ws.receive_json()["type"] == "audio.ack"

            # STT 연결이 없다면 receive_json()에서 무한 대기하므로 먼저 배선을 확인한다.
            assert len(fake_speechmatics_client) == 1
            fake_speechmatics_client[0].emit_transcript()

            response = ws.receive_json()
            assert response["type"] == "transcript.committed"
            assert response["meetingId"] == 1
            assert response["recordingSessionId"] == 1

            payload = response["payload"]
            assert payload["sequenceNumber"] == 0
            assert payload["content"] == "안녕하세요."
            assert payload["startedAtMs"] == 100
            assert payload["endedAtMs"] == 400
            assert payload["speakerId"] == 0

            recognized_at = datetime.fromisoformat(payload["recognizedAt"].replace("Z", "+00:00"))
            assert recognized_at.utcoffset() == timedelta(0)

        stt_client = fake_speechmatics_client[0]
        assert stt_client.proxy_token_id == "test-proxy-id"
        assert stt_client.fed == [b"\x01\x00" * 8000]
        assert not stt_client.finished
        assert stt_client.closed


# #18: 입력 대기 중 STT 연결 단절
# 테스트 이름: test_provider_disconnect_while_waiting_for_input
# 목적: 입력 대기 중에도 공급자 단절을 감지하고 연결과 작업을 정리한다.
# 준비: 기존 fake_speechmatics_client를 재사용한다.
# 준비: 공급자 대역에 연결 단절을 발생시키는 동작을 추가한다. 호출자에게 즉시 raise하는
# 준비: 방식이 아니라, 실제 클라이언트와 같은 오류 전달 경로로 라우터에 단절을 알린다.
# 준비: on_error 콜백으로 공급자 단절을 알리고, 라우터가 이 콜백을 연결했는지 확인한다.
# 실행: start → ready를 받은 뒤 공급자 연결 단절을 발생시킨다.
# 실행: 추가 입력 없이 error 이벤트와 종료를 기다린다.
# 기대 결과: 추가 입력 없이 error.payload.code는 provider_unavailable, retryable은 true이다.
# 기대 결과: 특정 Backend 요청의 실패가 아니므로 requestId는 생략한다.
# 기대 결과: session.ended 없이 WebSocketDisconnect.code=1011로 연결이 종료된다.
# 기대 결과: 세션 처리 종료 후 공급자가 닫히고 recording_connections가 0이다.
# 기대 결과: 세션 핸들러와 전사 전송 작업이 종료된다. SDK 내부 작업 정리는 별도 검증한다.
# 실패 조건: 다음 입력까지 종료가 보류되거나 다른 원인의 오류로 종료되거나,
# 실패 조건: 정상 종료 이벤트를 보내거나 연결·작업·연결 수가 정리되지 않으면 실패한다.
# 주의: 임의의 sleep 대신 준비 완료 응답과 종료 신호를 기준으로 검사한다.
# 주의: TestClient.receive_json에는 자체 timeout이 없으므로 테스트 프로세스에 10초
# 주의: 실행 기한을 둔다. 초과 시 실패로 처리하고 프로세스를 종료하여 무한 대기를 막는다.
# 정리: 성공·실패 모두 WebSocket과 TestClient를 닫는다. 작업 확인은 해당 세션 대상으로
# 정리: 한정하고 TestClient 자체 작업까지 모두 종료됐다고 검사하지 않는다.
# 핵심 API: TestClient.websocket_connect, receive_json, pytest.raises(WebSocketDisconnect).
def test_provider_disconnect_while_waiting_for_input(fake_speechmatics_client, monkeypatch):
    app = create_live_app()
    session_finished = Event()
    sender_tasks = []
    original_sender = connection_module.send_transcripts

    async def tracked_sender(*args, **kwargs):
        sender_tasks.append(asyncio.current_task())
        await original_sender(*args, **kwargs)

    async def observed_app(scope, receive, send):
        try:
            await app(scope, receive, send)
        finally:
            if scope["type"] == "websocket":
                session_finished.set()

    monkeypatch.setattr(connection_module, "send_transcripts", tracked_sender)

    with TestClient(observed_app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"
            provider = fake_speechmatics_client[0]
            assert not provider.closed
            assert app.state.recording_connections == 1
            assert callable(provider.on_error), "라우터가 STT 오류 콜백을 연결해야 합니다."

            client.portal.call(provider.emit_disconnect)

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
            assert app.state.recording_connections == 0
            assert sender_tasks and all(task.done() for task in sender_tasks)


# 테스트 이름: test_provider_disconnect_during_start
# 목적: STT 시작을 기다리는 중에도 공급자 오류가 즉시 전달되도록 한다.
# 준비: 공급자 start를 보류하고 종료·취소 여부를 신호로 관찰한다.
# 실행: start 처리 진입 후 공급자 오류를 발생시키고 추가 입력 없이 종료를 기다린다.
# 기대 결과: 시작 대기가 취소되고 provider_unavailable → close 1011 이후 자원이 정리된다.
# 실패 조건: 시작 완료나 다음 입력까지 오류 전달이 보류되거나 자원이 남으면 실패한다.
def test_provider_disconnect_during_start(fake_speechmatics_client):
    app = create_live_app()
    starting = Event()
    start_cancelled = Event()
    session_finished = Event()

    async def observed_app(scope, receive, send):
        try:
            await app(scope, receive, send)
        finally:
            if scope["type"] == "websocket":
                session_finished.set()

    with TestClient(observed_app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            provider = fake_speechmatics_client[0]

            async def waiting_start(**kwargs):
                provider.closed = False
                starting.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    start_cancelled.set()

            provider.start = waiting_start
            ws.send_json(start_message)
            assert starting.wait(3), "STT 시작 처리에 진입하지 않았습니다."

            client.portal.call(provider.emit_disconnect)

            assert session_finished.wait(3), "STT 시작 대기 중 공급자 오류가 전달되지 않았습니다."
            response = ws.receive_json()
            assert response["type"] == "error"
            assert response["payload"]["code"] == "provider_unavailable"
            assert response["payload"]["retryable"] is True
            assert "requestId" not in response
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1011
            assert start_cancelled.is_set()
            assert provider.closed
            assert not provider.finished
            assert app.state.recording_connections == 0


# 테스트 이름: test_final_transcript_precedes_session_ended
# 목적: 종료 직전의 전사와 문장부호 없는 잔여 문장이 유실되지 않도록 한다.
# 준비: 기존 공급자 대역의 finish 중 마지막 확정 단어를 전달하도록 구성한다.
# 실행: start → ready → stop 이후 Backend가 받는 이벤트 순서를 확인한다.
# 기대 결과: 마지막 transcript.committed → session.ended → close 1000 순서이며,
# 기대 결과: 세션 처리 종료 후 공급자가 닫히고 연결 수가 0이다.
# 실패 조건: 종료 응답이 전사보다 앞서거나 잔여 문장이 누락되면 실패한다.
def test_final_transcript_precedes_session_ended(fake_speechmatics_client):
    app = create_live_app()
    with TestClient(app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            ws.send_json(start_message)
            assert ws.receive_json()["type"] == "session.ready"

            provider = fake_speechmatics_client[0]

            async def finish_with_final_transcript():
                provider.on_transcript(
                    STTTranscript(content="마지막", started_at_ms=1000, ended_at_ms=1500)
                )
                provider.finished = True

            provider.finish = finish_with_final_transcript
            ws.send_json(stop_message)

            transcript = ws.receive_json()
            ended = ws.receive_json()
            assert transcript["type"] == "transcript.committed"
            assert transcript["payload"]["content"] == "마지막"
            assert transcript["payload"]["speakerId"] is None
            assert ended["type"] == "session.ended"

            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1000

        assert provider.finished
        assert provider.closed
        assert app.state.recording_connections == 0


# 테스트 이름: test_stop_timeout_cleans_up_session
# 목적: 공급자가 종료에 응답하지 않아도 세션과 전송 작업이 남지 않도록 한다.
# 준비: 공급자 finish가 대기하도록 하고 STT_STOP_TIMEOUT을 짧게 설정한다.
# 실행: start → ready → stop 후 세션 핸들러의 종료를 기한 내 기다린다.
# 기대 결과: processing_timeout → close 1011이며 관련 작업·연결 수가 정리된다.
# 실패 조건: session.ended가 오거나 세션이 계속 대기하면 실패한다.
def test_stop_timeout_cleans_up_session(fake_speechmatics_client, monkeypatch):
    app = create_live_app()
    monkeypatch.setattr(connection_module, "STT_STOP_TIMEOUT", 0.01)

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
        assert app.state.recording_connections == 0


# 테스트 이름: test_transcript_send_failure_stops_receiving
# 목적: 입력 대기 중에도 전사 전송 실패를 감지하고 자원을 정리한다.
# 준비: 전사 전송 경계에서 TimeoutError를 발생시킨다.
# 실행: start → ready → 전송 실패 확인 후 추가 입력 없이 오류 응답을 기다린다.
# 기대 결과: 공급자·세션 작업이 정리되고 연결 수가 0이다.
# 기대 결과: 정리 중 추가로 발생한 공급자 오류는 Sentry에 별도 보고하지 않는다.
# 실패 조건: 다음 입력이나 클라이언트 연결 해제까지 정리가 보류되면 실패한다.
# 핵심 API: 기존 observed_app·Event 종료 관찰 방식, client.portal.call, monkeypatch.
# 주의: 모든 사례는 프로세스 실행 기한을 두고 성공·실패 모두 TestClient를 닫는다.
def test_transcript_send_failure_stops_receiving(fake_speechmatics_client, monkeypatch):
    app = create_live_app()
    sender_failed = Event()
    reported_errors = []

    async def failing_sender(*args, **kwargs):
        sender_failed.set()
        raise TimeoutError

    monkeypatch.setattr(connection_module, "send_transcripts", failing_sender)
    monkeypatch.setattr(connection_module.sentry_sdk, "capture_exception", reported_errors.append)

    with TestClient(app) as client:
        with client.websocket_connect(WEBSOCKET_URL) as ws:
            provider = fake_speechmatics_client[0]

            async def close_with_provider_error():
                provider.emit_disconnect()
                provider.closed = True

            provider.close = close_with_provider_error
            ws.send_json(start_message)
            assert ws.receive_json()["type"] == "session.ready"
            assert sender_failed.wait(1), "전사 전송 작업이 시작되지 않았습니다."

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
        assert fake_speechmatics_client[0].fed == []
        assert app.state.recording_connections == 0
        assert reported_errors == []


# 테스트 이름: test_audio_ack_heartbeat_and_reconnection
# 목적: STT 지연·중복 전송·일시정지·재연결 중에도 수신 ACK와 전사 이어받기가 동작한다.
# 준비: 첫 STT 전송을 보류하고, 종료 시 마지막 확정 단어를 반환하는 공급자 대역.
# 실행: 중간 순번 시작 → meta 사이 ping → binary/ACK → 중복 → pause/pong → resume.
# 실행: 짧은 청크를 추가한 뒤 stop하고, 같은 녹음의 다음 순번으로 새 연결을 시작한다.
# 기대 결과: STT 완료 전 ACK, 중복 입력 제거, 동일 requestId pong, 전사 번호·시간 이어받기.
# 실패 조건: STT 지연으로 제어 응답이 막히거나 중복 PCM·전사 번호 초기화·정리 누락이 발생한다.
def test_audio_ack_heartbeat_and_reconnection(fake_speechmatics_client):
    app = create_live_app()
    first_chunk = b"\x01\x00" * 1600
    short_chunk = b"\x02\x00" * 320

    with TestClient(app) as client:
        for connection_index, sequence in enumerate((100, 102)):
            with client.websocket_connect(WEBSOCKET_URL) as ws:
                ws.send_json(
                    {
                        **start_message,
                        "startSequence": sequence,
                        "transcriptSequenceStart": 7 + connection_index,
                    }
                )
                assert ws.receive_json()["type"] == "session.ready"
                provider = fake_speechmatics_client[connection_index]
                release = client.portal.call(asyncio.Event)
                sending = Event()

                async def send_audio(chunk, *, sending=sending, release=release, provider=provider):
                    sending.set()
                    await release.wait()
                    provider.fed.append(chunk)

                async def finish(provider=provider):
                    provider.on_transcript(
                        STTTranscript(content="이어가기", started_at_ms=20, ended_at_ms=80)
                    )
                    provider.finished = True

                provider.send_audio = send_audio
                provider.finish = finish

                def ping(request_id):
                    ws.send_json(
                        {
                            "type": "heartbeat.ping",
                            "requestId": request_id,
                            "recordingSessionId": "1",
                        }
                    )
                    assert ws.receive_json() == {
                        "type": "heartbeat.pong",
                        "requestId": request_id,
                        "recordingSessionId": 1,
                    }

                def expect_ack(acked_sequence):
                    assert ws.receive_json() == {
                        "type": "audio.ack",
                        "recordingSessionId": 1,
                        "payload": {"sequence": acked_sequence},
                    }

                # 새 연결은 BE checkpoint 이전의 재전송도 다시 STT에 넣지 않는다.
                if connection_index:
                    ws.send_json({"type": "audio.meta", "payload": {"sequence": sequence - 1}})
                    ws.send_bytes(short_chunk)
                    expect_ack(sequence - 1)

                ws.send_json({"type": "audio.meta", "payload": {"sequence": sequence}})
                ping("between-meta-and-binary")
                ws.send_bytes(first_chunk)
                expect_ack(sequence)
                assert sending.wait(1), "STT 전달 작업이 시작되지 않았습니다."
                assert provider.fed == []

                ws.send_json({"type": "audio.meta", "payload": {"sequence": sequence}})
                ws.send_bytes(first_chunk)
                expect_ack(sequence)

                ws.send_json({"type": "session.pause", "requestId": "pause-ack"})
                assert ws.receive_json()["type"] == "session.paused"
                ping("while-paused")
                ws.send_json({"type": "session.resume", "requestId": "resume-ack"})
                assert ws.receive_json()["type"] == "session.resumed"
                assert len(fake_speechmatics_client) == connection_index + 1

                expected_pcm = [first_chunk]
                if not connection_index:
                    ws.send_json({"type": "audio.meta", "payload": {"sequence": sequence + 1}})
                    ws.send_bytes(short_chunk)
                    expect_ack(sequence + 1)
                    # 예전 중복에도 ACK는 마지막 수신 순번에서 뒤로 가지 않는다.
                    ws.send_json({"type": "audio.meta", "payload": {"sequence": sequence}})
                    ws.send_bytes(first_chunk)
                    expect_ack(sequence + 1)
                    expected_pcm.append(short_chunk)

                client.portal.call(release.set)
                ws.send_json(stop_message)
                transcript = ws.receive_json()
                assert transcript["type"] == "transcript.committed"
                assert transcript["payload"]["sequenceNumber"] == 7 + connection_index
                assert transcript["payload"]["startedAtMs"] == sequence * 100 + 20
                assert transcript["payload"]["endedAtMs"] == sequence * 100 + 80
                ended = ws.receive_json()
                assert ended["type"] == "session.ended"
                assert ended["payload"]["lastSequence"] == sequence + (connection_index == 0)
                assert ended["payload"]["audioDurationMs"] == (120 if not connection_index else 100)
                with pytest.raises(WebSocketDisconnect) as error:
                    ws.receive_json()
                assert error.value.code == 1000

            assert provider.fed == expected_pcm
            assert provider.finished
            assert provider.closed
            assert app.state.recording_connections == 0
