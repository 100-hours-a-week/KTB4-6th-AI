import asyncio
import json

import pytest
from speechmatics.rt import AudioEncoding, Model, ServerMessageType

import meety_ai.recording.stt_client as stt_client_module


# 테스트 이름: test_finish_waits_for_final_transcript
# 목적: 종료 직전의 확정 전사가 누락되지 않도록 한다.
# 준비: start_session·send_audio·stop_session·close 호출을 기록하는 가짜 AsyncClient를
# monkeypatch하고, stop_session 중 마지막 확정 전사 콜백을 실행하게 한다.
# 실행: start() → send_audio() → finish() 순서로 호출한다.
# 기대 결과: 마지막 AddTranscript가 on_transcript에 전달된 뒤 finish()가 반환된다.
# 실패 조건: finish()가 마지막 전사 처리 전에 반환하거나 전사가 누락된다.
# 핵심 API: monkeypatch, 가짜 AsyncClient.on·start_session·send_audio·stop_session.
async def test_finish_waits_for_final_transcript(monkeypatch):
    instances = []
    events = []
    transcript = {"message": "AddTranscript", "metadata": {"transcript": "안녕하세요."}}

    class FakeAsyncClient:
        def __init__(self, *, api_key):
            self.api_key = api_key
            self.callbacks = {}
            self._recv_task = None
            self.start_kwargs = None
            self.audio = []
            self.closed = False
            instances.append(self)

        def on(self, event, callback):
            self.callbacks[event] = callback
            return callback

        async def start_session(self, **kwargs):
            self.start_kwargs = kwargs
            self._recv_task = asyncio.create_task(asyncio.Event().wait())

        async def send_audio(self, chunk):
            self.audio.append(chunk)

        async def stop_session(self):
            self.callbacks[ServerMessageType.ADD_TRANSCRIPT](transcript)
            self.callbacks[ServerMessageType.END_OF_TRANSCRIPT]({"message": "EndOfTranscript"})
            events.append("stop_session_returned")

        async def close(self):
            self.closed = True
            self._recv_task.cancel()
            await asyncio.gather(self._recv_task, return_exceptions=True)

    monkeypatch.setattr(stt_client_module, "AsyncClient", FakeAsyncClient)

    def on_transcript(message):
        events.append("transcript")
        assert message == transcript

    client = stt_client_module.SpeechmaticsClient("test-api-key", on_transcript)
    try:
        await client.start()
        await client.send_audio(b"pcm")
        await client.finish()
        events.append("finish_returned")
    finally:
        await client.close()

    fake_client = instances[0]
    config = fake_client.start_kwargs["transcription_config"]
    audio_format = fake_client.start_kwargs["audio_format"]

    assert fake_client.api_key == "test-api-key"
    assert ServerMessageType.ADD_TRANSCRIPT in fake_client.callbacks
    assert config.language == "ko"
    assert config.model is Model.ENHANCED
    assert config.diarization == "speaker"
    assert audio_format.encoding is AudioEncoding.PCM_S16LE
    assert audio_format.sample_rate == 16000
    assert fake_client.audio == [b"pcm"]
    assert events == ["transcript", "stop_session_returned", "finish_returned"]
    assert fake_client.closed


# 서버 오류와 메시지 없는 단절은 SDK에서 서로 다른 경로로 발생한다.
# 실제 SDK 수신 루프를 사용하고 외부 전송 경계만 대체해 두 경로와 정상 종료를 확인한다.
@pytest.mark.parametrize("ending", ["server_error", "disconnect", "normal_finish"])
async def test_sdk_termination_notification(monkeypatch, ending):
    errors = []
    notified = asyncio.Event()
    incoming = asyncio.Queue()

    def on_error(error):
        errors.append(error)
        notified.set()

    client = stt_client_module.SpeechmaticsClient("test-api-key", lambda message: None, on_error)
    transport = client._client._transport

    async def connect(headers=None):
        return

    async def send_message(data):
        message = json.loads(data)
        if message["message"] == "StartRecognition":
            incoming.put_nowait({"message": "RecognitionStarted", "id": "test-session"})
        elif message["message"] == "EndOfStream":
            incoming.put_nowait({"message": "EndOfTranscript"})

    async def receive_message():
        message = await incoming.get()
        if isinstance(message, Exception):
            raise message
        return message

    async def close():
        return

    monkeypatch.setattr(transport, "connect", connect)
    monkeypatch.setattr(transport, "send_message", send_message)
    monkeypatch.setattr(transport, "receive_message", receive_message)
    monkeypatch.setattr(transport, "close", close)

    try:
        async with asyncio.timeout(2):
            await client.start()
            if ending == "normal_finish":
                await client.finish()
            else:
                if ending == "server_error":
                    incoming.put_nowait(
                        {"message": "Error", "type": "job_error", "reason": "synthetic-private"}
                    )
                incoming.put_nowait(ConnectionError("synthetic-private"))
                await notified.wait()
                # 서버 오류 후 연결 종료가 이어져도 알림은 한 번이어야 한다.
                await client._client._recv_task
    finally:
        await client.close()

    assert client._client._recv_task.done()
    if ending == "normal_finish":
        assert errors == []
    else:
        assert len(errors) == 1
        assert isinstance(errors[0], stt_client_module.STTProviderError)
        assert "synthetic-private" not in str(errors[0])
