import pytest

import meety_ai.live as live_module
from meety_ai.stt.schemas import STTTranscript


@pytest.fixture(autouse=True)
def fake_speechmatics_client(monkeypatch):
    instances = []
    transcript = STTTranscript(
        content="안녕하세요.", started_at_ms=100, ended_at_ms=400, speaker_id=0
    )

    class FakeSpeechmaticsClient:
        def __init__(self, api_key, on_transcript, on_error=None):
            self.api_key = api_key
            self.on_transcript = on_transcript
            self.on_error = on_error
            self.fed = []
            self.closed = True
            self.finished = False
            instances.append(self)

        async def start(self):
            self.closed = False

        async def send_audio(self, chunk):
            self.fed.append(chunk)

        def emit_transcript(self):
            self.on_transcript(transcript)

        def emit_disconnect(self):
            self.on_error(ConnectionError("공급자 연결이 끊어졌습니다."))

        async def finish(self):
            self.finished = True

        async def close(self):
            self.closed = True

    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-api-key")
    monkeypatch.setattr(
        live_module,
        "SpeechmaticsClient",
        FakeSpeechmaticsClient,
        raising=False,
    )
    return instances
