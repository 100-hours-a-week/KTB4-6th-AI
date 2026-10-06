import asyncio
from threading import Event

import pytest
from fastapi.testclient import TestClient
from pydantic import AnyHttpUrl
from starlette.websockets import WebSocketDisconnect

import meety_ai.live_meeting.router as router_module
from meety_ai.live import create_live_app


# 테스트 이름: test_provider_disconnect_without_next_input_closes_live_meeting
# 목적: 입력이 없는 일시정지 중에도 공급자 오류가 수신 대기를 깨우고 자원을 정리해야 한다.
# 준비: 기존 fake_decoders·fake_speechmatics_client와 observed_app 종료 신호를 재사용한다.
# 준비: tests/test_chat_flow.py의 질문 입력과 app.state의 chat 설정을 재사용한다.
# 준비: router.generate_answer만 대체해 시작 신호를 보낸 뒤 완료 신호 없이 대기하게 한다.
# 준비: 답변 대역은 CancelledError를 받으면 취소 신호를 남기고 같은 예외를 다시 올린다.
# 준비: 대기 신호는 asyncio.Event, 테스트 스레드에서 관찰할 신호는 threading.Event를 쓴다.
# 실행: start → ready → reset → decoder.ready → chat.request → chat.accepted를 확인한다.
# 실행: 답변 작업의 시작을 확인한 뒤 pause → session.paused를 확인한다.
# 실행: client.portal.call(provider.emit_disconnect)를 호출한다.
# 실행: 이후 입력·stop·클라이언트 종료 없이 session_finished.wait(3)로 종료를 관찰한다.
# 기대 결과: error(provider_unavailable, retryable=True) → close 1011 순서다.
# 기대 결과: 요청에 따른 오류가 아니므로 error에 requestId가 없다.
# 기대 결과: 대기 중 답변 작업은 취소되고 chat.completed나 chat.failed가 추가로 오지 않는다.
# 기대 결과: 공급자는 closed=True·finished=False, 디코더는 closed=True다.
# 기대 결과: observed_app 종료 시점에 답변 취소 신호가 있고 recording_connections는 0이다.
# 실패 조건: 다음 입력이나 클라이언트 연결 해제 전까지 종료가 지연되거나 자원이 남는다.
# 실패 조건: 종료 중 답변이 전달되거나 정상 종료 session.ended가 오면 실패한다.
# 준비: 성공·실패 모두 대기 신호를 같은 이벤트 루프에서 해제하고 TestClient를 닫는다.
# 준비: receive_json 자체에는 timeout이 없으므로 종료 신호 확인 뒤 응답을 읽는다.
# 준비: 종료 실패 시에는 응답 읽기를 진행하지 않고 정리한다. 실행 프로세스에도 기한을 둔다.
# 핵심 API: tests/test_recording_router.py의 observed_app·Event·client.portal.call.
def test_provider_disconnect_without_next_input_closes_live_meeting(
    monkeypatch, fake_decoders, fake_speechmatics_client
):
    agent_started = Event()
    agent_cancelled = Event()
    session_finished = Event()
    release_agent = None
    agent_tasks = []

    async def waiting_agent(request, model, backend_client, **kwargs):
        nonlocal release_agent
        release_agent = asyncio.Event()
        agent_tasks.append(asyncio.current_task())
        agent_started.set()
        try:
            await release_agent.wait()
        except asyncio.CancelledError:
            agent_cancelled.set()
            raise

    monkeypatch.setattr(router_module, "generate_answer", waiting_agent)
    app = create_live_app()
    app.state.chat_model = object()
    app.state.settings.backend_base_url = AnyHttpUrl("https://backend.example")

    async def observed_app(scope, receive, send):
        try:
            await app(scope, receive, send)
        finally:
            if scope["type"] == "websocket":
                session_finished.set()

    with TestClient(observed_app) as client:
        with client.websocket_connect("/v1/live-meeting") as ws:
            try:
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
                ws.send_json(
                    {
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
                )
                assert ws.receive_json() == {
                    "type": "chat.accepted",
                    "meetingId": "meeting-01",
                    "aiRequestId": "qna-101",
                }
                assert agent_started.wait(3), "답변 작업이 시작되지 않았습니다."
                ws.send_json({"type": "session.pause", "requestId": "pause-01"})
                assert ws.receive_json()["type"] == "session.paused"
                provider = fake_speechmatics_client[0]
                client.portal.call(provider.emit_disconnect)

                # 종료 신호가 없으면 receive_json으로 무기한 기다리지 않는다.
                assert session_finished.wait(3), "추가 입력 없이 세션이 종료되지 않았습니다."
                assert agent_cancelled.is_set(), "대기 중 답변 작업이 취소되지 않았습니다."
                assert agent_tasks and all(task.done() for task in agent_tasks)
                assert provider.closed
                assert not provider.finished
                assert fake_decoders[0].closed
                assert app.state.recording_connections == 0
                response = ws.receive_json()
                assert response["type"] == "error"
                assert response["payload"]["code"] == "provider_unavailable"
                assert response["payload"]["retryable"] is True
                assert "requestId" not in response
                with pytest.raises(WebSocketDisconnect) as error:
                    ws.receive_json()
                assert error.value.code == 1011
            finally:
                if release_agent is not None:
                    client.portal.call(release_agent.set)
