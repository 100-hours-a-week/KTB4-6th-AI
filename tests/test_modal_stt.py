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
from pydantic import ValidationError
from websockets.asyncio.client import connect

import meety_ai.stt.modal as adapter_module
from meety_ai.live import create_live_app
from meety_ai.modal_app.stt.pipeline import InferenceRuntime, create_inference_app
from meety_ai.modal_app.stt.protocol import SessionStart


# 테스트 이름: test_modal_context_update_applies_to_next_inference
# 목적: 같은 연결에서 맥락을 갱신해도 진행 중인 추론과 다른 세션을 변경하지 않는다.
# 준비: 실제 adapter·WebSocket·파이프라인을 연결하고 수치 처리 의존성과 모델만 대체한다.
# 실행: 기본 맥락 추론 시작 → 다음 구간 대기 → 맥락 갱신 → 추론 재개 → 다른 세션 전사 → 종료.
# 기대 결과: 다음 추론부터 새 맥락을 쓰고 화자·시간축·마지막 전사와 세션 정리를 유지한다.
# 실패 조건: 맥락이 소급되거나 세션 간 섞이거나 큐에 있던 구간에 갱신이 반영되지 않는다.
async def test_modal_context_update_applies_to_next_inference(monkeypatch):
    class Waveform:
        def __init__(self, pcm):
            self.samples = len(pcm) // 2

        def astype(self, dtype):
            return self

        def __truediv__(self, divisor):
            return self

        def __len__(self):
            return self.samples

    monkeypatch.setitem(
        sys.modules,
        "numpy",
        SimpleNamespace(frombuffer=lambda pcm, dtype: Waveform(pcm), float32=float),
    )
    monkeypatch.setitem(
        sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=MemoryError))
    )
    loop = asyncio.get_running_loop()
    first_started = asyncio.Event()
    resume_first = threading.Event()
    calls = []

    class Runtime(InferenceRuntime):
        def __init__(self):
            self.geometry = {
                "first_samples": 16000,
                "chunk_samples": 16000,
                "frame_samples": 160,
                "mel_step": 100,
            }
            self.metadata = {"test": True}
            self.generations = []
            self.asr = SimpleNamespace(transcribe=self.transcribe)
            self.start_runtime()

        async def diarize(self, session, pcm, *, last):
            return {
                "active": [
                    [int(frame % 300 < 100), 0, 0, 0, 0, 0, 0, 0]
                    for frame in range(session.frames, session.frames + len(pcm) // 320)
                ],
                "next_start": session.next_sample + len(pcm) // 2,
            }

        def transcribe(self, *, audio, context, language, return_time_stamps):
            calls.append(tuple(context))
            if len(calls) == 1:
                loop.call_soon_threadsafe(first_started.set)
                assert resume_first.wait(timeout=5)
            self.generations.extend({"finish_reason": "stop"} for _ in audio)
            return [SimpleNamespace(text=f"전사: {value}") for value in context]

        async def _call_worker(self, request):
            return {"released": True}

    async def wait_until(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)

    runtime = Runtime()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_inference_app(runtime), log_level="error", lifespan="off")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    transcripts = asyncio.Queue()
    other_transcripts = asyncio.Queue()
    errors = []
    clients = [
        adapter_module.ModalSTTClient(
            f"ws://127.0.0.1:{port}/transcribe", "test", "test", callback, errors.append
        )
        for callback in (transcripts.put_nowait, other_transcripts.put_nowait)
    ]
    default_context = "기본 " * 1000
    updated_context = "갱신 " * 266 + " 끝"
    try:
        async with asyncio.timeout(10):
            await wait_until(lambda: server.started)
            await clients[0].start(meeting_id=1, recording_session_id=1, context=default_context)
            await clients[1].start(meeting_id=2, recording_session_id=2, context="다른 세션")
            session, other = runtime.sessions.values()
            original_key = session.key
            await clients[0].send_audio(bytes(3 * 32000))
            await first_started.wait()
            await clients[0].send_audio(bytes(3 * 32000))
            await wait_until(lambda: session.segments == 2)
            with pytest.raises(ValidationError):
                await clients[0].update_context(updated_context + " ")
            await clients[0].update_context(updated_context)
            await wait_until(lambda: session.context == updated_context)
            assert session.key == original_key
            assert session.start.context == default_context
            assert other.context == "다른 세션"
            assert runtime.asr_queue.qsize() == 1
            resume_first.set()
            first = await transcripts.get()
            second = await transcripts.get()
            assert first.content == f"전사: {default_context}"
            assert second.content == f"전사: {updated_context}"
            assert first.speaker_id == second.speaker_id == 0
            assert (first.started_at_ms, first.ended_at_ms) == (0, 1000)
            assert (second.started_at_ms, second.ended_at_ms) == (3000, 4000)
            await clients[1].send_audio(bytes(3 * 32000))
            assert (await other_transcripts.get()).content == "전사: 다른 세션"
            await clients[0].update_context("")
            await clients[0].send_audio(bytes(32000))
            await clients[0].finish()
            tail = await transcripts.get()
            assert tail.content == "전사: "
            assert (tail.started_at_ms, tail.ended_at_ms) == (6000, 7000)
            await clients[1].finish()
            await wait_until(lambda: not runtime.sessions)
            assert calls == [(default_context,), (updated_context,), ("다른 세션",), ("",)]
            assert not errors
    finally:
        resume_first.set()
        for client in clients:
            await client.close()
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        listener.close()
        if runtime.asr_task is not None:
            runtime.asr_task.cancel()
            await asyncio.gather(runtime.asr_task, return_exceptions=True)


# 테스트 이름: test_modal_transcript_before_stop_and_final_drain
# 목적: Modal 기동 중에도 음성을 받고 준비 후 입력 순서대로 전사·종료하는 흐름을 보호한다.
# 준비: 모델 추론만 대체하고 실제 Modal adapter의 로컬 WebSocket 연결을 잠시 보류한다.
# 실행: 연결 준비 중 42개 PCM·heartbeat → 첫 전사 → pause/resume → 중간·다음 전사 → stop.
# 기대 결과: 준비 중 ACK·pong, PCM 순서, 빈 결과 뒤 화자·전사 번호와 마지막 전사 drain을 유지한다.
# 실패 조건: 준비 중 수신이 막히거나 PCM이 유실되거나 빈 결과로 종료·화자 정보 소실이 난다.
@pytest.mark.parametrize("empty_middle_turn", [False, True])
def test_modal_transcript_before_stop_and_final_drain(monkeypatch, capsys, empty_middle_turn):
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
            speaker = 0 if session.frames < 200 else (1 if session.frames < 500 else 2)
            active = (
                session.frames < 100
                or 200 <= session.frames < 300
                or 500 <= session.frames < 600
                or session.frames >= 800
            )
            return {
                "active": [[int(active and i == speaker) for i in range(8)]] * (len(pcm) // 320),
                "next_start": session.next_sample + len(pcm) // 2,
            }

        def _transcribe(self, jobs):
            return [
                ("", "empty_transcript")
                if empty_middle_turn and job.speaker == 1
                else (f"화자 {job.speaker}의 발언", None)
                for job in jobs
            ]

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
                remaining = [bytes([value]) * 32000 for value in range(6)]
                for sequence, chunk in enumerate(remaining, start=len(chunks)):
                    ws.send_json({"type": "audio.meta", "payload": {"sequence": sequence}})
                    ws.send_bytes(chunk)
                responses = [ws.receive_json() for _ in range(7 if empty_middle_turn else 8)]
                assert sum(event["type"] == "audio.ack" for event in responses) == 6
                transcripts = [
                    event for event in responses if event["type"] == "transcript.committed"
                ]
                assert len(transcripts) == (1 if empty_middle_turn else 2)
                if not empty_middle_turn:
                    middle = transcripts[0]
                    assert middle["payload"]["speakerId"] == 1
                    assert middle["payload"]["sequenceNumber"] == 1
                final = transcripts[-1]
                assert final["type"] == "transcript.committed"
                assert final["payload"]["speakerId"] == 2
                assert final["payload"]["content"] == "화자 2의 발언"
                assert final["payload"]["startedAtMs"] == 5000
                assert final["payload"]["endedAtMs"] == 6000
                assert final["payload"]["sequenceNumber"] == (1 if empty_middle_turn else 2)
                ws.send_json({"type": "session.stop", "requestId": "stop"})
                tail = ws.receive_json()
                assert tail["type"] == "transcript.committed"
                assert tail["payload"]["speakerId"] == 2
                assert tail["payload"]["content"] == "화자 2의 발언"
                assert tail["payload"]["startedAtMs"] == 8000
                assert tail["payload"]["endedAtMs"] == 9000
                assert tail["payload"]["sequenceNumber"] == final["payload"]["sequenceNumber"] + 1
                ended = ws.receive_json()
                assert ended["type"] == "session.ended"
                assert ended["payload"]["audioDurationMs"] == 9000
            assert app.state.recording_connections == 0
        assert not runtime.sessions
        assert b"".join(runtime.audio) == b"".join(chunks + remaining)
        if empty_middle_turn:
            failure = next(
                line
                for line in capsys.readouterr().out.splitlines()
                if "asr_segment_failed" in line
            )
            for field in (
                "meetingId=1",
                "recordingSessionId=1",
                "segmentId=1",
                "speakerId=1",
                "startedAtMs=2000",
                "endedAtMs=3000",
                "durationMs=1000",
                "reason=silence",
                "code=empty_transcript",
            ):
                assert field in failure
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
    session = runtime.open(SessionStart(type="session.start", meeting_id=1, recording_session_id=1))
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
async def test_modal_concurrent_sessions_keep_transcripts_separate():
    class Runtime(InferenceRuntime):
        def __init__(self):
            self.geometry = {
                "first_samples": 16000,
                "chunk_samples": 16000,
                "frame_samples": 160,
                "mel_step": 100,
            }
            self.metadata = {"test": True}
            self.audio = {}
            self.start_runtime()

        async def diarize(self, session, pcm, *, last):
            self.audio.setdefault(session.start.meeting_id, []).append(pcm)
            speaker = pcm[0] - 1
            return {
                "active": [
                    [int(frame % 300 < 100 and i == speaker) for i in range(8)]
                    for frame in range(session.frames, session.frames + len(pcm) // 320)
                ],
                "next_start": session.next_sample + len(pcm) // 2,
            }

        def _transcribe(self, jobs):
            return [(f"{job.session.context} 입력 {job.pcm[0]}", None) for job in jobs]

        async def _call_worker(self, request):
            return {"released": True}

    runtime = Runtime()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_inference_app(runtime), log_level="error", lifespan="off")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    transcripts = [asyncio.Queue(), asyncio.Queue()]
    errors = []
    clients = [
        adapter_module.ModalSTTClient(
            f"ws://127.0.0.1:{port}/transcribe", "test", "test", queue.put_nowait, errors.append
        )
        for queue in transcripts
    ]
    first_pcm, other_pcm = b"\x01" * 96000, b"\x02" * 96000

    async def wait_until(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)

    try:
        async with asyncio.timeout(10):
            await wait_until(lambda: server.started)
            await asyncio.gather(
                clients[0].start(meeting_id=1, recording_session_id=1, context="첫 회의"),
                clients[1].start(meeting_id=2, recording_session_id=2, context="다른 회의"),
            )
            sessions = {session.start.meeting_id: session for session in runtime.sessions.values()}
            assert set(sessions) == {1, 2}
            assert sessions[1].key != sessions[2].key
            await asyncio.gather(clients[0].send_audio(first_pcm), clients[1].send_audio(other_pcm))
            first, other = await asyncio.gather(*(queue.get() for queue in transcripts))
            assert (first.content, first.speaker_id, first.started_at_ms, first.ended_at_ms) == (
                "첫 회의 입력 1",
                0,
                0,
                1000,
            )
            assert (other.content, other.speaker_id, other.started_at_ms, other.ended_at_ms) == (
                "다른 회의 입력 2",
                1,
                0,
                1000,
            )
            await clients[0].finish()
            await wait_until(lambda: sessions[1].key not in runtime.sessions)
            assert runtime.sessions == {sessions[2].key: sessions[2]}
            await clients[1].send_audio(other_pcm)
            continued = await transcripts[1].get()
            assert (
                continued.content,
                continued.speaker_id,
                continued.started_at_ms,
                continued.ended_at_ms,
            ) == ("다른 회의 입력 2", 1, 3000, 4000)
            await clients[1].send_audio(other_pcm[:32000])
            await clients[1].finish()
            tail = await transcripts[1].get()
            assert (tail.content, tail.speaker_id, tail.started_at_ms, tail.ended_at_ms) == (
                "다른 회의 입력 2",
                1,
                6000,
                7000,
            )
            await wait_until(lambda: not runtime.sessions)
            assert b"".join(runtime.audio[1]) == first_pcm
            assert b"".join(runtime.audio[2]) == other_pcm * 2 + other_pcm[:32000]
            assert all(queue.empty() for queue in transcripts)
            assert runtime.asr_queue.empty()
            assert all(session.pending_jobs == 0 for session in sessions.values())
            assert not errors
    finally:
        for client in clients:
            await client.close()
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        listener.close()
        if runtime.asr_task is not None:
            runtime.asr_task.cancel()
            await asyncio.gather(runtime.asr_task, return_exceptions=True)


# 테스트 이름: test_exclusive_turns_do_not_merge_across_another_speaker
# 목적: 짧은 화자 전환을 가로질러 같은 PCM을 중복 전사하는 회귀를 막는다.
# 준비: A 0~1초, B 1~1.6초, A 1.6~3초의 배타적 활동과 실제 PipelineSession.
# 실행: 음성을 공급하고 EOF까지 처리한다.
# 기대 결과: ASR 입력은 A 0~1초, B 1~1.6초, A 1.6~3초이며 서로 겹치지 않는다.
# 실패 조건: A를 0~3초로 병합하거나 B의 입력이 다른 구간에 다시 포함된다.
async def test_exclusive_turns_do_not_merge_across_another_speaker():
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
                "active": [
                    [int(i == (1 if 100 <= frame < 160 else 0)) for i in range(8)]
                    for frame in range(session.frames, session.frames + len(pcm) // 320)
                ],
                "next_start": session.next_sample + len(pcm) // 2,
            }

        def _transcribe(self, jobs):
            self.calls.extend(job.pcm for job in jobs)
            return [(f"화자 {job.speaker}의 발언", None) for job in jobs]

        async def _call_worker(self, request):
            return {"released": True}

    runtime = Runtime()
    session = runtime.open(SessionStart(type="session.start", meeting_id=1, recording_session_id=1))
    pcm = b"\x01" * 32000 + b"\x02" * 19200 + b"\x03" * 44800
    try:
        async with asyncio.timeout(5):
            await session.feed(pcm)
            await session.finish()
            results = []
            while (event := await session.results.get()) is not None:
                results.append(event)
            committed = [event for event in results if event["type"] == "segment.committed"]
            assert [
                (event["segmentId"], event["speakerId"], event["startedAtMs"], event["endedAtMs"])
                for event in committed
            ] == [(0, 0, 0, 1000), (1, 1, 1000, 1600), (2, 0, 1600, 3000)]
            assert [event["content"] for event in committed] == [
                "화자 0의 발언",
                "화자 1의 발언",
                "화자 0의 발언",
            ]
            assert runtime.calls == [
                pcm[:32000],
                pcm[32000:51200],
                pcm[51200:],
            ]
            assert [event["type"] for event in results] == ["segment.committed"] * 3 + [
                "session.ended"
            ]
            assert results[-1]["succeededSegments"] == 3
            assert results[-1]["failedSegments"] == 0
            assert results[-1]["audioDurationMs"] == 3000
            assert session.pending_jobs == 0
    finally:
        await runtime.release(session)
        runtime.asr_task.cancel()
        await asyncio.gather(runtime.asr_task, return_exceptions=True)
    assert not runtime.sessions
