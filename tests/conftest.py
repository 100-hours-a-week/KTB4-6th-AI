import subprocess

import pytest

import meety_ai.recording.router as router_module
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


SINE_SOURCE = ["-f", "lavfi", "-i", "sine=frequency=440:duration=10"]
ENCODE_OPTIONS = {
    "webm_opus": ["-c:a", "libopus", "-f", "webm"],
    "mp4_aac": ["-c:a", "aac", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4"],
}


def run_ffmpeg(*args, input=None):
    return subprocess.run(
        ["ffmpeg", "-loglevel", "error", *args, "pipe:1"],
        input=input,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture(scope="session")
def audio_samples():
    """형식별 (압축 입력, 입력 전체를 한 번에 변환한 기준 PCM)을 만든다."""
    samples = {}
    for audio_format, options in ENCODE_OPTIONS.items():
        encoded = run_ffmpeg(*SINE_SOURCE, *options)
        reference_pcm = run_ffmpeg(
            "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "s16le", input=encoded
        )
        samples[audio_format] = (encoded, reference_pcm)
    return samples


@pytest.fixture(autouse=True)
def fake_speechmatics_client(monkeypatch):
    instances = []
    transcript = {
        "message": "AddTranscript",
        "metadata": {"transcript": "안녕하세요."},
        "results": [
            {
                "type": "word",
                "start_time": 0.1,
                "end_time": 0.4,
                "alternatives": [{"content": "안녕하세요.", "confidence": 0.99, "speaker": "S1"}],
            }
        ],
    }

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
        router_module,
        "SpeechmaticsClient",
        FakeSpeechmaticsClient,
        raising=False,
    )
    return instances
