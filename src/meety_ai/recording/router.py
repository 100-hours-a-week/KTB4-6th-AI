"""Backend 전용 WebSocket의 수신·응답·연결 종료를 처리한다."""

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from meety_ai.recording.decoder import AudioDecodeError
from meety_ai.recording.schemas import (
    SessionEnded,
    SessionError,
    SessionErrorPayload,
    SessionStart,
    SessionStop,
    client_event_adapter,
)
from meety_ai.recording.session import RecordingSession, SessionProtocolError
from meety_ai.recording.stt_client import SpeechmaticsClient, STTProviderError
from meety_ai.recording.transcript import TranscriptMessage, TranscriptQueue, send_transcripts

recording_router = APIRouter()
MAX_TEXT_SIZE = 4 * 1024
MAX__RECORDING_CONNECTIONS = 20
BINARY_WAIT_TIMEOUT = 10
STT_STOP_TIMEOUT = 30


@recording_router.websocket("/v1/live-meeting")
async def start_recording_session(websocket: WebSocket) -> None:
    if websocket.app.state.recording_connections >= MAX__RECORDING_CONNECTIONS:
        await websocket.send_denial_response(
            JSONResponse(
                content={"error": "capacity_exceeded"},
                status_code=429,
            )
        )
        return

    api_key = websocket.app.state.settings.speechmatics_api_key
    if api_key is None:
        await websocket.send_denial_response(
            JSONResponse(content={"error": "service_unavailable"}, status_code=503)
        )
        return

    websocket.app.state.recording_connections += 1

    transcript_queue: TranscriptQueue = asyncio.Queue()
    sender_task: asyncio.Task[None] | None = None
    background_error: Exception | None = None

    def on_transcript(message: TranscriptMessage) -> None:
        transcript_queue.put_nowait(message)

    def on_error(error: Exception) -> None:
        nonlocal background_error
        if background_error is None:
            background_error = error

    def on_provider_error(error: Exception) -> None:
        on_error(STTProviderError("음성 전사 공급자 연결 또는 처리에 실패했습니다."))

    def raise_background_error() -> None:
        if background_error is not None:
            raise background_error

    async def forward_transcripts(event: SessionStart) -> None:
        try:
            await send_transcripts(
                websocket,
                transcript_queue,
                event.meeting_id,
                event.recording_session_id,
            )
        except Exception as error:
            on_error(error)

    stt_client = SpeechmaticsClient(
        api_key=api_key.get_secret_value(),
        on_transcript=on_transcript,
        on_error=on_provider_error,
    )

    session = RecordingSession(stt_client.send_audio)
    try:
        await websocket.accept()
        try:
            while True:
                request_id = None
                raise_background_error()

                timeout = BINARY_WAIT_TIMEOUT if session.expects_binary else None
                async with asyncio.timeout(timeout):
                    data = await websocket.receive()

                raise_background_error()

                if data["type"] == "websocket.disconnect":
                    break

                response = None
                if data.get("text") is not None:
                    if len(data["text"].encode("utf-8")) > MAX_TEXT_SIZE:
                        raise SessionProtocolError("message_too_large", "메시지가 너무 큽니다.")

                    event = client_event_adapter.validate_json(data["text"])
                    request_id = getattr(event, "request_id", None)
                    response = await session.handle_event(event)

                    if isinstance(event, SessionStart):
                        await stt_client.start()
                        sender_task = asyncio.create_task(forward_transcripts(event))

                    if isinstance(event, SessionStop):
                        async with asyncio.timeout(STT_STOP_TIMEOUT):
                            await stt_client.finish()
                            raise_background_error()
                            await transcript_queue.put(None)
                            await sender_task
                            raise_background_error()

                elif data.get("bytes") is not None:
                    await session.handle_binary(data["bytes"])

                if response is not None:
                    await websocket.send_json(response.model_dump(by_alias=True))

                if isinstance(response, SessionEnded):
                    await websocket.close(code=1000)
                    break

        except (
            ValidationError,
            SessionProtocolError,
            AudioDecodeError,
            TimeoutError,
            STTProviderError,
        ) as error:
            retryable = False
            if isinstance(error, STTProviderError):
                code, message = "provider_unavailable", str(error)
                retryable = True
                request_id = None
            elif isinstance(error, ValidationError):
                code, message = "invalid_message", "메시지 형식이 올바르지 않습니다."
            elif isinstance(error, SessionProtocolError):
                code, message = error.code, str(error)
            elif isinstance(error, AudioDecodeError):
                code, message = "audio_decode_failed", "오디오 처리에 실패했습니다."
            else:
                code, message = "processing_timeout", "처리 대기 시간이 초과됐습니다."

            close_code = 1008
            if code == "message_too_large":
                close_code = 1009
            elif code in ("audio_decode_failed", "processing_timeout", "provider_unavailable"):
                close_code = 1011

            response = SessionError(
                type="error",
                request_id=request_id,
                payload=SessionErrorPayload(code=code, message=message, retryable=retryable),
            )
            await websocket.send_json(response.model_dump(by_alias=True, exclude_none=True))
            await websocket.close(code=close_code)

    except WebSocketDisconnect:
        pass

    finally:
        try:
            if sender_task is not None:
                sender_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender_task
        finally:
            try:
                await stt_client.close()
            finally:
                try:
                    await session.close()
                finally:
                    websocket.app.state.recording_connections -= 1
