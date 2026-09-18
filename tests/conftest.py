import pytest

import meety_ai.recording.session as session_module


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
