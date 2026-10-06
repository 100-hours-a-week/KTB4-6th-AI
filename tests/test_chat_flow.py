import asyncio
from threading import Event

import pytest
from fastapi.testclient import TestClient
from pydantic import AnyHttpUrl
from starlette.websockets import WebSocketDisconnect

import meety_ai.live_meeting.router as router_module
import meety_ai.recording.transcript as transcript_module
from meety_ai.chat.schemas import ChatCompletedPayload
from meety_ai.live import create_live_app


# 테스트 이름: test_qna_generation_does_not_block_transcription
# 목적: 기존 WebSocket에서 답변 생성 대기 중에도 오디오 수신과 확정 전사 전달이 계속됨을 보호한다.
# 준비: tests/conftest.py의 fake_decoders와 fake_speechmatics_client를 재사용한다.
# 준비: tests/test_recording_router.py의 start/reset/meta/binary/stop 메시지 형식을 참고한다.
# 준비: 질문은 시작 메시지와 같은 meetingId, aiRequestId="qna-101", teamId=1을 사용한다.
# 준비: 질문 이전의 공유 대화와 현재 전사 목록은 빈 배열로 전달한다.
# 준비: 실제 Agent 실행 경계인 live_meeting.router.generate_answer만 대체한다.
# 준비: Agent 대역은 시작 신호를 보내고 asyncio.Event가 해제될 때까지 기다린다.
# 준비: 해제 후 ChatCompletedPayload(answer="확인했습니다.", citations=[])를 반환한다.
# 실행: session.start → session.ready → decoder.reset → decoder.ready → chat.request를 보낸다.
# 실행: chat.accepted 수신과 Agent 시작을 확인하되, Agent 완료 신호는 해제하지 않는다.
# 실행: audio.meta(0) → binary를 연속 전송하고 가짜 STT에서 emit_transcript()를 호출한다.
# 실행: Agent가 대기 중인 상태에서 transcript.committed를 받은 뒤 완료 신호를 해제한다.
# 실행: chat.completed 수신 후 session.stop → session.ended → 정상 연결 종료를 확인한다.
# 기대 결과: 이벤트 순서는 chat.accepted → transcript.committed → chat.completed다.
# 기대 결과: 두 chat 이벤트의 meetingId와 aiRequestId가 질문과 같고 답변·빈 근거가 전달된다.
# 기대 결과: STT에 디코딩된 PCM이 전달되며 전사 내용은 "안녕하세요."다.
# 기대 결과: 종료 후 공급자·디코더가 닫히고 recording_connections는 0이다.
# 기대 결과: 해당 연결의 QnA 작업이 완료되어 남지 않는다.
# 실패 조건: 답변을 해제하기 전 전사를 받지 못하거나 오디오가 처리되지 않으면 실패한다.
# 실패 조건: 이벤트 순서·식별자·답변이 다르거나 연결 자원이 남으면 실패한다.
# 준비: 입력 샘플은 WEBM_HEAD + b"compressed audio"를 참고한다. 실제 외부 통신은 하지 않는다.
# 준비: 성공·실패 모두 완료 신호를 해제하고 WebSocket과 TestClient를 닫는다.
# 준비: receive_json에는 자체 timeout이 없으므로 테스트 프로세스에 10초 실행 기한을 둔다.
# 준비: 임의의 sleep 대신 시작·완료 신호를 사용하고, asyncio.Event 해제는 같은 이벤트 루프에서 한다.
# 핵심 API: TestClient.websocket_connect, send_json, send_bytes, receive_json, client.portal.call.
def test_qna_generation_does_not_block_transcription(
    monkeypatch, fake_decoders, fake_speechmatics_client
):
    agent_started = Event()
    pcm_received = Event()
    transcript_sent = Event()
    answer_sent = Event()
    release_agent = None
    agent_tasks = []
    original_send = transcript_module.send_message
    provider_factory = router_module.SpeechmaticsClient

    def observed_provider(**kwargs):
        provider = provider_factory(**kwargs)
        original_audio = provider.send_audio

        async def observed_audio(chunk):
            await original_audio(chunk)
            pcm_received.set()

        provider.send_audio = observed_audio
        return provider

    async def waiting_agent(request, model, backend_client, **kwargs):
        nonlocal release_agent
        release_agent = asyncio.Event()
        agent_tasks.append(asyncio.current_task())
        agent_started.set()
        await release_agent.wait()
        return ChatCompletedPayload(answer="확인했습니다.", citations=[])

    async def observed_send(websocket, message, send_lock=None):
        await original_send(websocket, message, send_lock)
        if message.type == "transcript.committed":
            transcript_sent.set()
        elif message.type == "chat.completed":
            answer_sent.set()

    monkeypatch.setattr(router_module, "generate_answer", waiting_agent)
    monkeypatch.setattr(router_module, "SpeechmaticsClient", observed_provider)
    monkeypatch.setattr(router_module, "send_message", observed_send)
    monkeypatch.setattr(transcript_module, "send_message", observed_send)
    app = create_live_app()
    app.state.chat_model = object()
    app.state.settings.backend_base_url = AnyHttpUrl("https://backend.example")
    chat_request = {
        "type": "chat.request",
        "meetingId": "meeting-01",
        "aiRequestId": "qna-101",
        "payload": {
            "teamId": 1,
            "question": "현재 발언을 확인해 주세요.",
            "conversationHistory": [],
            "transcriptSegments": [],
        },
    }

    with TestClient(app) as client:
        with client.websocket_connect("/v1/live-meeting") as ws:
            ws.send_json(
                {
                    "type": "session.start",
                    "requestId": "start-01",
                    "meetingId": "meeting-01",
                    "recordingSessionId": "recording-01",
                }
            )
            assert ws.receive_json()["type"] == "session.ready"
            ws.send_json(
                {
                    "type": "decoder.reset",
                    "requestId": "reset-01",
                    "payload": {"audioFormat": "webm_opus"},
                }
            )
            assert ws.receive_json()["type"] == "decoder.ready"
            provider = fake_speechmatics_client[0]
            try:
                ws.send_json(chat_request)
                assert ws.receive_json() == {
                    "type": "chat.accepted",
                    "meetingId": "meeting-01",
                    "aiRequestId": "qna-101",
                }
                assert agent_started.wait(3), "Agent 작업이 시작되지 않았습니다."
                ws.send_json({"type": "audio.meta", "payload": {"sequence": 0}})
                ws.send_bytes(b"\x1a\x45\xdf\xa3compressed audio")
                assert pcm_received.wait(3), "답변 대기 중 오디오 처리가 막혔습니다."
                assert provider.fed == [b"\0" * 16000]
                client.portal.call(provider.emit_transcript)
                assert transcript_sent.wait(3), "답변 대기 중 전사 전달이 막혔습니다."
                transcript = ws.receive_json()
                assert transcript["type"] == "transcript.committed"
                assert transcript["meetingId"] == "meeting-01"
                assert transcript["payload"]["content"] == "안녕하세요."
                assert not answer_sent.is_set()

                client.portal.call(release_agent.set)
                assert answer_sent.wait(3), "Agent 완료 후 답변이 전달되지 않았습니다."
                assert ws.receive_json() == {
                    "type": "chat.completed",
                    "meetingId": "meeting-01",
                    "aiRequestId": "qna-101",
                    "payload": {"answer": "확인했습니다.", "citations": []},
                }
                ws.send_json({"type": "session.stop", "requestId": "stop-01"})
                assert ws.receive_json()["type"] == "session.ended"
                with pytest.raises(WebSocketDisconnect) as error:
                    ws.receive_json()
                assert error.value.code == 1000
            finally:
                if release_agent is not None:
                    client.portal.call(release_agent.set)

        assert provider.closed
        assert fake_decoders[0].closed
        assert app.state.recording_connections == 0
        assert agent_tasks and all(task.done() for task in agent_tasks)
