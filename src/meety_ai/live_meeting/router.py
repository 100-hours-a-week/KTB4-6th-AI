"""실시간 회의의 Backend 전용 WebSocket 수신·응답·연결 종료를 처리한다."""

import asyncio
import time

import sentry_sdk
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from meety_ai.chat.agent import generate_answer
from meety_ai.chat.schemas import ChatFailed, ChatRequest
from meety_ai.chat.session import ChatSession
from meety_ai.live_meeting.errors import MEETING_ERRORS, build_error_response
from meety_ai.live_meeting.messages import parse_meeting_message
from meety_ai.live_meeting.transport import MeetingReceiver, send_message
from meety_ai.recording.live import LiveRecording
from meety_ai.recording.schemas import (
    SessionEnded,
    SessionStop,
)
from meety_ai.recording.stt_client import SpeechmaticsClient
from meety_ai.recording.transcript import send_transcripts

logger = structlog.stdlib.get_logger(__name__)

live_meeting_router = APIRouter()
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
    receiver = MeetingReceiver(websocket)
    send_lock = asyncio.Lock()

    async def send_event(message) -> None:
        await send_message(websocket, message, send_lock)

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
        receiver.report_error,
        on_provider_error,
        provider_factory=provider_factory,
        transcript_sender=send_transcripts,
        stop_timeout=STT_STOP_TIMEOUT,
    )
    chat = ChatSession(
        websocket.app.state.chat_model,
        str(settings.backend_base_url) if settings.backend_base_url else None,
        send_event,
        receiver.report_error,
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
                data = await receiver.receive(timeout)

                if data["type"] == "websocket.disconnect":
                    close_reason = "client_disconnect"
                    break

                response = None
                if data.get("text") is not None:
                    event = parse_meeting_message(data["text"])
                    if isinstance(event, ChatFailed):
                        await send_event(event)
                        continue
                    if isinstance(event, ChatRequest):
                        await chat.submit(event, recording.chat_meeting_id)
                        continue
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

        except MEETING_ERRORS as error:
            close_reason = "error"
            response, close_code = build_error_response(error, request_id)
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
