"""Nemotron·Qwen의 GPU 상태를 복원해 인증된 WebSocket으로 제공한다."""

import os
import uuid

import modal

from meety_ai.modal_app.redimnet import PACKAGE, WEIGHTS_COMMANDS
from meety_ai.modal_app.stt.pipeline import MAX_SESSIONS

app = modal.App("meety-realtime-stt")
cache = modal.Volume.from_name("meety-stt-learning-model-cache", create_if_missing=True)
image = (
    modal.Image.from_registry("nvidia/cuda:12.8.0-cudnn-devel-ubuntu22.04", add_python="3.12")
    .apt_install("ffmpeg", "libsndfile1", "git", "curl")
    .uv_pip_install(
        "qwen-asr[vllm]==0.0.6",
        "vllm==0.14.0",
        "torch==2.9.1",
        "transformers==4.57.6",
        "fastapi==0.141.1",
        # ReDimNet2+ 저자 코드가 import 시점에 hydra를 쓴다. torchaudio·scipy는 기존 버전을 쓴다.
        "hydra-core==1.3.8",
        "omegaconf==2.3.1",
    )
    .uv_pip_install(PACKAGE, extra_options="--no-deps")
    .run_commands(*WEIGHTS_COMMANDS)
    .run_commands(
        "python -m venv /opt/nemotron --system-site-packages",
        "/opt/nemotron/bin/python -m pip install "
        "git+https://github.com/huggingface/transformers.git@27166ea03f12c940f23176a904ab1d2ff1a3dcbb",
    )
    .env(
        {
            "HF_HOME": "/cache",
            "VLLM_WORKER_MULTIPROC_METHOD": "spawn",
            "VLLM_ENABLE_V1_MULTIPROCESSING": "0",
            "TOKENIZERS_PARALLELISM": "false",
            "NEMOTRON_REVISION": "f667ed73aee57d40cc39428eb768b4fd87a0a29e",
        }
    )
    .add_local_python_source("meety_ai", copy=True)
)


@app.cls(
    image=image,
    gpu="A10",
    cpu=4,
    memory=16384,
    timeout=6000,
    startup_timeout=900,
    scaledown_window=60,
    volumes={"/cache": cache},
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
@modal.concurrent(max_inputs=MAX_SESSIONS)
class TranscriptionWorker:
    @modal.enter(snap=True)
    def load(self):
        import torch

        from meety_ai.modal_app.stt.pipeline import InferenceRuntime

        self.runtime = InferenceRuntime()
        self.runtime.metadata["modelLoadId"] = self.runtime.metadata["bootId"]
        # 단일 GPU의 NCCL 감시 스레드가 복원 뒤 이전 TCPStore를 참조하지 않게 한다.
        if torch.distributed.is_initialized():
            torch.distributed.destroy_process_group()
        cache.commit()

    @modal.enter(snap=False)
    def restore(self):
        self.runtime.start_runtime()
        self.runtime.metadata["bootId"] = uuid.uuid4().hex
        self.runtime.metadata["containerId"] = os.environ.get("MODAL_TASK_ID")

    @modal.asgi_app(requires_proxy_auth=True)
    def web(self):
        from meety_ai.modal_app.stt.pipeline import create_inference_app

        return create_inference_app(self.runtime)

    @modal.exit()
    def cleanup(self):
        self.runtime.close()
