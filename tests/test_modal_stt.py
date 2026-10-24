"""Modal 파이프라인부터 Backend 전사 송신까지의 핵심 흐름을 검증한다."""

import asyncio
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from types import SimpleNamespace

import uvicorn
from fastapi.testclient import TestClient
from websockets.asyncio.client import connect

import meety_ai.stt.modal as adapter_module
from meety_ai.live import create_live_app
from meety_ai.modal_app.stt.pipeline import InferenceRuntime, create_inference_app
from meety_ai.modal_app.stt.protocol import SessionStart


# 테스트 이름: test_modal_transcript_before_stop_and_final_drain
# 목적: 실제 adapter·구간 처리·전사 중계를 거쳐 녹음 중 전사와 최종 drain을 보호한다.
# 준비: 모델 추론만 대체하고 로컬 WebSocket 서버에 실제 Modal adapter를 연결한다.
# 실행: session.start → PCM → 첫 전사 → pause/resume → 마지막 PCM → stop.
# 기대 결과: pause 전후 같은 화자의 발화가 이어지고 마지막 전사 뒤에 종료된다.
# 실패 조건: stop 이전 전사가 없거나 pause만으로 발화를 나누거나 마지막 구간이 누락된다.
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
            self.start_runtime()

        async def diarize(self, session, pcm, *, last):
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
    monkeypatch.setattr(
        adapter_module,
        "connect",
        lambda url, **kwargs: connect(f"ws://127.0.0.1:{port}/transcribe", **kwargs),
    )
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
                assert ws.receive_json()["type"] == "session.ready"

                for sequence in range(3):
                    ws.send_json({"type": "audio.meta", "payload": {"sequence": sequence}})
                    ws.send_bytes(bytes(32000))
                    assert ws.receive_json()["type"] == "audio.ack"
                first = ws.receive_json()
                assert first["type"] == "transcript.committed"
                assert first["payload"]["speakerId"] == 0
                assert first["payload"]["startedAtMs"] == 0
                assert first["payload"]["endedAtMs"] == 1000

                ws.send_json({"type": "session.pause", "requestId": "pause"})
                assert ws.receive_json()["type"] == "session.paused"
                ws.send_json({"type": "session.resume", "requestId": "resume"})
                assert ws.receive_json()["type"] == "session.resumed"
                ws.send_json({"type": "audio.meta", "payload": {"sequence": 3}})
                ws.send_bytes(bytes(32000))
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
    finally:
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
