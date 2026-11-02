"""사전 녹음에서 ReDimNet2+ 등록 벡터를 Modal CPU에서 추출한다.

배포: modal deploy -m meety_ai.modal_app.speaker_embedding

분석 서비스는 배포된 함수를 Modal Python SDK로 호출한다.
    modal.Function.from_name("meety-speaker-embedding", "embed_speaker").remote(audio_url)

회의 구간 벡터는 STT 컨테이너가 같은 `meety_ai.modal_app.redimnet.embed`로 만든다.
"""

import logging
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import modal

from meety_ai.modal_app.redimnet import MODEL_VERSION, PACKAGE, WEIGHTS_COMMANDS, embed

SAMPLE_RATE = 16000
MIN_SECONDS = 10
MAX_SECONDS = 60
MAX_BYTES = 20 * 1024 * 1024

logger = logging.getLogger(__name__)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "git", "curl")
    .uv_pip_install(
        "torch==2.14.1",
        "torchaudio==2.11.0",
        # uv는 처음 찾은 index만 보므로 PyTorch CPU index에 있는 버전으로 고정한다.
        "numpy==2.5.2",
        "scipy==1.18.1",
        # 저자 코드의 asv.models 패키지가 import 시점에 hydra를 불러온다.
        "hydra-core==1.3.8",
        "omegaconf==2.3.1",
        extra_index_url="https://download.pytorch.org/whl/cpu",
    )
    .uv_pip_install(PACKAGE, extra_options="--no-deps")
    .run_commands(*WEIGHTS_COMMANDS)
    # 공통 추출 모듈을 컨테이너에서 import한다.
    .add_local_python_source("meety_ai", copy=True)
)
app = modal.App("meety-speaker-embedding")


def _error(code: str) -> dict:
    return {"status": "error", "error_code": code}


def decode(data: bytes):
    """음성 파일을 16kHz mono float32 PCM으로 바꾼다. 실패하면 None을 반환한다."""
    import numpy as np

    # m4a는 moov atom이 파일 끝에 있을 수 있어 파이프 대신 파일로 넘긴다.
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source"
        source.write_bytes(data)
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                # 긴 파일을 전부 디코딩하지 않도록 최대 길이 초과를 판별할 만큼만 읽는다.
                "-t",
                str(MAX_SECONDS + 1),
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-f",
                "f32le",
                "pipe:1",
            ],
            capture_output=True,
        )
    if result.returncode != 0:
        logger.warning("embed_speaker decode_failed: ffmpeg exit %s", result.returncode)
        return None
    # frombuffer는 읽기 전용 배열이라 torch 변환 경고가 나지 않도록 복사한다.
    return np.frombuffer(result.stdout, dtype=np.float32).copy()


@app.function(image=image, cpu=4, memory=4096, timeout=60)
@modal.concurrent(max_inputs=4)
def embed_speaker(audio_url: str) -> dict:
    """presigned HTTPS GET URL의 사전 녹음을 받아 등록 벡터 또는 오류 코드를 반환한다.

    URL과 서명 쿼리는 로그에 남기지 않는다. 예외 메시지에 URL이 섞일 수 있어 예외 타입만 기록한다.
    """
    try:
        if not audio_url.startswith("https://"):
            raise ValueError("https URL이 아닙니다.")
        with urllib.request.urlopen(audio_url, timeout=30) as response:
            data = response.read(MAX_BYTES + 1)
    except Exception as e:
        logger.warning("embed_speaker download_failed: %s", type(e).__name__)
        return _error("download_failed")
    if len(data) > MAX_BYTES:
        logger.warning("embed_speaker invalid_audio: too_large")
        return _error("invalid_audio")

    pcm = decode(data)
    if pcm is None:
        return _error("invalid_audio")
    seconds = len(pcm) / SAMPLE_RATE
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        logger.warning("embed_speaker invalid_audio: duration %.1fs", seconds)
        return _error("invalid_audio")

    try:
        embedding = embed(pcm)
    except Exception as e:
        logger.warning("embed_speaker inference_failed: %s", type(e).__name__)
        return _error("inference_failed")
    return {"status": "ok", "model_version": MODEL_VERSION, "embedding": embedding}
