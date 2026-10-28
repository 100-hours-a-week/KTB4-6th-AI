"""사전 녹음에서 ReDimNet2+ 등록 벡터를 Modal CPU에서 추출한다.

배포: modal deploy -m meety_ai.modal_app.speaker_embedding

분석 서비스는 배포된 함수를 Modal Python SDK로 호출한다.
    modal.Function.from_name("meety-speaker-embedding", "embed_speaker").remote(audio_url)

회의 구간 벡터도 `embed`로 만들어야 등록 벡터와 비교할 수 있다.
"""

import functools
import logging
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import modal

MODEL_REVISION = "fd45d1968c0ea91b62ad0d2469eee4607756bb07"
WEIGHTS_SHA256 = "57056ea3ff1afcf6d825f0f44abc0a4c68fcdd6738ec36e23017df060712f02b"
# 가중치를 strict 로딩할 수 있는 저자 저장소(github.com/lab260ru/redimnet2-plus) 커밋.
CODE_REVISION = "2a8d6dbd6d85e337a20b1d74ed3323c44bff86c5"
# 가중치 revision과 전처리 규칙을 함께 식별한다. 전처리가 바뀌면 끝 번호를 올린다.
MODEL_VERSION = "redimnet2-plus-fd45d19-v1"
EMBEDDING_DIM = 192
SAMPLE_RATE = 16000
MIN_SECONDS = 10
MAX_SECONDS = 60
MAX_BYTES = 20 * 1024 * 1024
WEIGHTS_PATH = "/models/redimnet2-plus.bin"
CODE_PATH = "/opt/redimnet2-plus"

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
    .run_commands(
        f"git clone https://github.com/lab260ru/redimnet2-plus.git {CODE_PATH}",
        f"git -C {CODE_PATH} checkout {CODE_REVISION}",
        "mkdir -p /models",
        "curl -fsSL -o /models/redimnet2-plus.bin "
        f"https://huggingface.co/lab260/redimnet2-plus/resolve/{MODEL_REVISION}/pytorch_model_fsdp.bin",
        # 가중치가 바뀌면 빌드를 실패시켜 다른 벡터 공간이 섞이지 않게 한다.
        f"echo '{WEIGHTS_SHA256}  {WEIGHTS_PATH}' | sha256sum -c",
    )
)
app = modal.App("meety-speaker-embedding")


def _error(code: str) -> dict:
    return {"status": "error", "error_code": code}


@functools.cache
def _load_model():
    """컨테이너가 유지되는 동안 모델을 재사용한다. 실패는 캐시되지 않아 다음 호출에서 재시도한다."""
    import torch

    sys.path.insert(0, CODE_PATH)
    from asv.models.redimnet2 import ReDimNet2Encoder

    state = torch.load(WEIGHTS_PATH, map_location="cpu", weights_only=True)
    state = {k.replace("_orig_mod.", "").replace("module.", ""): v for k, v in state.items()}
    encoder = {k.removeprefix("encoder."): v for k, v in state.items() if k.startswith("encoder.")}
    # 고정한 체크포인트의 head 모양(224채널·2D 출력)에 맞춘 설정이다.
    model = ReDimNet2Encoder(
        model_name="b6",
        pretrained=False,
        model_overrides={"out_channels": 224, "return_2d_output": True},
    )
    model.load_state_dict(encoder, strict=True)
    return model.eval()


def embed(pcm) -> list[float]:
    """16kHz mono float32 PCM 전체에서 L2 정규화한 192차원 화자 벡터를 만든다."""
    import numpy as np
    import torch

    model = _load_model()
    with torch.inference_mode():
        x = torch.from_numpy(np.asarray(pcm, dtype=np.float32)).unsqueeze(0)
        vector = model(model.spec_frontend(x)).squeeze(0).numpy()
    norm = np.linalg.norm(vector)
    if vector.shape != (EMBEDDING_DIM,) or not np.isfinite(vector).all() or norm <= 0:
        raise ValueError("유효하지 않은 임베딩입니다.")
    return (vector / norm).tolist()


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
