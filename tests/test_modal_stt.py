"""Modal 파이프라인부터 Backend 전사 송신까지의 핵심 흐름을 검증한다."""

import asyncio
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient
from websockets.asyncio.client import connect

import meety_ai.stt.modal as adapter_module
from meety_ai.live import create_live_app
from meety_ai.modal_app.stt.pipeline import InferenceRuntime, create_inference_app
from meety_ai.modal_app.stt.protocol import SessionStart


# 테스트 이름: test_modal_transcript_before_stop_and_final_drain
# 목적: Modal 기동 중에도 음성을 받고 준비 후 입력 순서대로 전사·종료하는 흐름을 보호한다.
# 준비: 모델 추론만 대체하고 실제 Modal adapter의 로컬 WebSocket 연결을 잠시 보류한다.
# 실행: start/ready → 연결 준비 중 42개 PCM·heartbeat → 준비 완료 → 전사 → pause/resume → stop.
# 기대 결과: 준비 중 ACK·pong을 받고 PCM 원본 순서와 pause 전후 발화·최종 drain이 유지된다.
# 실패 조건: 준비 때문에 ready·수신이 막히거나 32개 이후 버퍼 초과·PCM 유실·구간 누락이 난다.
def test_modal_transcript_before_stop_and_final_drain(monkeypatch):
    class Runtime(InferenceRuntime):
        def __init__(self):
            self.geometry = {
                "first_samples": 16000,
                "chunk_samples": 16000,
                "frame_samples": 160,
                "mel_step": 100,
            }
            self.metadata = {"test": True}
            self.audio = []
            self.start_runtime()

        async def diarize(self, session, pcm, *, last):
            self.audio.append(pcm)
            speaker = 0 if session.frames < 200 else 1
            active = session.frames < 100 or session.frames >= 200
            return {
                "active": [[int(active and i == speaker) for i in range(8)]] * (len(pcm) // 320),
                "next_start": session.next_sample + len(pcm) // 2,
            }

        def _transcribe(self, jobs):
            return [(f"화자 {job.speaker}의 발언", None) for job in jobs]

        async def _call_worker(self, request):
            return {"released": True}

    runtime = Runtime()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_inference_app(runtime), log_level="error", lifespan="off")
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()

    async def wait_started():
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)

    asyncio.run(wait_started())
    ready_to_connect = threading.Event()
    connecting = threading.Event()

    async def delayed_connect(url, **kwargs):
        connecting.set()
        while not ready_to_connect.is_set():
            await asyncio.sleep(0.01)
        return await connect(f"ws://127.0.0.1:{port}/transcribe", **kwargs)

    monkeypatch.setattr(adapter_module, "connect", delayed_connect)
    # 기존 테스트의 공급자 대체 대신 실제 Modal adapter를 사용한다.
    monkeypatch.setattr("meety_ai.live.ModalSTTClient", adapter_module.ModalSTTClient)
    app = create_live_app()
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/v1/live-meeting") as ws:
                ws.send_json(
                    {
                        "type": "session.start",
                        "requestId": "start",
                        "meetingId": 1,
                        "recordingSessionId": 1,
                        "participantCount": 0,
                        "participants": [],
                        "title": "개발 회의",
                    }
                )
                with ThreadPoolExecutor(max_workers=1) as receiver:
                    response = receiver.submit(ws.receive_json)
                    try:
                        assert response.result(timeout=1)["type"] == "session.ready"
                    finally:
                        if not response.done():
                            ready_to_connect.set()

                assert connecting.wait(1)
                chunks = [bytes([i]) * 800 for i in range(40)] + [b"A" * 32000, b"B" * 32000]
                for sequence, chunk in enumerate(chunks):
                    ws.send_json({"type": "audio.meta", "payload": {"sequence": sequence}})
                    ws.send_bytes(chunk)
                    assert ws.receive_json()["type"] == "audio.ack"
                ws.send_json(
                    {"type": "heartbeat.ping", "requestId": "loading", "recordingSessionId": 1}
                )
                assert ws.receive_json()["type"] == "heartbeat.pong"
                assert not runtime.sessions
                ready_to_connect.set()
                first = ws.receive_json()
                assert first["type"] == "transcript.committed"
                assert first["payload"]["speakerId"] == 0
                assert first["payload"]["startedAtMs"] == 0
                assert first["payload"]["endedAtMs"] == 1000

                ws.send_json({"type": "session.pause", "requestId": "pause"})
                assert ws.receive_json()["type"] == "session.paused"
                ws.send_json({"type": "session.resume", "requestId": "resume"})
                assert ws.receive_json()["type"] == "session.resumed"
                ws.send_json({"type": "audio.meta", "payload": {"sequence": len(chunks)}})
                ws.send_bytes(b"C" * 32000)
                assert ws.receive_json()["type"] == "audio.ack"
                ws.send_json({"type": "session.stop", "requestId": "stop"})
                final = ws.receive_json()
                ended = ws.receive_json()
                assert final["type"] == "transcript.committed"
                assert final["payload"]["speakerId"] == 1
                assert final["payload"]["content"] == "화자 1의 발언"
                assert final["payload"]["startedAtMs"] == 2000
                assert final["payload"]["endedAtMs"] == 4000
                assert final["payload"]["sequenceNumber"] == 1
                assert ended["type"] == "session.ended"
                assert ended["payload"]["audioDurationMs"] == 4000
            assert app.state.recording_connections == 0
        assert not runtime.sessions
        assert b"".join(runtime.audio) == b"".join(chunks) + b"C" * 32000
    finally:
        ready_to_connect.set()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


# 테스트 이름: test_runtime_close_stops_asr_output_thread
# 목적: 실제 GPU 종료에서 발견한 WorkerAsyncOutput 스레드 잔존을 재현한다.
# 준비: vLLM과 같은 출력 스레드 풀을 만들고 GPU 없는 엔진 객체에 연결한다.
# 실행: runtime.close()를 호출한다.
# 기대 결과: 출력 스레드가 종료되고 NCCL 정리가 호출된다.
# 실패 조건: 종료 후 출력 스레드가 살아 있거나 NCCL 정리를 생략한다.
def test_runtime_close_stops_asr_output_thread(monkeypatch):
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="meety-close-regression")
    pool.submit(lambda: None).result()
    cleaned = []
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            distributed=SimpleNamespace(
                is_initialized=lambda: True,
                destroy_process_group=lambda: cleaned.append("nccl"),
            )
        ),
    )
    runtime = InferenceRuntime.__new__(InferenceRuntime)
    runtime.asr_task = runtime.proc = None
    runtime.asr = SimpleNamespace(
        model=SimpleNamespace(
            llm_engine=SimpleNamespace(
                engine_core=SimpleNamespace(shutdown=lambda: None),
                model_executor=SimpleNamespace(async_output_thread=pool),
            )
        )
    )
    try:
        runtime.close()
        assert not any(t.name.startswith("meety-close-regression") for t in threading.enumerate())
        assert cleaned == ["nccl"]
    finally:
        pool.shutdown(wait=True)


# 테스트 이름: test_short_turns_are_skipped_before_asr
# 목적: 문맥을 따라 전사한 초단구간을 생략하되 500ms부터는 정상 전사한다.
# 준비: 실제 오류 길이와 500ms 경계의 PCM을 넣고 모델 추론만 고정 결과로 대체한다.
# 실행: 세션별 PCM 공급 후 EOF까지 처리한다.
# 기대 결과: 500ms 미만은 ASR 호출·전사 없이 종료되고 500ms부터 구간 번호 0으로 전사된다.
# 실패 조건: 짧은 구간이 추론되거나 500ms가 생략되거나 종료 집계가 어긋난다.
async def test_short_turns_are_skipped_before_asr():
    class Runtime(InferenceRuntime):
        def __init__(self):
            self.geometry = {
                "first_samples": 16000,
                "chunk_samples": 16000,
                "frame_samples": 160,
                "mel_step": 100,
            }
            self.calls = []
            self.start_runtime()

        async def diarize(self, session, pcm, *, last):
            return {
                "active": [[1, 0, 0, 0, 0, 0, 0, 0]] * (len(pcm) // 320),
                "next_start": len(pcm) // 2,
            }

        def _transcribe(self, jobs):
            self.calls.extend(jobs)
            return [("정상 전사", None) for job in jobs]

        async def _call_worker(self, request):
            return {"released": True}

    runtime = Runtime()
    try:
        for samples in (640, 3680, 5120, 7999, 8000, 8001):
            session = runtime.open(
                SessionStart(
                    type="session.start",
                    meeting_id=1,
                    recording_session_id=1,
                )
            )
            await session.feed(bytes(samples * 2))
            await session.finish()
            results = []
            while (event := await session.results.get()) is not None:
                results.append(event)
            committed = [event for event in results if event["type"] == "segment.committed"]
            expected = int(samples >= 8000)
            assert len(committed) == expected
            assert len(runtime.calls) == expected
            assert results[-1]["succeededSegments"] == expected
            assert results[-1]["failedSegments"] == 0
            assert results[-1]["receivedSamples"] == samples
            if expected:
                assert committed[0]["segmentId"] == 0
                assert committed[0]["endedAtMs"] - committed[0]["startedAtMs"] == 500
            runtime.calls.clear()
            await runtime.release(session)
    finally:
        runtime.asr_task.cancel()
        await asyncio.gather(runtime.asr_task, return_exceptions=True)


# 테스트 이름: test_same_speaker_turns_merge_within_one_second
# 목적: 정확히 1초 간격인 짧은 발화가 따로 닫혀 모두 생략되는 회귀를 막는다.
# 준비: 300ms 발화 두 개 사이에 990·1000·1010ms 무음을 반환하는 모델 대역.
# 실행: 실제 세션에 PCM을 공급하고 EOF까지 처리한다.
# 기대 결과: 1초 이내 간격은 한 전사로 남고 1초 초과 시 두 초단구간은 생략된다.
# 실패 조건: 1초 경계에서 전사가 사라지거나 1초 초과 간격까지 병합된다.
@pytest.mark.parametrize("gap_frames", [99, 100, 101])
async def test_same_speaker_turns_merge_within_one_second(gap_frames):
    class Runtime(InferenceRuntime):
        def __init__(self):
            self.geometry = {
                "first_samples": 16000,
                "chunk_samples": 16000,
                "frame_samples": 160,
                "mel_step": 100,
            }
            self.start_runtime()

        async def diarize(self, session, pcm, *, last):
            return {
                "active": [
                    [int(frame < 30 or frame >= 30 + gap_frames), 0, 0, 0, 0, 0, 0, 0]
                    for frame in range(session.frames, session.frames + len(pcm) // 320)
                ],
                "next_start": session.next_sample + len(pcm) // 2,
            }

        def _transcribe(self, jobs):
            return [("이어 말한 발언", None) for job in jobs]

        async def _call_worker(self, request):
            return {"released": True}

    runtime = Runtime()
    session = runtime.open(
        SessionStart(type="session.start", meeting_id=1, recording_session_id=1)
    )
    try:
        await session.feed(bytes((60 + gap_frames) * 320))
        await session.finish()
        results = []
        while (event := await session.results.get()) is not None:
            results.append(event)
        committed = [event for event in results if event["type"] == "segment.committed"]
        assert len(committed) == int(gap_frames <= 100)
        assert results[-1]["succeededSegments"] == len(committed)
        if committed:
            assert committed[0]["startedAtMs"] == 0
            assert committed[0]["endedAtMs"] == (60 + gap_frames) * 10
            assert committed[0]["content"] == "이어 말한 발언"
    finally:
        await runtime.release(session)
        runtime.asr_task.cancel()
        await asyncio.gather(runtime.asr_task, return_exceptions=True)


# 테스트 이름: test_modal_concurrent_sessions_keep_transcripts_separate
# 목적: 같은 GPU worker에서 두 회의의 전사와 종료 처리가 서로 영향을 주지 않게 한다.
# 준비: 하나의 Runtime과 create_inference_app에 서로 다른 회의·녹음 세션으로 연결한다.
# 실행: 두 연결에 구분되는 PCM을 동시에 보내고 첫 회의 종료 후 두 번째 회의 입력을 이어간다.
# 기대 결과: 전사·화자·시간축이 회의별로 유지되고 각 연결은 최종 전사 후 정상 종료된다.
# 실패 조건: 두 번째 회의를 거절하거나 결과가 섞이거나 첫 회의 정리가 다른 회의를 중단한다.
