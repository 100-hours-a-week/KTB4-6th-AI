"""등록 API와 회의 STT가 같은 벡터 공간을 쓰도록 ReDimNet2+ 추출을 공유한다.

Modal에 의존하지 않으므로 각 앱의 이미지에 PACKAGE를 --no-deps로 설치하고
WEIGHTS_COMMANDS를 실행해 코드·가중치를 준비한다.
"""

import functools

MODEL_REVISION = "fd45d1968c0ea91b62ad0d2469eee4607756bb07"
WEIGHTS_SHA256 = "57056ea3ff1afcf6d825f0f44abc0a4c68fcdd6738ec36e23017df060712f02b"
# 가중치를 strict 로딩할 수 있는 저자 저장소(github.com/lab260ru/redimnet2-plus) 커밋.
CODE_REVISION = "2a8d6dbd6d85e337a20b1d74ed3323c44bff86c5"
# 원본 삭제·히스토리 변경에 대비해 수정 없이 보존한 포크에서 같은 커밋을 설치한다.
# 학습용 의존성이 기존 환경을 바꾸지 않도록 --no-deps로 설치하고, import에 필요한
# hydra-core·omegaconf·torchaudio·scipy는 각 이미지가 고정한다.
PACKAGE = f"asv @ git+https://github.com/SungjinWi99/redimnet2-plus.git@{CODE_REVISION}"
# 가중치 revision과 전처리 규칙을 함께 식별한다. 전처리가 바뀌면 끝 번호를 올린다.
MODEL_VERSION = "redimnet2-plus-fd45d19-v1"
EMBEDDING_DIM = 192
WEIGHTS_PATH = "/models/redimnet2-plus.bin"
# 이미지 빌드에 git(PACKAGE)·curl이 필요하다.
WEIGHTS_COMMANDS = [
    "mkdir -p /models",
    f"curl -fsSL -o {WEIGHTS_PATH} "
    f"https://huggingface.co/lab260/redimnet2-plus/resolve/{MODEL_REVISION}/pytorch_model_fsdp.bin",
    # 가중치가 바뀌면 빌드를 실패시켜 다른 벡터 공간이 섞이지 않게 한다.
    f"echo '{WEIGHTS_SHA256}  {WEIGHTS_PATH}' | sha256sum -c",
]


@functools.cache
def load_model(device: str = "cpu"):
    """장치별 모델을 컨테이너가 유지되는 동안 재사용한다. 실패는 캐시되지 않아 다시 시도한다."""
    import torch
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
    return model.eval().to(device)


def embed(pcm, device: str = "cpu") -> list[float]:
    """16kHz mono float32 PCM 전체에서 L2 정규화한 192차원 화자 벡터를 만든다."""
    import numpy as np
    import torch

    model = load_model(device)
    with torch.inference_mode():
        x = torch.from_numpy(np.asarray(pcm, dtype=np.float32)).unsqueeze(0).to(device)
        vector = model(model.spec_frontend(x)).squeeze(0).cpu().numpy()
    norm = np.linalg.norm(vector)
    if vector.shape != (EMBEDDING_DIM,) or not np.isfinite(vector).all() or norm <= 0:
        raise ValueError("유효하지 않은 임베딩입니다.")
    return (vector / norm).tolist()
