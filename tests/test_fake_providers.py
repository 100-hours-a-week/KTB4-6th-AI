import time

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from structlog.testing import capture_logs

import meety_ai.live as live_module
from meety_ai.fake_providers import create_live_app


# 테스트 이름: test_fake_live_app_applies_context_without_external_llm
# 목적: mock 앱이 맥락 생성 경로까지 가짜로 대체해 녹음을 정상 종료하는지 보호한다.
# 준비: 실제 LLM 자리에 생성에 성공하는 모델을 두고, fake_providers 앱과 제목 있는 시작 메시지.
# 실행: start → 오디오 1초 → 맥락 처리 결과 로그 대기 → stop.
# 기대 결과: 맥락 갱신 후에도 전사와 session.ended를 받는다.
# 실패 조건: 가짜 STT에 update_context가 없거나 실제 LLM이 호출되어 세션이 오류로 끝난다.
def test_fake_live_app_applies_context_without_external_llm(monkeypatch):
    monkeypatch.setattr(
        live_module, "ChatOpenAI", lambda **_: FakeListChatModel(responses=["회의: 실제 LLM"])
    )
    with TestClient(create_live_app()) as client, capture_logs() as logs:
        with client.websocket_connect("/v1/live-meeting") as ws:
            ws.send_json(
                {
                    "type": "session.start",
                    "requestId": "start-01",
                    "meetingId": 1,
                    "recordingSessionId": 1,
                    "participantCount": 0,
                    "participants": [],
                    "title": "STT 검토",
                }
            )
            assert ws.receive_json()["type"] == "session.ready"
            ws.send_json({"type": "audio.meta", "payload": {"sequence": 0}})
            ws.send_bytes(b"\x01\x00" * 16000)
            assert ws.receive_json()["type"] == "audio.ack"

            done = {"stt_context_applied", "stt_context_failed", "session_closed"}
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any(log["event"] in done for log in logs):
                time.sleep(0.01)
            assert [log["event"] for log in logs if log["event"] in done] == ["stt_context_applied"]
            ws.send_json({"type": "session.stop", "requestId": "stop-01"})
            assert [ws.receive_json()["type"] for _ in range(2)] == [
                "transcript.committed",
                "session.ended",
            ]
