"""운영 STT worker 구성을 A10 한 대에 띄우고 동시 회의 수를 늘려 포화점을 찾는다.

모델은 한 컨테이너에 한 번만 적재하고, 같은 컨테이너에서 단계를 이어 측정한다.
구간 끝→수신 p95가 SLO를 넘거나 회의가 실패한 첫 단계에서 멈춘다.
GPU 처리 용량만 재려고 Live AI를 거치지 않고 Modal WebSocket에 직접 연결한다.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import random
import subprocess
import threading
import time
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path

import httpx
import modal
from websockets.asyncio.client import connect

from meety_ai.modal_app.stt.app import cache, image

SLO_P95_SECONDS = 10.0
MAX_LIMIT = 40
INPUT_SECONDS = 300
CHUNK_BYTES = 3200  # 100ms
PROFILES_PER_MEETING = 4
# 회의마다 session.start에 넣는 STT 맥락. 운영에서는 Qwen 프롬프트 앞(system)에 붙는다.
CONTEXT = ""
RESTORE_ATTEMPTS = 5
# 로컬과 컨테이너가 같은 실험 설정을 읽도록 이미지 환경 변수로 전달한다.
SNAPSHOT = os.environ.get("SWEEP_SNAPSHOT") == "1"
# Qwen 배치 상한(vLLM max_num_seqs). 비우면 운영 값을 쓴다.
ASR_BATCH = os.environ.get("SWEEP_ASR_BATCH", "")
# 운영 앱 이름과 겹치지 않게 한다. snapshot은 배포 앱에서만 만들어진다.
# 여러 구성을 동시에 측정할 때 배포 앱이 겹치지 않게 접미사를 붙인다.
app = modal.App("meety-stt-capacity-sweep" + os.environ.get("SWEEP_APP_SUFFIX", ""))


@app.cls(
    image=image.env(
        {
            "SWEEP_SNAPSHOT": "1" if SNAPSHOT else "0",
            "SWEEP_ASR_BATCH": ASR_BATCH,
        }
    ),
    enable_memory_snapshot=SNAPSHOT,
    experimental_options={"enable_gpu_snapshot": True} if SNAPSHOT else None,
    gpu="A10",
    cpu=4,
    memory=16384,
    timeout=6000,
    startup_timeout=900,
    scaledown_window=60,
    max_containers=1,
    volumes={"/cache": cache},
)
# 측정 연결 외에 통계 요청도 같은 컨테이너로 들어와야 한다.
@modal.concurrent(max_inputs=MAX_LIMIT + 4)
class SweepWorker:
    # 운영 TranscriptionWorker와 같게 모델 적재는 snapshot 전, 실행 상태는 복원 후 만든다.
    # snapshot이 없으면 두 hook을 정의 순서대로 이어서 실행한다.
    @modal.enter(snap=SNAPSHOT)
    def load(self):
        import meety_ai.modal_app.stt.pipeline as pipeline

        # 입장 한도와 배치 상한만 바꾸고 나머지 모델·구간 정책은 운영 값을 쓴다.
        pipeline.MAX_SESSIONS = MAX_LIMIT
        if ASR_BATCH:
            pipeline.BATCH_SIZE = int(ASR_BATCH)
        load_started = time.perf_counter()
        self.runtime = runtime = pipeline.InferenceRuntime()
        runtime.metadata.update(
            experimentMaxSessions=MAX_LIMIT,
            asrBatchSize=pipeline.BATCH_SIZE,
            snapshotEnabled=SNAPSHOT,
            loadSeconds=time.perf_counter() - load_started,
            # snapshot에서 복원한 컨테이너는 이 값이 적재한 컨테이너의 bootId와 같다.
            modelLoadId=runtime.metadata["bootId"],
        )

        original_rpc = runtime._rpc

        def timed_rpc(request):
            started = time.perf_counter()
            response = original_rpc(request)
            self.diar.append(
                {
                    "t": started - self.t0,
                    "rpcSeconds": time.perf_counter() - started,
                }
            )
            return response

        runtime._rpc = timed_rpc
        original_diarize = runtime.diarize

        async def timed_diarize(session, pcm, *, last):
            # 회의 청크 하나가 잠금 대기·Nemotron 호출을 거쳐 결과를 받기까지의 시간이다.
            started = time.perf_counter()
            response = await original_diarize(session, pcm, last=last)
            self.diar_calls.append(
                {"t": started - self.t0, "seconds": time.perf_counter() - started}
            )
            return response

        runtime.diarize = timed_diarize
        original_transcribe = runtime._transcribe

        def timed_transcribe(jobs):
            started = time.perf_counter()
            try:
                return original_transcribe(jobs)
            finally:
                self.batches.append(
                    {
                        "t": started - self.t0,
                        "size": len(jobs),
                        "seconds": time.perf_counter() - started,
                    }
                )

        runtime._transcribe = timed_transcribe
        original_embed = runtime.embed_segment

        def timed_embed(session, pcm):
            started = time.perf_counter()
            result = original_embed(session, pcm)
            self.embeds.append({"t": started - self.t0, "seconds": result[1]})
            return result

        runtime.embed_segment = timed_embed
        if SNAPSHOT:
            import torch

            # 운영과 같이 단일 GPU의 NCCL 감시 스레드가 복원 뒤 이전 TCPStore를 참조하지 않게 한다.
            if torch.distributed.is_initialized():
                torch.distributed.destroy_process_group()
        cache.commit()

    @modal.enter(snap=False)
    def restore(self):
        self.runtime.start_runtime()
        self.runtime.metadata.update(
            bootId=uuid.uuid4().hex, containerId=os.environ.get("MODAL_TASK_ID")
        )
        self.reset()
        self.stop = threading.Event()
        self.sampler = threading.Thread(target=self.sample_gpu, daemon=True)
        self.sampler.start()

    def reset(self):
        self.t0 = time.perf_counter()
        self.diar, self.diar_calls, self.batches, self.embeds, self.gpu = [], [], [], [], []

    def sample_gpu(self):
        while not self.stop.is_set():
            try:
                out = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=memory.used,utilization.gpu",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=True,
                ).stdout
                used, util = map(float, out.strip().split(","))
                self.gpu.append(
                    {"t": time.perf_counter() - self.t0, "usedMiB": used, "utilPercent": util}
                )
            except Exception as error:
                self.gpu.append({"errorType": type(error).__name__})
            self.stop.wait(1)

    @modal.asgi_app(requires_proxy_auth=True)
    def web(self):
        from meety_ai.modal_app.stt.pipeline import create_inference_app

        inference = create_inference_app(self.runtime)

        @inference.post("/sweep/reset")
        async def reset():
            if self.runtime.sessions:
                return {"ok": False, "activeSessions": len(self.runtime.sessions)}
            self.reset()
            return {"ok": True}

        @inference.get("/sweep/stats")
        async def stats():
            return {
                "metadata": self.runtime.metadata,
                "failure": self.runtime.failure,
                "activeSessions": len(self.runtime.sessions),
                "wallSeconds": time.perf_counter() - self.t0,
                "diar": self.diar,
                "diarCalls": self.diar_calls,
                "batches": self.batches,
                "embeds": self.embeds,
                "gpu": self.gpu,
            }

        return inference

    @modal.exit()
    def close(self):
        self.stop.set()
        self.runtime.close()


def percentile(values, p):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * p) - 1)] if values else None


def random_profiles(model_version, seed):
    """매핑 정확도가 아니라 운영과 같은 화자 벡터 추출 부하를 켜기 위한 임의 등록 벡터다."""
    rng = random.Random(seed)
    profiles = []
    for member in range(PROFILES_PER_MEETING):
        vector = [rng.gauss(0, 1) for _ in range(192)]
        norm = math.sqrt(sum(v * v for v in vector))
        profiles.append(
            {
                "teamMemberId": member + 1,
                "speakerEmbedding": {
                    "modelVersion": model_version,
                    "embedding": [v / norm for v in vector],
                },
            }
        )
    return profiles


async def replay(url, headers, pcm, session_id, model_version, ready, go):
    """회의 하나를 1배속으로 보내고 전사 수신 시각을 기록한다."""
    result = {"recordingSessionId": session_id, "status": "failed", "segments": [], "failed": 0}
    origin = stop_sent = None
    try:
        async with connect(url, additional_headers=headers, open_timeout=60, max_size=None) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "session.start",
                        "meetingId": session_id,
                        "recordingSessionId": session_id,
                        "speakerProfiles": random_profiles(model_version, session_id),
                        "context": CONTEXT,
                    }
                )
            )
            first = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if first["type"] != "session.ready":
                raise RuntimeError(f"준비 응답 불일치: {first}")
            ready.set()
            origin = await go

            async def feed():
                nonlocal stop_sent
                for offset in range(0, len(pcm), CHUNK_BYTES):
                    chunk = pcm[offset : offset + CHUNK_BYTES]
                    target = origin + (offset + len(chunk)) / 32000
                    await asyncio.sleep(max(0, target - time.perf_counter()))
                    await ws.send(chunk)
                stop_sent = time.perf_counter()
                await ws.send(json.dumps({"type": "session.stop"}))

            sender = asyncio.create_task(feed())
            try:
                while True:
                    event = json.loads(await asyncio.wait_for(ws.recv(), 120))
                    now = time.perf_counter() - origin
                    if event["type"] == "error":
                        result["error"] = event["code"]
                        break
                    if event["type"] == "segment.failed":
                        result["failed"] += 1
                    elif event["type"] == "segment.committed":
                        timings = event["timings"]
                        result["segments"].append(
                            {
                                "endedAtSeconds": event["endedAtMs"] / 1000,
                                "durationSeconds": (event["endedAtMs"] - event["startedAtMs"])
                                / 1000,
                                # 말을 시작한 뒤 전사를 보기까지. 구간이 길어지면 커진다.
                                "startToReceivedSeconds": now - event["startedAtMs"] / 1000,
                                "segmentId": event["segmentId"],
                                "content": event["content"],
                                "endToReceivedSeconds": now - event["endedAtMs"] / 1000,
                                "queueSeconds": timings["queueSeconds"],
                                "asrSeconds": timings["asrSeconds"],
                                "embeddingTimedOut": bool(timings.get("embeddingTimedOut")),
                            }
                        )
                    elif event["type"] == "session.ended":
                        result["drainSeconds"] = time.perf_counter() - stop_sent
                        result["status"] = "completed"
                        break
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    return result


def summarize(n, sessions, stats, slo=SLO_P95_SECONDS):
    """단계 결과를 집계하고 SLO 통과 여부를 판정한다."""
    segments = [s for session in sessions for s in session["segments"]]
    latencies = [s["endToReceivedSeconds"] for s in segments]
    # 60초 창별 p95가 뒤로 갈수록 커지면 처리 속도가 입력을 못 따라간다는 신호다.
    windows = {}
    for s in segments:
        windows.setdefault(int(s["endedAtSeconds"] // 60), []).append(s["endToReceivedSeconds"])
    diar, batches, embeds = stats["diar"], stats["batches"], stats["embeds"]
    wall = stats["wallSeconds"]
    gpu = [g for g in stats["gpu"] if "usedMiB" in g]
    completed = sum(s["status"] == "completed" for s in sessions)
    p95 = percentile(latencies, 0.95)
    return {
        "concurrency": n,
        "completedSessions": completed,
        "errors": sorted({s["error"] for s in sessions if "error" in s}),
        "workerFailure": stats["failure"],
        "committedSegments": len(segments),
        "failedSegments": sum(s["failed"] for s in sessions),
        "endToReceivedP50Seconds": percentile(latencies, 0.5),
        "endToReceivedP95Seconds": p95,
        "endToReceivedP99Seconds": percentile(latencies, 0.99),
        "endToReceivedMaxSeconds": max(latencies, default=None),
        "startToReceivedP95Seconds": percentile(
            [s["startToReceivedSeconds"] for s in segments if "startToReceivedSeconds" in s], 0.95
        ),
        "segmentSecondsMean": sum(s.get("durationSeconds", 0) for s in segments) / len(segments)
        if segments
        else None,
        "windowP95Seconds": [percentile(windows[k], 0.95) for k in sorted(windows)],
        "asrQueueP95Seconds": percentile([s["queueSeconds"] for s in segments], 0.95),
        "asrBatchP95Seconds": percentile([b["seconds"] for b in batches], 0.95),
        "asrBatchSizeMean": sum(b["size"] for b in batches) / len(batches) if batches else None,
        "asrBusyRatio": sum(b["seconds"] for b in batches) / wall,
        "diarRpcP95Seconds": percentile([d["rpcSeconds"] for d in diar], 0.95),
        "diarCallP95Seconds": percentile([c["seconds"] for c in stats["diarCalls"]], 0.95),
        # Nemotron 호출은 전역 잠금으로 직렬 실행되므로 1에 가까우면 포화다.
        "diarBusyRatio": sum(d["rpcSeconds"] for d in diar) / wall,
        "embedP95Seconds": percentile([e["seconds"] for e in embeds], 0.95),
        "embedBusyRatio": sum(e["seconds"] for e in embeds) / wall,
        "embeddingTimeouts": sum(s["embeddingTimedOut"] for s in segments),
        "gpuUtilMeanPercent": sum(g["utilPercent"] for g in gpu) / len(gpu) if gpu else None,
        "gpuPeakMiB": max((g["usedMiB"] for g in gpu), default=None),
        "drainMaxSeconds": max((s.get("drainSeconds") or 0 for s in sessions), default=0),
        "passed": completed == n and not stats["failure"] and p95 is not None and p95 <= slo,
    }


async def run_stage(base_url, headers, n, pcm, model_version, id_base):
    ws_url = base_url.replace("https://", "wss://") + "/transcribe"
    async with httpx.AsyncClient(headers=headers, timeout=60, follow_redirects=True) as client:
        reset = (await client.post(base_url + "/sweep/reset")).raise_for_status().json()
        if not reset["ok"]:
            raise RuntimeError(f"이전 단계 세션이 남아 있습니다: {reset}")
        loop = asyncio.get_running_loop()
        go = loop.create_future()
        tasks = []
        for index in range(n):
            # 녹음 하나를 회의마다 37초씩 순환 이동해 발화 종료 시점을 분산한다.
            shift = (index * 37 * 32000) % len(pcm)
            ready = asyncio.Event()
            tasks.append(
                asyncio.create_task(
                    replay(
                        ws_url,
                        headers,
                        pcm[shift:] + pcm[:shift],
                        id_base + index,
                        model_version,
                        ready,
                        go,
                    )
                )
            )
            # 연결 준비를 하나씩 확인한 뒤 음성은 동시에 보낸다.
            ready_wait = asyncio.create_task(ready.wait())
            await asyncio.wait([ready_wait, tasks[-1]], return_when=asyncio.FIRST_COMPLETED)
            if not ready.is_set():
                raise RuntimeError(f"{index + 1}번째 연결 준비 실패: {tasks[-1].result()}")
        go.set_result(time.perf_counter())
        sessions = await asyncio.gather(*tasks)
        async with asyncio.timeout(60):
            while (stats := (await client.get(base_url + "/sweep/stats")).json())["activeSessions"]:
                await asyncio.sleep(0.5)
    summary = summarize(n, sessions, stats)
    summary["containerId"] = stats["metadata"].get("containerId")
    return summary, sessions, stats


def write_report(path, summaries):
    head = (
        "| 회의 | 완료 | 끝→수신 p95 | 60초 창 p95 | ASR 큐 p95 | Nemotron 점유 | "
        "Nemotron 청크 p95 | RPC p95 | ASR 점유 | 벡터 timeout | GPU util | GPU MiB | 판정 |"
    )
    rows = [head, "|" + "---|" * 13]

    def f(x, digits=2):
        return f"{x:.{digits}f}" if isinstance(x, float) else str(x)

    for s in summaries:
        if "error" in s:
            rows.append(f"| {s['concurrency']} | 실험 오류: {s['error']} |" + " |" * 11)
            continue
        rows.append(
            "| "
            + " | ".join(
                [
                    str(s["concurrency"]),
                    f"{s['completedSessions']}/{s['concurrency']}",
                    f(s["endToReceivedP95Seconds"]),
                    " → ".join(f(x, 1) for x in s["windowP95Seconds"]),
                    f(s["asrQueueP95Seconds"]),
                    f(s["diarBusyRatio"]),
                    f(s["diarCallP95Seconds"]),
                    f(s["diarRpcP95Seconds"], 3),
                    f(s["asrBusyRatio"]),
                    str(s["embeddingTimeouts"]),
                    f(s["gpuUtilMeanPercent"], 0),
                    f(s["gpuPeakMiB"], 0),
                    "통과" if s["passed"] else "실패",
                ]
            )
            + " |"
        )
    path.write_text("\n".join(rows) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="16kHz mono s16le WAV")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stages", nargs="*", type=int, default=[10, 12, 14, 16, 18, 20])
    parser.add_argument("--context-file", type=Path, help="회의마다 보낼 STT 맥락 문서")
    args = parser.parse_args()
    global CONTEXT
    CONTEXT = args.context_file.read_text() if args.context_file else ""
    if max(args.stages, default=0) > MAX_LIMIT:
        parser.error(f"단계는 {MAX_LIMIT} 이하여야 합니다.")
    with wave.open(str(args.input), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
            raise ValueError("16kHz mono s16le 입력이 필요합니다.")
        pcm = source.readframes(INPUT_SECONDS * 16000)
    if len(pcm) != INPUT_SECONDS * 32000:
        raise ValueError(f"{INPUT_SECONDS}초 이상 PCM이 필요합니다.")
    args.output.mkdir(parents=True, exist_ok=False)

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))

    save(
        "manifest.json",
        {
            "startedUtc": datetime.now(UTC).isoformat(),
            "input": str(args.input.resolve()),
            "inputPcmSha256": hashlib.sha256(pcm).hexdigest(),
            "stages": args.stages,
            "sloP95Seconds": SLO_P95_SECONDS,
            "snapshot": SNAPSHOT,
            "asrBatchSize": ASR_BATCH or "운영 값",
            "contextChars": len(CONTEXT),
            "harnessSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    )
    workspace = modal.Workspace.from_context()
    token = workspace.proxy_tokens.create()
    headers = {"Modal-Key": token.token_id, "Modal-Secret": token.token_secret}
    summaries = []
    try:
        with modal.enable_output(), contextlib.ExitStack() as stack:
            if SNAPSHOT:
                app.deploy()
                stack.callback(
                    subprocess.run, ["modal", "app", "stop", "-y", app.app_id], check=False
                )
            else:
                stack.enter_context(app.run())
            base_url = SweepWorker().web.get_web_url()

            def get_stats():
                return (
                    httpx.get(
                        base_url + "/sweep/stats",
                        headers=headers,
                        timeout=900,
                        follow_redirects=True,
                    )
                    .raise_for_status()
                    .json()
                )

            print("모델 적재 대기", flush=True)
            stats = get_stats()
            if SNAPSHOT:
                # 처음 2~3개 컨테이너는 snapshot을 새로 만들므로 복원될 때까지 재시작한다.
                attempts, load_ids = [], {stats["metadata"]["modelLoadId"]}
                for _ in range(RESTORE_ATTEMPTS):
                    previous = stats["metadata"]
                    subprocess.run(
                        ["modal", "container", "stop", "-y", previous["containerId"]], check=True
                    )
                    stopped = time.perf_counter()
                    while True:
                        try:
                            stats = get_stats()
                        except httpx.HTTPError:
                            stats = None
                        if stats and stats["metadata"]["containerId"] != previous["containerId"]:
                            break
                        time.sleep(2)
                    current = stats["metadata"]
                    # 복원한 컨테이너는 앞선 컨테이너가 적재한 값을 그대로 갖는다.
                    restored = current["modelLoadId"] in load_ids
                    load_ids.add(current["modelLoadId"])
                    attempts.append(
                        {
                            "containerId": current["containerId"],
                            "stopToStatsSeconds": time.perf_counter() - stopped,
                            "restored": restored,
                        }
                    )
                    save("snapshot-restore.json", attempts)
                    print(json.dumps(attempts[-1]), flush=True)
                    if restored:
                        break
                else:
                    raise RuntimeError(f"{RESTORE_ATTEMPTS}회 재시작 동안 복원되지 않았습니다.")
            model_version = stats["metadata"]["speakerEmbeddingModel"]
            if model_version is None:
                raise RuntimeError("화자 벡터 모델이 없어 운영과 같은 부하를 만들 수 없습니다.")
            save("worker-metadata.json", stats["metadata"])
            # 첫 요청의 컴파일·캐시 비용을 측정에서 빼기 위해 1회의 35초를 먼저 처리한다.
            warm = asyncio.run(run_stage(base_url, headers, 1, pcm[: 35 * 32000], model_version, 1))
            if not warm[0]["completedSessions"]:
                raise RuntimeError(f"warmup 실패: {warm[0]}")
            containers = {warm[0]["containerId"]}
            for n in args.stages:
                print(f"{n}개 회의 시작", flush=True)
                try:
                    summary, sessions, stats = asyncio.run(
                        run_stage(base_url, headers, n, pcm, model_version, n * 100)
                    )
                except Exception as error:
                    summary = {"concurrency": n, "passed": False, "error": repr(error)}
                else:
                    containers.add(summary["containerId"])
                    save(f"stage-{n}-sessions.json", sessions)
                    save(f"stage-{n}-worker.json", stats)
                summaries.append(summary)
                save("summary.json", summaries)
                write_report(args.output / "report.md", summaries)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                if len(containers) != 1:
                    raise RuntimeError(f"단계 사이에 컨테이너가 바뀌었습니다: {containers}")
                if not summary["passed"]:
                    print(f"{n}개에서 SLO 실패, 탐색 종료", flush=True)
                    break
    finally:
        workspace.proxy_tokens.delete(token.token_id)
        print("임시 proxy token 삭제 완료", flush=True)


if __name__ == "__main__":
    main()
