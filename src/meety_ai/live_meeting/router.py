"""실시간 회의의 Backend 전용 WebSocket 수신·응답·연결 종료를 처리한다."""

import asyncio
import json
import time

import sentry_sdk
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from meety_ai.chat.agent import generate_answer
from meety_ai.chat.session import ChatSession
from meety_ai.live_meeting.transport import receive_message, send_message
from meety_ai.recording.decoder import AudioDecodeError
from meety_ai.recording.live import LiveRecording
from meety_ai.recording.schemas import (
    SessionEnded,
    SessionError,
    SessionErrorPayload,
    SessionStop,
    client_event_adapter,
)
from meety_ai.recording.session import SessionProtocolError
from meety_ai.recording.stt_client import SpeechmaticsClient, STTProviderError
from meety_ai.recording.transcript import send_transcripts

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

    settings = websocket.app.state.settings
    failure: asyncio.Future[Exception] = asyncio.get_running_loop().create_future()
    send_lock = asyncio.Lock()

    async def send_event(message) -> None:
        await send_message(websocket, message, send_lock)

    def on_error(error: Exception) -> None:
        if not failure.done():
            failure.set_result(error)

    def on_provider_error(error: Exception) -> None:
        sentry_sdk.metrics.count("meety.stt.errors", 1, attributes={"service": "live"})
        sentry_sdk.capture_exception(error)
        # 백엔드에는 일반화한 메시지만 보내므로 원인 예외는 로그에만 남긴다.
        logger.error("stt_provider_failed", exc_info=error)

    # 실제 공급자와 부하테스트용 Adapter가 같은 Interface를 사용한다.
    provider_factory = getattr(websocket.app.state, "stt_client_factory", SpeechmaticsClient)
    recording = LiveRecording(
        settings.speechmatics_api_key.get_secret_value(),
        send_event,
        on_error,
        on_provider_error,
        provider_factory=provider_factory,
        transcript_sender=send_transcripts,
        stop_timeout=STT_STOP_TIMEOUT,
    )
    chat = ChatSession(
        websocket.app.state.chat_model,
        str(settings.backend_base_url) if settings.backend_base_url else None,
        send_event,
        on_error,
        timeout_seconds=settings.chat_timeout_seconds,
        answer_generator=generate_answer,
    )
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
                timeout = BINARY_WAIT_TIMEOUT if recording.expects_binary else None
                data = await receive_message(websocket, failure, timeout)

                if data["type"] == "websocket.disconnect":
                    close_reason = "client_disconnect"
                    break

                response = None
                if data.get("text") is not None:
                    text_size = len(data["text"].encode("utf-8"))
                    if text_size > 2 * 1024 * 1024:
                        raise SessionProtocolError("message_too_large", "메시지가 너무 큽니다.")
                    try:
                        raw = json.loads(data["text"])
                    except ValueError as error:
                        raise SessionProtocolError(
                            "invalid_message", "메시지 형식이 올바르지 않습니다."
                        ) from error
                    if isinstance(raw, dict) and raw.get("type") == "chat.request":
                        await chat.submit(raw, text_size, recording.chat_meeting_id)
                        continue
                    if text_size > MAX_TEXT_SIZE:
                        raise SessionProtocolError("message_too_large", "메시지가 너무 큽니다.")

                    event = client_event_adapter.validate_json(data["text"])
                    request_id = getattr(event, "request_id", None)
                    if isinstance(event, SessionStop):
                        await chat.close()
                    response = await recording.handle_event(event)

                elif data.get("bytes") is not None:
                    audio_bytes += len(data["bytes"])
                    await recording.handle_binary(data["bytes"])

                if response is not None:
                    await send_event(response)

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
            await chat.close()
            await send_event(response)
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
            await chat.close()
        finally:
            try:
                await recording.close()
            finally:
                websocket.app.state.recording_connections -= 1
