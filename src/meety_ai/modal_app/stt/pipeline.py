"""회의별 PCM·화자 상태를 분리하고 공용 GPU에서 구간을 배치 전사한다."""

import asyncio
import base64
import contextlib
import hashlib
import importlib.metadata
import json
import logging
import os
import select
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from meety_ai.modal_app.stt.protocol import (
    MAX_AUDIO_MESSAGE_BYTES,
    MAX_TEXT_MESSAGE_BYTES,
    SAMPLE_RATE,
    SessionStart,
    SessionStop,
)

logger = logging.getLogger(__name__)
ASR_MODEL = "TeamUNIVA/qwen3_asr_1.7b_ko"
ASR_REVISION = "3e66f586033753589c3effeb2c8cbce46a1ddabb"
NEMOTRON_REVISION = "f667ed73aee57d40cc39428eb768b4fd87a0a29e"
# shortcut: GPU당 10개는 5분 부하로만 검증, 장시간 측정 후 한도를 재검토한다.
MAX_SESSIONS = 10
BATCH_SIZE = 8
BATCH_WAIT_SECONDS = 0.05
QUEUE_WAIT_SECONDS = 5
SEND_TIMEOUT_SECONDS = 10
DRAIN_TIMEOUT_SECONDS = 30
MODEL_TIMEOUT_SECONDS = 30


class PipelineError(Exception):
    """입력 원문이나 모델 오류를 노출하지 않는 파이프라인 오류."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class ASRJob:
    session: "PipelineSession"
    segment_id: int
    speaker: int
    start_sample: int
    end_sample: int
    reason: str
    pcm: bytes
    queued_at: float

    def fields(self) -> dict:
        return {
            "segmentId": self.segment_id,
            "speakerId": self.speaker,
            "startedAtMs": self.start_sample * 1000 // SAMPLE_RATE,
            "endedAtMs": self.end_sample * 1000 // SAMPLE_RATE,
            "reason": self.reason,
        }


class PipelineSession:
    def __init__(self, runtime: "InferenceRuntime", start: SessionStart):
        self.runtime, self.start = runtime, start
        self.key = uuid.uuid4().hex
        self.pcm = bytearray()
        self.base_sample = self.total_bytes = self.next_sample = self.frames = self.mel_cursor = 0
        self.open_turns: dict[int, int] = {}
        self.pending_ends: dict[int, int] = {}
        self.first = True
        self.stopping = self.aborted = False
        self.failure: str | None = None
        self.segments = self.pending_jobs = self.succeeded = self.failed = 0
        self.results: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=32)
        self.drained = asyncio.Event()
        self.drained.set()

    async def feed(self, pcm: bytes) -> None:
        self.runtime.check_health()
        if self.failure:
            raise PipelineError(self.failure)
        if self.stopping or self.aborted:
            raise PipelineError("invalid_state")
        if not pcm or len(pcm) > MAX_AUDIO_MESSAGE_BYTES:
            raise PipelineError("invalid_audio_size")
        self.pcm.extend(pcm)
        self.total_bytes += len(pcm)
        if len(self.pcm) > 40 * SAMPLE_RATE * 2:
            raise PipelineError("audio_buffer_full")
        await self._pump()

    def _slice(self, lower: int, upper: int) -> bytes:
        if not self.base_sample <= lower <= upper <= self.total_bytes // 2:
            raise PipelineError("audio_buffer_invalid")
        return bytes(self.pcm[(lower - self.base_sample) * 2 : (upper - self.base_sample) * 2])

    async def _pump(self, *, last: bool = False) -> None:
        geometry = self.runtime.geometry
        stride = geometry["frame_samples"]
        while True:
            size = geometry["first_samples" if self.first else "chunk_samples"]
            available = self.total_bytes // 2 - self.next_sample
            if available <= 0 or (available < size and not last):
                return
            final = last and available <= size
            upper = self.next_sample + min(size, available)
            response = await self.runtime.diarize(
                self,
                self._slice(self.next_sample, upper),
                last=final,
            )
            active = response["active"]
            if any(len(row) != 8 for row in active):
                raise PipelineError("diarization_output_invalid")
            count = min(len(active), self.total_bytes // 2 // stride - self.frames)
            if count < 0:
                raise PipelineError("diarization_cursor_invalid")
            for row in active[:count]:
                lower, end = self.frames * stride, (self.frames + 1) * stride
                await self._activity(row, lower, end)
                self.frames += 1
            self.first = False
            self.mel_cursor += geometry["mel_step"]
            next_sample = response["next_start"]
            if next_sample <= self.next_sample:
                raise PipelineError("diarization_cursor_invalid")
            self.next_sample = next_sample
            self._trim()
            if final:
                return

    async def _activity(self, row: list[int], lower: int, upper: int) -> None:
        for speaker, enabled in enumerate(row):
            if enabled:
                self.pending_ends.pop(speaker, None)
                self.open_turns.setdefault(speaker, lower)
                boundary = self.open_turns[speaker] + 25 * SAMPLE_RATE
                if upper >= boundary:
                    await self._close_turn(speaker, boundary, "max_duration")
                    if upper > boundary:
                        self.open_turns[speaker] = boundary
            elif speaker in self.open_turns:
                self.pending_ends.setdefault(speaker, lower)
                if upper - self.pending_ends[speaker] > SAMPLE_RATE:
                    await self._close_turn(speaker, self.pending_ends[speaker], "silence")

    def _trim(self) -> None:
        lower = min(
            self.next_sample,
            self.total_bytes // 2,
            self.frames * self.runtime.geometry["frame_samples"],
            *self.open_turns.values(),
        )
        del self.pcm[: (lower - self.base_sample) * 2]
        self.base_sample = lower

    async def _close_turn(self, speaker: int, upper: int, reason: str) -> None:
        lower = self.open_turns.pop(speaker)
        self.pending_ends.pop(speaker, None)
        if upper - lower < SAMPLE_RATE // 2:
            return
        if upper - lower > 25 * SAMPLE_RATE:
            raise PipelineError("segment_duration_invalid")
        job = ASRJob(
            self,
            self.segments,
            speaker,
            lower,
            upper,
            reason,
            self._slice(lower, upper),
            time.perf_counter(),
        )
        self.pending_jobs += 1
        self.drained.clear()
        try:
            async with asyncio.timeout(QUEUE_WAIT_SECONDS):
                await self.runtime.asr_queue.put(job)
        except BaseException:
            self.job_done()
            raise
        self.segments += 1

    def job_done(self) -> None:
        self.pending_jobs -= 1
        if self.pending_jobs == 0:
            self.drained.set()

    async def finish(self) -> None:
        self.stopping = True
        if self.total_bytes % 2:
            raise PipelineError("truncated_pcm_sample")
        await self._pump(last=True)
        for speaker in list(self.open_turns):
            boundary = self.frames * self.runtime.geometry["frame_samples"]
            await self._close_turn(speaker, self.pending_ends.get(speaker, boundary), "eof")
        await self.drained.wait()
        self.runtime.check_health()
        if self.failure:
            raise PipelineError(self.failure)
        if self.succeeded + self.failed != self.segments:
            raise PipelineError("segment_result_missing")
        await self.results.put(
            {
                "type": "session.ended",
                "status": "completed",
                "succeededSegments": self.succeeded,
                "failedSegments": self.failed,
                "receivedSamples": self.total_bytes // 2,
                "audioDurationMs": self.total_bytes * 1000 // (SAMPLE_RATE * 2),
                "unscoredTailSamples": self.total_bytes // 2
                - self.frames * self.runtime.geometry["frame_samples"],
            }
        )
        await self.results.put(None)


class InferenceRuntime:
    def __init__(self):
        from huggingface_hub import snapshot_download
        from qwen_asr import Qwen3ASRModel

        self.asr_task: asyncio.Task | None = None
        self.asr = None
        self.proc = None
        self.generations: list[dict] = []
        self.proc = subprocess.Popen(
            ["/opt/nemotron/bin/python", str(Path(__file__).with_name("nemotron_worker.py"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            ready = self._read_response(180)
            if not ready.get("ready") or ready["revision"] != NEMOTRON_REVISION:
                raise PipelineError("diarization_initialization_failed")
            self.geometry = ready["geometry"]
            snapshot = snapshot_download(ASR_MODEL, revision=ASR_REVISION)
            self.asr = Qwen3ASRModel.LLM(
                model=snapshot,
                gpu_memory_utilization=0.45,
                max_model_len=8192,
                max_num_seqs=BATCH_SIZE,
                max_inference_batch_size=BATCH_SIZE,
                max_new_tokens=1024,
                enforce_eager=True,
                seed=0,
                enable_prefix_caching=False,
                mm_processor_cache_gb=0,
            )
            self.metadata = {
                "bootId": uuid.uuid4().hex,
                "asrModel": ASR_MODEL,
                "asrRevision": Path(snapshot).name,
                "nemotronRevision": ready["revision"],
                "nemotronTransformers": ready["transformers"],
                "geometry": self.geometry,
                "sourceSha256": {
                    name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                    for name in ("pipeline.py", "nemotron_worker.py", "protocol.py")
                },
                "versions": {
                    name: importlib.metadata.version(name)
                    for name in ("qwen-asr", "vllm", "torch", "transformers")
                },
            }
            self.metadata["containerId"] = os.environ.get("MODAL_TASK_ID")
            original_generate = self.asr.model.generate

            def capture_generate(*args, **kwargs):
                outputs = original_generate(*args, **kwargs)
                self.generations.extend(
                    {"finish_reason": part.finish_reason}
                    for output in outputs
                    for part in output.outputs
                )
                return outputs

            self.asr.model.generate = capture_generate
        except BaseException:
            self.close()
            raise

    def start_runtime(self) -> None:
        """모델과 별개로 worker의 비동기 실행 상태를 준비한다."""
        self.sessions: dict[str, PipelineSession] = {}
        self.asr_queue: asyncio.Queue[ASRJob] = asyncio.Queue(maxsize=16)
        self.diar_lock = asyncio.Lock()
        self.failed_event = asyncio.Event()
        self.failure: str | None = None
        self.asr_task = None

    def _read_response(self, timeout: float) -> dict:
        if not select.select([self.proc.stdout], [], [], timeout)[0]:
            self.proc.kill()
            raise PipelineError("diarization_timeout")
        line = self.proc.stdout.readline()
        if not line:
            raise PipelineError("diarization_worker_unavailable")
        return json.loads(line)

    def _rpc(self, request: dict) -> dict:
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        return self._read_response(MODEL_TIMEOUT_SECONDS)

    def check_health(self) -> None:
        if self.failure:
            raise PipelineError(self.failure)

    def fail(self, code: str) -> None:
        self.failure = self.failure or code
        self.failed_event.set()

    def open(self, start: SessionStart) -> PipelineSession:
        self.check_health()
        if len(self.sessions) >= MAX_SESSIONS:
            raise PipelineError("capacity_exceeded")
        if any(
            s.start.recording_session_id == start.recording_session_id
            for s in self.sessions.values()
        ):
            raise PipelineError("session_already_active")
        if self.asr_task is None:
            self.asr_task = asyncio.create_task(self._asr_loop())
        session = PipelineSession(self, start)
        self.sessions[session.key] = session
        return session

    async def _call_worker(self, request: dict) -> dict:
        async with self.diar_lock:
            self.check_health()
            operation = asyncio.create_task(asyncio.to_thread(self._rpc, request))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # 취소된 연결의 응답을 다른 세션 요청이 읽지 않도록 RPC 완료까지 잠금을 유지한다.
                try:
                    await operation
                except Exception:
                    self.fail("diarization_worker_unavailable")
                raise
            except Exception as error:
                logger.error("nemotron_failed error_type=%s", type(error).__name__)
                self.fail("diarization_worker_unavailable")
                raise PipelineError(self.failure) from error

    async def diarize(self, session: PipelineSession, pcm: bytes, *, last: bool) -> dict:
        return await self._call_worker(
            {
                "op": "chunk",
                "session": session.key,
                "pcm": base64.b64encode(pcm).decode("ascii"),
                "first": session.first,
                "last": last,
                "mel_cursor": session.mel_cursor,
            }
        )

    def _transcribe(self, jobs: list[ASRJob]) -> list[tuple[str, str | None]]:
        import numpy as np
        import torch

        self.generations.clear()
        audio = []
        for job in jobs:
            waveform = np.frombuffer(job.pcm, dtype="<i2").astype(np.float32) / 32768.0
            if not len(waveform) or len(waveform) > 25 * SAMPLE_RATE:
                raise PipelineError("asr_input_invalid")
            audio.append((waveform, SAMPLE_RATE))
        try:
            outputs = self.asr.transcribe(
                audio=audio,
                context=[job.session.start.context for job in jobs],
                language="Korean",
                return_time_stamps=False,
            )
        except (torch.cuda.OutOfMemoryError, MemoryError) as error:
            raise PipelineError("asr_worker_unavailable") from error
        except Exception as error:
            input_error = isinstance(error, ValueError) or (
                isinstance(error, RuntimeError)
                and any(word in str(error).lower() for word in ("padding", "stft"))
            )
            if not input_error:
                raise PipelineError("asr_worker_unavailable") from error
            logger.error("asr_batch_failed error_type=%s", type(error).__name__)
            if len(jobs) > 1:
                return [self._transcribe([job])[0] for job in jobs]
            return [("", "asr_failed")] * len(jobs)
        if len(outputs) != len(jobs) or len(self.generations) != len(jobs):
            return [("", "asr_result_mismatch")] * len(jobs)
        return [
            (
                output.text,
                "generation_token_cap"
                if generation["finish_reason"] == "length"
                else ("empty_transcript" if not output.text.strip() else None),
            )
            for output, generation in zip(outputs, self.generations, strict=True)
        ]

    async def _asr_loop(self) -> None:
        try:
            while True:
                first = await self.asr_queue.get()
                batch = [first]
                deadline = first.queued_at + BATCH_WAIT_SECONDS
                while len(batch) < BATCH_SIZE:
                    try:
                        batch.append(self.asr_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        remaining = deadline - time.perf_counter()
                        if remaining <= 0:
                            break
                        try:
                            batch.append(await asyncio.wait_for(self.asr_queue.get(), remaining))
                        except TimeoutError:
                            break
                jobs = [job for job in batch if not job.session.aborted]
                started = time.perf_counter()
                try:
                    async with asyncio.timeout(MODEL_TIMEOUT_SECONDS):
                        results = await asyncio.to_thread(self._transcribe, jobs) if jobs else []
                    service = time.perf_counter() - started
                    for job, (text, error) in zip(jobs, results, strict=True):
                        session = job.session
                        if session.aborted:
                            continue
                        message = {
                            "type": "segment.failed" if error else "segment.committed",
                            **job.fields(),
                            "recognizedAt": datetime.now(UTC).isoformat(),
                            "timings": {
                                "queueSeconds": started - job.queued_at,
                                "asrSeconds": service,
                                "batchSize": len(jobs),
                            },
                        }
                        if error:
                            message["code"] = error
                            session.failed += 1
                            if error != "empty_transcript":
                                session.failure = error
                            logger.warning(
                                "asr_segment_failed meetingId=%s recordingSessionId=%s "
                                "segmentId=%s speakerId=%s startedAtMs=%s endedAtMs=%s "
                                "durationMs=%s reason=%s code=%s",
                                session.start.meeting_id,
                                session.start.recording_session_id,
                                job.segment_id,
                                job.speaker,
                                message["startedAtMs"],
                                message["endedAtMs"],
                                (job.end_sample - job.start_sample) * 1000 // SAMPLE_RATE,
                                job.reason,
                                error,
                            )
                        else:
                            message["content"] = text
                            session.succeeded += 1
                        try:
                            async with asyncio.timeout(QUEUE_WAIT_SECONDS):
                                await session.results.put(message)
                        except TimeoutError:
                            session.aborted = True
                            session.failure = "result_delivery_timeout"
                            session.drained.set()
                finally:
                    for job in batch:
                        job.pcm = b""
                        job.session.job_done()
                        self.asr_queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("asr_worker_failed error_type=%s", type(error).__name__)
            self.fail("asr_worker_unavailable")

    async def release(self, session: PipelineSession) -> None:
        session.aborted = True
        self.sessions.pop(session.key, None)
        session.pcm.clear()
        session.open_turns.clear()
        session.pending_ends.clear()
        while not session.results.empty():
            session.results.get_nowait()
        retained = []
        while not self.asr_queue.empty():
            job = self.asr_queue.get_nowait()
            self.asr_queue.task_done()
            if job.session is session:
                session.job_done()
            else:
                retained.append(job)
        for job in retained:
            self.asr_queue.put_nowait(job)
        if not self.failure:
            with contextlib.suppress(PipelineError):
                await self._call_worker({"op": "release", "session": session.key})

    def close(self) -> None:
        if self.asr_task is not None:
            self.asr_task.get_loop().call_soon_threadsafe(self.asr_task.cancel)
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.asr is not None:
            import torch

            engine = self.asr.model.llm_engine
            engine.engine_core.shutdown()
            # 고정한 vLLM 0.14.0의 shutdown은 출력 스레드 풀을 정리하지 않는다.
            if engine.model_executor.async_output_thread is not None:
                engine.model_executor.async_output_thread.shutdown(wait=True)
            if torch.distributed.is_initialized():
                torch.distributed.destroy_process_group()
            self.asr = None


def create_inference_app(runtime: InferenceRuntime) -> FastAPI:
    """모델을 공유하고 연결마다 독립적인 추론 세션을 만든다."""
    app = FastAPI()

    async def send(websocket: WebSocket, message: dict) -> None:
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            await websocket.send_json(message)

    async def receive_text(websocket: WebSocket) -> str:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        text = message.get("text")
        if text is None or len(text.encode("utf-8")) > MAX_TEXT_MESSAGE_BYTES:
            raise PipelineError("invalid_message")
        return text

    @app.websocket("/transcribe")
    async def transcribe(websocket: WebSocket) -> None:
        await websocket.accept()
        session = None
        tasks = []
        flow = None
        try:
            async with asyncio.timeout(10):
                start = SessionStart.model_validate_json(await receive_text(websocket))
            session = runtime.open(start)
            await send(websocket, {"type": "session.ready", "metadata": runtime.metadata})

            async def ingest() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(message.get("code", 1000))
                    if message.get("bytes") is not None:
                        await session.feed(message["bytes"])
                    else:
                        text = message.get("text", "")
                        if len(text.encode("utf-8")) > MAX_TEXT_MESSAGE_BYTES:
                            raise PipelineError("invalid_message")
                        SessionStop.model_validate_json(text)
                        async with asyncio.timeout(DRAIN_TIMEOUT_SECONDS):
                            await session.finish()
                        return

            async def emit() -> None:
                while (message := await session.results.get()) is not None:
                    if session.failure:
                        raise PipelineError(session.failure)
                    await send(websocket, message)

            tasks = [
                asyncio.create_task(ingest()),
                asyncio.create_task(emit()),
                asyncio.create_task(runtime.failed_event.wait()),
            ]
            flow = asyncio.gather(*tasks[:2])
            done, _ = await asyncio.wait([flow, tasks[2]], return_when=asyncio.FIRST_COMPLETED)
            if tasks[2] in done:
                runtime.check_health()
            await flow
            await websocket.close(code=1000)
        except WebSocketDisconnect:
            pass
        except Exception as error:
            code = (
                error.code
                if isinstance(error, PipelineError)
                else (
                    "invalid_message"
                    if isinstance(error, ValidationError)
                    else (
                        "processing_timeout"
                        if isinstance(error, TimeoutError)
                        else "processing_failed"
                    )
                )
            )
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.warning(
                "inference_session_failed code=%s error_type=%s", code, type(error).__name__
            )
            with contextlib.suppress(RuntimeError, WebSocketDisconnect, TimeoutError):
                await send(websocket, {"type": "error", "code": code})
                await websocket.close(code=1008 if code == "invalid_message" else 1011)
        finally:
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if flow is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await flow
            if session is not None:
                await runtime.release(session)

    return app
