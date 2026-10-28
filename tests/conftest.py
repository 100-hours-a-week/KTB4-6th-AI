import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

import meety_ai.live as live_module
from meety_ai.stt.schemas import STTTranscript


@pytest.fixture(autouse=True)
def fake_speechmatics_client(monkeypatch):
    instances = []
    transcript = STTTranscript(
        content="안녕하세요.", started_at_ms=100, ended_at_ms=400, speaker_id=0
    )

    class FakeSpeechmaticsClient:
        def __init__(
            self,
            url,
            proxy_token_id,
            proxy_token_secret,
            on_transcript,
            on_error=None,
            start_timeout_seconds=240,
        ):
            self.proxy_token_id = proxy_token_id
            self.on_transcript = on_transcript
            self.on_error = on_error
            self.fed = []
            self.contexts = []
            self.closed = True
            self.finished = False
            instances.append(self)

        async def start(self, **kwargs):
            self.closed = False

        async def send_audio(self, chunk):
            self.fed.append(chunk)

        def emit_transcript(self):
            self.on_transcript(transcript)

        def emit_disconnect(self):
            self.on_error(ConnectionError("공급자 연결이 끊어졌습니다."))

        async def update_context(self, context):
            self.contexts.append(context)

        async def finish(self):
            self.finished = True

        async def close(self):
            self.closed = True

    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-api-key")
    monkeypatch.setenv("MEETY_MODAL_STT_URL", "wss://test.modal.run/transcribe")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "test-proxy-id")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "test-proxy-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    # 맥락 생성이 실제 LLM을 호출하지 않도록 빈 응답 모델로 대체한다.
    monkeypatch.setattr(live_module, "ChatOpenAI", lambda **_: FakeListChatModel(responses=[""]))
    monkeypatch.setattr(
        live_module,
        "ModalSTTClient",
        FakeSpeechmaticsClient,
        raising=False,
    )
    return instances
