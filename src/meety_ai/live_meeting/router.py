"""실시간 회의의 Backend 전용 WebSocket 수신·응답·연결 종료를 처리한다."""

import asyncio
import contextlib
import time

import sentry_sdk
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from structlog.contextvars import bind_contextvars

from meety_ai.recording.decoder import AudioDecodeError
from meety_ai.recording.schemas import (
    DecoderReset,
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

logger = structlog.stdlib.get_logger(__name__)

live_meeting_router = APIRouter()
MAX_TEXT_SIZE = 4 * 1024
BINARY_WAIT_TIMEOUT = 10
STT_STOP_TIMEOUT = 30


@live_meeting_router.websocket("/v1/live-meeting")
async def start_recording_session(websocket: WebSocket) -> None:
    if (
        websocket.app.state.recording_connections
        >= websocket.app.state.settings.max_recording_connections
    ):
        logger.warning(
            "recording_rejected",
            reason="capacity_exceeded",
            active_connections=websocket.app.state.recording_connections,
        )
        await websocket.send_denial_response(
            JSONResponse(
                content={"error": "capacity_exceeded"},
                status_code=429,
            )
        )
        return

    api_key = websocket.app.state.settings.speechmatics_api_key
    transcript_queue: TranscriptQueue = asyncio.Queue()
    sender_task: asyncio.Task[None] | None = None
    background_error: Exception | None = None
    provider_error_reported = False

    def on_transcript(message: TranscriptMessage) -> None:
        transcript_queue.put_nowait(message)

    def on_error(error: Exception) -> None:
        nonlocal background_error
        if background_error is None:
            background_error = error

    def on_provider_error(error: Exception) -> None:
        nonlocal provider_error_reported
        if provider_error_reported:
            return
        provider_error_reported = True
        sentry_sdk.metrics.count("meety.stt.errors", 1, attributes={"service": "live"})
        sentry_sdk.capture_exception(error)
        # 백엔드에는 일반화한 메시지만 보내므로 원인 예외는 로그에만 남긴다.
        logger.error("stt_provider_failed", exc_info=error)
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

    # 부하테스트 진입점은 app.state에 가짜 클라이언트를 넣는다.
    stt_client_factory = getattr(websocket.app.state, "stt_client_factory", SpeechmaticsClient)
    stt_client = stt_client_factory(
        api_key=api_key.get_secret_value(),
        on_transcript=on_transcript,
        on_error=on_provider_error,
    )

    session = RecordingSession(stt_client.send_audio)
    started = time.perf_counter()
    # 사유를 확인하지 못한 채 끝나면(예상하지 못한 예외 등) 오류로 기록한다.
    close_reason: str | None = None
    audio_bytes = 0
    websocket.app.state.recording_connections += 1
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
                    close_reason = "client_disconnect"
                    break

                response = None
                if data.get("text") is not None:
                    if len(data["text"].encode("utf-8")) > MAX_TEXT_SIZE:
                        raise SessionProtocolError("message_too_large", "메시지가 너무 큽니다.")

                    event = client_event_adapter.validate_json(data["text"])
                    request_id = getattr(event, "request_id", None)
                    response = await session.handle_event(event)

                    if isinstance(event, SessionStart):
                        # 이후 이 연결에서 남기는 로그에 회의 식별자를 함께 기록한다.
                        bind_contextvars(
                            meeting_id=event.meeting_id,
                            recording_session_id=event.recording_session_id,
                        )
                        logger.info("session_started")
                        await stt_client.start()
                        # @debt
                        sender_task = asyncio.create_task(forward_transcripts(event))

                    if isinstance(event, DecoderReset):
                        logger.info("decoder_reset", audio_format=event.payload.audio_format)

                    if isinstance(event, SessionStop):
                        async with asyncio.timeout(STT_STOP_TIMEOUT):
                            await stt_client.finish()
                            raise_background_error()
                            await transcript_queue.put(None)
                            await sender_task
                            raise_background_error()

                elif data.get("bytes") is not None:
                    audio_bytes += len(data["bytes"])
                    await session.handle_binary(data["bytes"])

                if response is not None:
                    await websocket.send_json(response.model_dump(by_alias=True))

                if isinstance(response, SessionEnded):
                    logger.info(
                        "session_ended",
                        last_sequence=response.payload.last_sequence,
                        audio_duration_ms=response.payload.audio_duration_ms,
                    )
                    await websocket.close(code=1000)
                    close_reason = "normal"
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

            close_reason = "error"
            log = logger.error if code == "provider_unavailable" else logger.warning
            # ValidationError 등의 메시지에는 입력 원문이 섞일 수 있어 예외 타입만 남긴다.
            log(
                "session_failed",
                code=code,
                close_code=close_code,
                request_id=request_id,
                error_type=type(error).__name__,
            )

            response = SessionError(
                type="error",
                request_id=request_id,
                payload=SessionErrorPayload(code=code, message=message, retryable=retryable),
            )
            await websocket.send_json(response.model_dump(by_alias=True, exclude_none=True))
            await websocket.close(code=close_code)

    except WebSocketDisconnect:
        # 오류 응답을 보내다 끊긴 경우는 오류 사유를 유지한다.
        close_reason = close_reason or "client_disconnect"

    finally:
        logger.info(
            "session_closed",
            reason=close_reason or "error",
            duration_ms=round((time.perf_counter() - started) * 1000),
            audio_bytes=audio_bytes,
        )
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
