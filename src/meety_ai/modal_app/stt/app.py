"""Nemotron과 Qwen을 같은 A10에 올려 인증된 WebSocket으로 제공한다."""

import modal

app = modal.App("meety-realtime-stt")
cache = modal.Volume.from_name("meety-stt-learning-model-cache", create_if_missing=True)
image = (
    modal.Image.from_registry("nvidia/cuda:12.8.0-cudnn-devel-ubuntu22.04", add_python="3.12")
    .apt_install("ffmpeg", "libsndfile1", "git")
    .uv_pip_install(
        "qwen-asr[vllm]==0.0.6",
        "vllm==0.14.0",
        "torch==2.9.1",
        "transformers==4.57.6",
        "fastapi==0.141.1",
    )
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
)
@modal.concurrent(max_inputs=1)
class TranscriptionWorker:
    @modal.enter()
    def load(self):
        from meety_ai.modal_app.stt.pipeline import InferenceRuntime

        self.runtime = InferenceRuntime()
        self.runtime.start_runtime()
        cache.commit()

    @modal.asgi_app(requires_proxy_auth=True)
    def web(self):
        from meety_ai.modal_app.stt.pipeline import create_inference_app

        return create_inference_app(self.runtime)

    @modal.exit()
    def cleanup(self):
        self.runtime.close()
