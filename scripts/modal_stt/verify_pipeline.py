"""임시 Modal 앱과 로컬 Live 서버에서 35초·300초 PCM을 1배속으로 검증한다."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import socket
import time
import wave
from pathlib import Path

import modal
import uvicorn
from websockets.asyncio.client import connect

import meety_ai.stt.modal as adapter_module
from meety_ai.live import create_live_app
from meety_ai.modal_app.stt.app import TranscriptionWorker, app


async def replay(url: str, pcm: bytes, output: Path, *, pause: bool, resumed: bool) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    start_sequence = 100 if resumed else 0
    origin = None
    sent_stop = False
    samples = len(pcm) // 2
    segments, latencies = [], []
    summary = {"status": "failed", "inputSha256": hashlib.sha256(pcm).hexdigest()}
    started = time.perf_counter()
    sender = None
    try:
        async with connect(url, open_timeout=300, close_timeout=5) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "session.start",
                        "requestId": "verify-start",
                        "meetingId": 1,
                        "recordingSessionId": 1,
                        "participantCount": 0,
                        "participants": [],
                        "startSequence": start_sequence,
                        "transcriptSequenceStart": 7 if resumed else 0,
                        "title": "Meety 개발 회의",
                        "purpose": "개발 현황과 서비스 설계 논의",
                    }
                )
            )
            ready = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert ready["type"] == "session.ready", ready
            summary["readySeconds"] = time.perf_counter() - started
            origin = time.perf_counter()
            paused, resumed_event = asyncio.Event(), asyncio.Event()

            async def send_audio():
                nonlocal sent_stop
                for index, position in enumerate(range(0, len(pcm), 3200)):
                    chunk = pcm[position : position + 3200]
                    if pause and index == 120:
                        await ws.send(json.dumps({"type": "session.pause", "requestId": "pause"}))
                        await asyncio.wait_for(paused.wait(), 10)
                        await ws.send(json.dumps({"type": "session.resume", "requestId": "resume"}))
                        await asyncio.wait_for(resumed_event.wait(), 10)
                    await asyncio.sleep(
                        max(0, origin + (position + len(chunk)) / 32000 - time.perf_counter())
                    )
                    await ws.send(
                        json.dumps(
                            {"type": "audio.meta", "payload": {"sequence": start_sequence + index}}
                        )
                    )
                    await ws.send(chunk)
                sent_stop = True
                await ws.send(json.dumps({"type": "session.stop", "requestId": "stop"}))

            sender = asyncio.create_task(send_audio())
            with (output / "events.jsonl").open("w") as events:
                while True:
                    event = json.loads(await asyncio.wait_for(ws.recv(), 45))
                    received = time.perf_counter() - origin
                    event["clientReceivedSeconds"] = received
                    kind = event["type"]
                    events.write(json.dumps(event, ensure_ascii=False) + "\n")
                    events.flush()
                    assert kind != "error", event
                    if kind == "session.paused":
                        paused.set()
                    elif kind == "session.resumed":
                        resumed_event.set()
                    elif kind == "transcript.committed":
                        payload = event["payload"]
                        lower = payload["startedAtMs"] - start_sequence * 100
                        upper = payload["endedAtMs"] - start_sequence * 100
                        assert 0 <= lower < upper <= samples * 1000 // 16000
                        assert 500 <= upper - lower <= 25000
                        assert payload["sequenceNumber"] == len(segments) + (7 if resumed else 0)
                        assert payload["content"].strip()
                        if resumed:
                            assert payload["speakerId"] is None
                        else:
                            assert (
                                isinstance(payload["speakerId"], int)
                                and 0 <= payload["speakerId"] <= 7
                            )
                        latency = received - upper / 1000
                        latencies.append(latency)
                        segments.append(
                            {
                                **payload,
                                "endToReceivedSeconds": latency,
                                "beforeStop": not sent_stop,
                            }
                        )
                        print(f"{output.name}: 전사 {len(segments)}개 수신", flush=True)
                    elif kind == "session.ended":
                        assert event["payload"]["audioDurationMs"] == samples * 1000 // 16000
                        break
                await sender
                assert segments, "확정 전사가 없습니다."
                # 짧은 녹음은 Modal 기동 중 끝날 수 있으므로 장시간 검증에서만 확인한다.
                if samples >= 300 * 16000:
                    assert any(segment["beforeStop"] for segment in segments), (
                        "stop 이전 전사가 없습니다."
                    )
                async with asyncio.timeout(10):
                    async for _ in ws:
                        raise AssertionError("종료 뒤 추가 이벤트입니다.")
                assert ws.close_code == 1000
            summary.update(
                {
                    "status": "completed",
                    "receivedSegments": len(segments),
                    "beforeStopSegments": sum(segment["beforeStop"] for segment in segments),
                    "audioSamples": samples,
                    "audioSeconds": samples / 16000,
                    "endToReceivedP95Seconds": sorted(latencies)[
                        math.ceil(len(latencies) * 0.95) - 1
                    ],
                }
            )
    finally:
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        summary["elapsedSeconds"] = time.perf_counter() - started
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        (output / "transcript.json").write_text(json.dumps(segments, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


async def verify(remote_url: str, audio: Path, output: Path, durations: list[int]):
    os.environ["MEETY_MODAL_STT_URL"] = remote_url
    live = create_live_app()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(live, log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    original_connect = adapter_module.connect
    remote_events = (output / "modal-events.jsonl").open("w")

    class TracedSocket:
        def __init__(self, ws):
            self.ws = ws

        async def recv(self):
            message = await self.ws.recv()
            remote_events.write(
                json.dumps(
                    {"event": json.loads(message), "receivedMonotonic": time.perf_counter()},
                    ensure_ascii=False,
                )
                + "\n"
            )
            remote_events.flush()
            return message

        async def send(self, message):
            await self.ws.send(message)

        async def close(self):
            await self.ws.close()

    async def traced_connect(*args, **kwargs):
        return TracedSocket(await original_connect(*args, **kwargs))

    adapter_module.connect = traced_connect
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        for seconds in durations:
            with wave.open(str(audio), "rb") as source:
                assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (
                    16000,
                    1,
                    2,
                )
                pcm = source.readframes(seconds * 16000)
                assert len(pcm) == seconds * 32000
            await replay(
                f"ws://127.0.0.1:{port}/v1/live-meeting",
                pcm,
                output / f"{seconds}s",
                pause=seconds == 35,
                resumed=seconds == 300,
            )
            assert live.state.recording_connections == 0
    finally:
        adapter_module.connect = original_connect
        remote_events.close()
        server.should_exit = True
        await serving
        listener.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("docs/private/two-pipelines-20261013/input-exact-300s.wav"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, nargs="+", choices=(35, 300), default=[35, 300])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    workspace = modal.Workspace.from_context()
    token = workspace.proxy_tokens.create()
    try:
        os.environ["MODAL_PROXY_TOKEN_ID"] = token.token_id
        os.environ["MODAL_PROXY_TOKEN_SECRET"] = token.token_secret
        with modal.enable_output(), app.run():
            url = (
                TranscriptionWorker().web.get_web_url().replace("https://", "wss://")
                + "/transcribe"
            )
            asyncio.run(verify(url, args.input, args.output, args.seconds))
    finally:
        workspace.proxy_tokens.delete(token.token_id)
        print("임시 proxy token 삭제 완료", flush=True)


if __name__ == "__main__":
    main()
