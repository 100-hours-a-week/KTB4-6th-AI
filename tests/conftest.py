import subprocess

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
