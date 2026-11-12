"""별도 Transformers 환경에서 세션별 Nemotron 캐시를 유지한다."""

import base64
import contextlib
import importlib.metadata
import json
import os
import sys
import time
from pathlib import Path

# 정상 상태 FIFO 주기(약 25스텝)를 포함해 나올 수 있는 입력 길이를 모두 지나도록 충분히 돌린다.
PRECAPTURE_STEPS = 200


def load():
    """processor·모델과 파이프라인이 쓰는 청크 규격을 준비한다."""
    import torch
    from huggingface_hub import snapshot_download
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor

    snapshot = snapshot_download(
        "nvidia/Nemotron-3-Diarization",
        revision=os.environ["NEMOTRON_REVISION"],
        allow_patterns=["*.json", "*.safetensors"],
    )
    processor = AutoProcessor.from_pretrained(snapshot)
    processor.set_streaming_mode("low_latency")
    model = AutoModelForAudioFrameClassification.from_pretrained(snapshot).to("cuda").eval()
    # 2026-11-04 A10 실측: CUDA graph와 함께 쓰면 청크당 18.6ms→9.9ms, 화자 프레임 99.99% 일치.
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    feature = processor.feature_extractor
    geometry = {
        "first_samples": processor.num_samples_first_audio_chunk,
        "chunk_samples": processor.num_samples_per_audio_chunk,
        "mel_step": processor.num_mel_frames_per_step,
        "frame_samples": feature.hop_length,
        "latency_ms": processor.streaming_latency_ms,
    }
    if feature.sampling_rate != 16000 or geometry["frame_samples"] != 160:
        raise RuntimeError("Nemotron 음성·프레임 규격이 설계와 다릅니다.")
    return processor, model, geometry, Path(snapshot).name


class GraphedHead:
    """인코더~분류기를 입력 길이별 CUDA graph로 재생해 kernel 약 970개의 CPU 실행 비용을 없앤다.

    FIFO 주기에 따라 길이가 바뀌므로 길이가 처음 나올 때 한 번 녹화하고 이후 재생한다.
    graph들은 메모리 pool을 공유하므로 출력은 다음 재생 전에 복사해 돌려준다.
    """

    def __init__(self, model):
        import torch

        self.model = model
        self.graphs = {}
        self.pool = None
        self.capture_seconds = 0.0
        self.positions = torch.arange(4096, device=model.device)[None, :]

    def _forward(self, inputs_embeds):
        hidden = self.model.model(
            inputs_embeds=inputs_embeds, position_ids=self.positions[:, : inputs_embeds.shape[1]]
        ).last_hidden_state
        return self.model.classifier(hidden)

    def __call__(self, inputs_embeds):
        import torch

        length = inputs_embeds.shape[1]
        entry = self.graphs.get(length)
        if entry is None:
            started = time.perf_counter()
            static_input = inputs_embeds.clone()
            side = torch.cuda.Stream()
            side.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(side):
                for _ in range(2):
                    self._forward(static_input)
            torch.cuda.current_stream().wait_stream(side)
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph, pool=self.pool):
                static_output = self._forward(static_input)
            self.pool = graph.pool()
            entry = self.graphs[length] = (graph, static_input, static_output)
            self.capture_seconds += time.perf_counter() - started
        graph, static_input, static_output = entry
        static_input.copy_(inputs_embeds)
        graph.replay()
        return static_output.clone()


def single_step(processor, model, audio, *, first, last, cache):
    """청크 하나를 모델 기본 forward로 처리한다. 길이가 다른 마지막 청크에 쓴다."""
    import numpy as np

    # 매우 짧은 EOF도 STFT 창을 만들되 실제 음성 밖의 프레임은 호출자가 제외한다.
    if last and len(audio) < processor.feature_extractor.n_fft:
        audio = np.pad(audio, (0, processor.feature_extractor.n_fft - len(audio)))
    inputs = processor(
        audio,
        sampling_rate=16000,
        is_streaming=True,
        is_first_audio_chunk=first,
        is_last_audio_chunk=last,
    ).to(model.device, dtype=model.dtype)
    result = model(**inputs, speaker_cache=cache)
    if not last and result.speaker_cache is None:
        raise RuntimeError("Nemotron 중간 청크의 캐시가 없습니다.")
    return result.logits[0], result.speaker_cache


def graph_step(processor, model, head, audio, *, first, cache):
    """마지막이 아닌 청크를 스트리밍 forward와 같은 순서로 처리하되 인코더는 graph로 재생한다."""
    import torch
    from transformers.models.nemotron3_diarization.modeling_nemotron3_diarization import (
        Nemotron3DiarizationSpeakerCache,
    )

    inputs = processor(
        audio, sampling_rate=16000, is_streaming=True, is_first_audio_chunk=first
    ).to(model.device, dtype=model.dtype)
    if cache is None:
        cache = Nemotron3DiarizationSpeakerCache(model.config.streaming_config)
    embeds = model.model.audio_tower.embedder(inputs["input_features"])
    num_chunk = embeds.shape[1] - inputs["num_lookahead_frames"]
    cached = cache.get_embeds(embeds)
    chunk_input = torch.cat([cached, embeds], dim=1)
    logits = head(chunk_input)
    cache.update(chunk_input, logits, model.silence_embeds, num_chunk)
    factor = model.config.audio_config.subsampling_factor
    start = cached.shape[1] * factor
    return logits[0, start : start + num_chunk * factor], cache


def to_active(logits):
    """프레임마다 확률이 가장 높은 화자 하나만 0.5 초과일 때 활성으로 표시한다."""
    import torch

    probabilities = logits.sigmoid()
    confidence, speaker = probabilities.max(dim=-1)
    active = torch.zeros_like(probabilities, dtype=torch.uint8)
    active.scatter_(1, speaker[:, None], (confidence > 0.5).to(torch.uint8)[:, None])
    return active.cpu().tolist()


def precapture(processor, model, head, geometry):
    """첫 회의가 녹화 지연을 겪지 않게 무음 세션으로 나올 수 있는 길이의 graph를 미리 만든다.

    입력 길이는 cache·FIFO 크기로만 정해져 음성 내용과 무관하다.
    """
    import numpy as np

    cache = None
    for step in range(PRECAPTURE_STEPS):
        size = geometry["first_samples" if step == 0 else "chunk_samples"]
        _, cache = graph_step(
            processor, model, head, np.zeros(size, dtype=np.float32), first=step == 0, cache=cache
        )


def main(output):
    import numpy as np
    import torch

    processor, model, geometry, revision = load()
    head = GraphedHead(model)
    with torch.inference_mode():
        precapture(processor, model, head, geometry)
    print(
        json.dumps(
            {
                "ready": True,
                "geometry": geometry,
                "revision": revision,
                "transformers": importlib.metadata.version("transformers"),
                "cudaGraphs": len(head.graphs),
                "captureSeconds": head.capture_seconds,
            }
        ),
        file=output,
        flush=True,
    )
    caches = {}
    for line in sys.stdin:
        request = json.loads(line)
        key = request["session"]
        if request["op"] == "release":
            caches.pop(key, None)
            response = {"released": True}
        else:
            pcm = base64.b64decode(request["pcm"], validate=True)
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            if not request["first"] and key not in caches:
                raise RuntimeError("연속 세션의 화자 캐시가 없습니다.")
            with torch.inference_mode():
                if request["last"]:
                    logits, _ = single_step(
                        processor,
                        model,
                        audio,
                        first=request["first"],
                        last=True,
                        cache=caches.pop(key, None),
                    )
                else:
                    logits, caches[key] = graph_step(
                        processor,
                        model,
                        head,
                        audio,
                        first=request["first"],
                        cache=caches.get(key),
                    )
                response = {
                    "active": to_active(logits),
                    "next_start": processor.audio_chunk_start(
                        request["mel_cursor"] + geometry["mel_step"]
                    ),
                }
        print(json.dumps(response), file=output, flush=True)


if __name__ == "__main__":
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        main(output)
