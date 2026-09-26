"""회의 전체 음성의 화자 분리를 Modal L4 GPU에서 실행한다.

분석 서비스는 배포된 함수를 Modal Python SDK로 호출한다.
    modal.Function.from_name("meety-diarization", "diarize").remote(audio_url)
"""

import functools
import logging
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import modal

MODEL_ID = "nvidia/Nemotron-3-Diarization"
SAMPLE_RATE = 16000

logger = logging.getLogger(__name__)

image = (
    modal.Image.from_registry("nvidia/cuda:12.8.0-cudnn-devel-ubuntu22.04", add_python="3.12")
    .apt_install("ffmpeg", "libsndfile1", "git")
    .uv_pip_install(
        "torch==2.14.0",
        "soundfile==0.14.0",
        "librosa==1.0.0",
        "git+https://github.com/huggingface/transformers.git@27166ea03f12c940f23176a904ab1d2ff1a3dcbb",
    )
)
app = modal.App("meety-diarization")


def to_intervals(segments: list[dict]) -> list[dict]:
    """모델 구간(초)을 계약 형식(ms)으로 바꾸고, 겹침은 유지한 채 시작 시각순으로 정렬한다."""
    intervals = [
        {
            "speaker_id": int(item["Speaker"]),
            "start_ms": round(float(item["Start"]) * 1000),
            "end_ms": round(float(item["End"]) * 1000),
        }
        for item in segments
    ]
    intervals = [item for item in intervals if item["start_ms"] < item["end_ms"]]
    return sorted(intervals, key=lambda i: (i["start_ms"], i["end_ms"], i["speaker_id"]))


def _error(code: str) -> dict:
    return {"status": "error", "error_code": code}


@functools.cache
def _load_model():
    """컨테이너가 유지되는 동안 모델을 재사용한다. 실패는 캐시되지 않아 다음 호출에서 재시도한다."""
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor

    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModelForAudioFrameClassification.from_pretrained(MODEL_ID).to("cuda").eval()
    return processor, model


def _infer(wav: Path) -> list[dict]:
    import soundfile as sf
    import torch

    processor, model = _load_model()
    audio, sample_rate = sf.read(wav, dtype="float32")
    with torch.inference_mode():
        inputs = processor(audio, sampling_rate=sample_rate).to(model.device, dtype=model.dtype)
        logits = model(**inputs).logits.cpu()
    return processor.extract_speaker_dict(logits)[0]


# 모델 가중치는 콜드 스타트마다 HF에서 받는다.
# 콜드 스타트 지연이 문제되면 Volume 캐시로 옮긴다.
@app.function(
    image=image,
    gpu="L4",
    timeout=3600,
    secrets=[modal.Secret.from_name("huggingface-secret")],
)
def diarize(audio_url: str) -> dict:
    """presigned HTTPS GET URL의 회의 음성을 받아 화자 구간 또는 오류 코드를 반환한다.

    URL과 서명 쿼리는 로그에 남기지 않는다. 예외 메시지에 URL이 섞일 수 있어 예외 타입만 기록한다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source"
        wav = Path(tmp) / "meeting-16k.wav"

        try:
            if not audio_url.startswith("https://"):
                raise ValueError("https URL이 아닙니다.")
            with urllib.request.urlopen(audio_url, timeout=60) as response, source.open("wb") as f:
                shutil.copyfileobj(response, f)
        except Exception as e:
            logger.warning("diarize download_failed: %s", type(e).__name__)
            return _error("download_failed")

        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                str(wav),
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning("diarize decode_failed: ffmpeg exit %s", result.returncode)
            return _error("decode_failed")

        try:
            segments = _infer(wav)
        except Exception as e:
            logger.warning("diarize inference_failed: %s", type(e).__name__)
            return _error("inference_failed")

    return {"status": "ok", "intervals": to_intervals(segments)}
