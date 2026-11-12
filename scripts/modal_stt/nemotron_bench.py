"""Nemotron을 FP32 eager 기준과 CUDA graph(FP32·TF32·BF16)로 실행해 결과 일치·속도를 비교한다.

사용: uv run python scripts/modal_stt/nemotron_bench.py --input <WAV> --output <json>
컨테이너 안에서는 Nemotron 전용 venv로 `--inner`를 실행한다.
"""

import json
import sys
import time

SESSIONS = 8
STEPS = 150
TIMING_STEPS = 30
SHIFT_SECONDS = 37
WARMUP_STEPS = 60


def inner(wav_path):
    import copy
    import wave

    import numpy as np
    import torch

    sys.path.insert(0, "/root")
    from meety_ai.modal_app.stt import nemotron_worker as worker

    processor, fp32_model, geometry, _ = worker.load()
    bf16_model = copy.deepcopy(fp32_model).to(torch.bfloat16)
    with wave.open(wav_path, "rb") as source:
        audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    audio = audio.astype(np.float32) / 32768.0

    def set_tf32(enabled):
        torch.backends.cuda.matmul.allow_tf32 = enabled
        torch.backends.cudnn.allow_tf32 = enabled

    # 이름: (모델, TF32 사용, graph). FP32 eager를 기준으로 나머지를 비교한다.
    modes = {
        "fp32": (fp32_model, False, None),
        "fp32_graph": (fp32_model, False, worker.GraphedHead(fp32_model)),
        "tf32_graph": (fp32_model, True, worker.GraphedHead(fp32_model)),
        "bf16_graph": (bf16_model, False, worker.GraphedHead(bf16_model)),
    }
    candidates = [name for name in modes if name != "fp32"]

    class Stream:
        """파이프라인 `_pump`와 같은 규칙으로 회의 하나의 청크를 차례로 자른다."""

        def __init__(self, index, model, tf32, head):
            shift = index * SHIFT_SECONDS * 16000 % len(audio)
            self.audio = np.concatenate([audio[shift:], audio[:shift]])
            self.model, self.tf32, self.head = model, tf32, head
            self.next_sample = self.mel_cursor = 0
            self.first = True
            self.cache = None

        def step(self):
            size = geometry["first_samples" if self.first else "chunk_samples"]
            if self.next_sample + size > len(self.audio):
                raise RuntimeError("측정 입력이 짧습니다.")
            chunk = self.audio[self.next_sample : self.next_sample + size]
            set_tf32(self.tf32)
            if self.head is None:
                logits, self.cache = worker.single_step(
                    processor, self.model, chunk, first=self.first, last=False, cache=self.cache
                )
            else:
                logits, self.cache = worker.graph_step(
                    processor, self.model, self.head, chunk, first=self.first, cache=self.cache
                )
            self.first = False
            self.next_sample = processor.audio_chunk_start(self.mel_cursor + geometry["mel_step"])
            self.mel_cursor += geometry["mel_step"]
            return logits

    result = {"equivalence": {}, "timing": {}}
    with torch.inference_mode():
        streams = {
            name: [Stream(i, model, tf32, head) for i in range(SESSIONS)]
            for name, (model, tf32, head) in modes.items()
        }
        stats = {name: {"frames": 0, "equal": 0, "maxLogitDiff": 0.0} for name in modes}
        for _ in range(STEPS):
            for i in range(SESSIONS):
                want = streams["fp32"][i].step().float()
                expected = worker.to_active(want)
                for name in candidates:
                    got = streams[name][i].step().float()
                    actual = worker.to_active(got)
                    entry = stats[name]
                    entry["frames"] += len(expected)
                    entry["equal"] += sum(a == b for a, b in zip(expected, actual, strict=True))
                    entry["maxLogitDiff"] = max(
                        entry["maxLogitDiff"], (want - got).abs().max().item()
                    )
        for name in candidates:
            entry, head = stats[name], modes[name][2]
            result["equivalence"][name] = {
                "frames": entry["frames"],
                "agreement": entry["equal"] / entry["frames"],
                "maxLogitAbsDiff": entry["maxLogitDiff"],
                "graphs": len(head.graphs),
                "captureSeconds": head.capture_seconds,
            }
        print(json.dumps(result), file=sys.stderr, flush=True)

        # cache 압축까지 끝난 정상 상태에서 청크 1개 처리 시간(mel 계산·cache 갱신 포함)을 잰다.
        for name in modes:
            stream = streams[name][0]
            runs = []
            for _ in range(TIMING_STEPS):
                torch.cuda.synchronize()
                started = time.perf_counter()
                worker.to_active(stream.step())
                torch.cuda.synchronize()
                runs.append(time.perf_counter() - started)
            runs.sort()
            result["timing"][name] = {
                "medianSeconds": runs[len(runs) // 2],
                "p90Seconds": runs[int(len(runs) * 0.9)],
            }
        base = result["timing"]["fp32"]["medianSeconds"]
        for name in modes:
            result["timing"][name]["speedupVsFp32"] = base / result["timing"][name]["medianSeconds"]
    print(json.dumps(result), flush=True)


def profile(wav_path):
    """정상 상태 청크 처리에서 GPU 계산·CPU·kernel 수가 각각 얼마인지 나눠 잰다."""
    import wave

    import numpy as np
    import torch
    from torch.autograd import DeviceType
    from torch.profiler import ProfilerActivity
    from torch.profiler import profile as torch_profile

    sys.path.insert(0, "/root")
    from meety_ai.modal_app.stt import nemotron_worker as worker

    processor, model, geometry, _ = worker.load()
    with wave.open(wav_path, "rb") as source:
        audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    audio = audio.astype(np.float32) / 32768.0
    state = {"next": 0, "mel": 0, "first": True, "cache": None}

    def take():
        size = geometry["first_samples" if state["first"] else "chunk_samples"]
        chunk = audio[state["next"] : state["next"] + size]
        first = state["first"]
        state["first"] = False
        state["next"] = processor.audio_chunk_start(state["mel"] + geometry["mel_step"])
        state["mel"] += geometry["mel_step"]
        return chunk, first

    def step():
        chunk, first = take()
        logits, state["cache"] = worker.single_step(
            processor, model, chunk, first=first, last=False, cache=state["cache"]
        )
        return worker.to_active(logits)

    result = {}
    with torch.inference_mode():
        for _ in range(WARMUP_STEPS):
            step()
        # 단계별 시간: mel 계산(CPU) / 모델 forward / active 변환
        phases = {"processor": [], "forward": [], "toActive": []}
        for _ in range(TIMING_STEPS):
            chunk, first = take()
            started = time.perf_counter()
            inputs = processor(
                chunk, sampling_rate=16000, is_streaming=True, is_first_audio_chunk=first
            ).to(model.device, dtype=model.dtype)
            torch.cuda.synchronize()
            mid = time.perf_counter()
            output = model(**inputs, speaker_cache=state["cache"])
            state["cache"] = output.speaker_cache
            torch.cuda.synchronize()
            end = time.perf_counter()
            worker.to_active(output.logits[0])
            phases["processor"].append(mid - started)
            phases["forward"].append(end - mid)
            phases["toActive"].append(time.perf_counter() - end)
        result["phaseMedianSeconds"] = {
            name: sorted(values)[len(values) // 2] for name, values in phases.items()
        }

        steps = 10
        torch.cuda.synchronize()
        with torch_profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA]) as prof:
            started = time.perf_counter()
            for _ in range(steps):
                step()
            torch.cuda.synchronize()
            wall = time.perf_counter() - started
        kernels = [e for e in prof.events() if e.device_type == DeviceType.CUDA]
        result["profile"] = {
            "wallSecondsPerStep": wall / steps,
            "gpuKernelSecondsPerStep": sum(e.self_device_time_total for e in kernels) / 1e6 / steps,
            "kernelsPerStep": len(kernels) / steps,
        }
        result["profile"]["gpuBusyShare"] = (
            result["profile"]["gpuKernelSecondsPerStep"] / result["profile"]["wallSecondsPerStep"]
        )
        result["topCpuOps"] = prof.key_averages().table(sort_by="self_cpu_time_total", row_limit=15)
        result["topGpuOps"] = prof.key_averages().table(
            sort_by="self_device_time_total", row_limit=10
        )
    print(json.dumps(result), flush=True)


if __name__ == "__main__" and "--inner" in sys.argv:
    wav = sys.argv[sys.argv.index("--inner") + 1]
    profile(wav) if "--profile" in sys.argv else inner(wav)
    sys.exit(0)

import argparse  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import modal  # noqa: E402

from meety_ai.modal_app.stt.app import cache, image  # noqa: E402

app = modal.App("meety-nemotron-bench")
REMOTE_SCRIPT = "/root/nemotron_bench.py"
REMOTE_INPUT = "/root/bench-input.wav"


def build_image(input_path):
    return image.add_local_file(__file__, REMOTE_SCRIPT).add_local_file(input_path, REMOTE_INPUT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", action="store_true", help="정밀도 비교 대신 시간 구성 측정")
    args = parser.parse_args()
    extra = ["--profile"] if args.profile else []

    @app.function(
        image=build_image(args.input),
        gpu="A10",
        cpu=4,
        memory=16384,
        timeout=1800,
        volumes={"/cache": cache},
        serialized=True,
    )
    def bench():
        result = subprocess.run(
            ["/opt/nemotron/bin/python", REMOTE_SCRIPT, "--inner", REMOTE_INPUT, *extra],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-6000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    with modal.enable_output(), app.run():
        result = bench.remote()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
