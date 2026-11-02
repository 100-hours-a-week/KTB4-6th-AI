"""녹음 연결의 입력 처리·전사 작업·종료와 자원 정리를 관리한다."""

import asyncio
import time
from collections.abc import Awaitable, Callable

import sentry_sdk
import structlog
from anyio import CancelScope
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from structlog.contextvars import bind_contextvars

from meety_ai.core.schemas import Identifier, Message
from meety_ai.recording.schemas import (
    AudioMeta,
    SessionEnded,
    SessionError,
    SessionErrorPayload,
    SessionStart,
    SessionStop,
    client_event_adapter,
)
from meety_ai.recording.session import RecordingSession, SessionProtocolError
from meety_ai.recording.transcript import TranscriptQueue, send_message, send_transcripts
from meety_ai.stt.client import STTClient, STTProviderError
from meety_ai.stt.schemas import SpeakerProfile, STTSpeakerMapping, STTTranscript

logger = structlog.stdlib.get_logger(__name__)

# 참가자별 192차원 등록 벡터를 포함한 시작 메시지를 받는다.
MAX_TEXT_SIZE = 64 * 1024
BINARY_WAIT_TIMEOUT = 10
STT_STOP_TIMEOUT = 30
STT_SEND_TIMEOUT = 10
# Modal 기동 대기 240초 분량의 16kHz mono PCM을 보관한다.
MAX_PENDING_AUDIO_BYTES = 240 * 32000


class RecordingConnection:
    """연결 하나의 실행과 정리를 run() 호출 안에서 끝낸다."""

    def __init__(
        self,
        websocket: WebSocket,
        stt_client_factory: Callable[..., STTClient],
        generate_context: Callable[[SessionStart], Awaitable[str]],
        context_timeout_seconds: float,
    ) -> None:
        self._websocket = websocket
        self._generate_context = generate_context
        self._context_timeout = context_timeout_seconds
        self._context_task: asyncio.Task[None] | None = None
        self._stt_ready = asyncio.Event()
        self._transcript_queue: TranscriptQueue = asyncio.Queue()
        self._audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pending_audio_bytes = 0
        self._sender_task: asyncio.Task[None] | None = None
        self._audio_task: asyncio.Task[None] | None = None
        self._background_error: asyncio.Future[Exception] = (
            asyncio.get_running_loop().create_future()
        )
        self._stt_client = stt_client_factory(
            on_transcript=self._on_transcript,
            on_error=self._on_provider_error,
        )
        self._session = RecordingSession(self._enqueue_audio)
        self._request_id: Identifier | None = None
        self._binary_deadline: float | None = None
        self._close_reason: str | None = None
        self._audio_bytes = 0
        self._resumed = False
        self._started = time.perf_counter()

    async def run(self) -> None:
        """연결을 수락하고 입력 처리의 성공 여부와 관계없이 자원을 정리한다."""
        try:
            await self._websocket.accept()
            try:
                async with asyncio.TaskGroup() as tasks:
                    error_task = tasks.create_task(self._raise_background_error())
                    try:
                        await self._receive_loop()
                    finally:
                        error_task.cancel()
            except* (
                ValidationError,
                SessionProtocolError,
                TimeoutError,
                STTProviderError,
            ) as errors:
                # TaskGroup이 모은 오류 중 첫 원인을 Backend에 전달한다.
                await self._send_error_and_close(errors.exceptions[0])
        except* WebSocketDisconnect:
            # 오류 응답을 보내다 끊긴 경우는 오류 사유를 유지한다.
            self._close_reason = self._close_reason or "client_disconnect"
        finally:
            await self._cleanup()

    async def _receive_loop(self) -> None:
        while True:
            self._request_id = None
            data = await self._receive()
            if data["type"] == "websocket.disconnect":
                self._close_reason = "client_disconnect"
                return

            # shortcut: 오류·입력 경합 시 ACK가 앞설 수 있음, 차단하려면 수신 직후 오류 확인.
            response = None
            if data.get("text") is not None:
                response = await self._handle_text(data["text"])
            elif data.get("bytes") is not None:
                self._audio_bytes += len(data["bytes"])
                response = await self._session.handle_binary(data["bytes"])
                self._binary_deadline = None

            if response is not None:
                await send_message(self._websocket, response)

            if isinstance(response, SessionEnded):
                logger.info(
                    "session_ended",
                    last_sequence=response.payload.last_sequence,
                    audio_duration_ms=response.payload.audio_duration_ms,
                )
                await self._websocket.close(code=1000)
                self._close_reason = "normal"
                return

    async def _receive(self):
        async with asyncio.timeout_at(self._binary_deadline):
            return await self._websocket.receive()

    async def _handle_text(self, text: str) -> Message | None:
        if len(text.encode("utf-8")) > MAX_TEXT_SIZE:
            raise SessionProtocolError("message_too_large", "메시지가 너무 큽니다.")

        event = client_event_adapter.validate_json(text)
        self._request_id = getattr(event, "request_id", None)
        response = await self._session.handle_event(event)

        if isinstance(event, SessionStart):
            self._start_transcription(event)
        elif isinstance(event, AudioMeta):
            self._binary_deadline = asyncio.get_running_loop().time() + BINARY_WAIT_TIMEOUT
        elif isinstance(event, SessionStop):
            await self._finish_transcription()
        return response

    def _start_transcription(self, event: SessionStart) -> None:
        bind_contextvars(
            meeting_id=event.meeting_id,
            recording_session_id=event.recording_session_id,
        )
        logger.info("session_started")
        self._resumed = event.start_sequence > 0 or event.transcript_sequence_start > 0
        self._sender_task = asyncio.create_task(self._forward_transcripts(event))
        self._audio_task = asyncio.create_task(self._forward_audio(event))
        self._context_task = asyncio.create_task(self._apply_context(event))

    async def _finish_transcription(self) -> None:
        """남은 오디오와 마지막 전사를 모두 전달한 뒤 종료 응답을 허용한다."""
        # 입력 종료를 알린 Modal 세션에는 맥락 갱신을 보내지 않는다.
        if self._context_task is not None:
            self._context_task.cancel()
        async with asyncio.timeout(STT_STOP_TIMEOUT):
            await self._audio_queue.put(None)
            await self._audio_task
            await self._stt_client.finish()
            await self._transcript_queue.put(None)
            await self._sender_task

    async def _send_error_and_close(
        self, error: ValidationError | SessionProtocolError | TimeoutError | STTProviderError
    ) -> None:
        retryable = False
        if isinstance(error, STTProviderError):
            code, message = "provider_unavailable", str(error)
            retryable = True
            self._request_id = None
        elif isinstance(error, ValidationError):
            code, message = "invalid_message", "메시지 형식이 올바르지 않습니다."
        elif isinstance(error, SessionProtocolError):
            code, message = error.code, str(error)
            retryable = code == "audio_buffer_full"
        else:
            code, message = "processing_timeout", "처리 대기 시간이 초과됐습니다."

        close_code = 1008
        if code == "message_too_large":
            close_code = 1009
        elif code in ("processing_timeout", "provider_unavailable", "audio_buffer_full"):
            close_code = 1011

        self._close_reason = "error"
        log = logger.error if code == "provider_unavailable" else logger.warning
        # ValidationError 등의 메시지에는 입력 원문이 섞일 수 있어 예외 타입만 남긴다.
        log(
            "session_failed",
            code=code,
            close_code=close_code,
            request_id=self._request_id,
            error_type=type(error).__name__,
        )
        response = SessionError(
            type="error",
            request_id=self._request_id,
            payload=SessionErrorPayload(code=code, message=message, retryable=retryable),
        )
        await send_message(self._websocket, response)
        await self._websocket.close(code=close_code)

    async def _cleanup(self) -> None:
        logger.info(
            "session_closed",
            reason=self._close_reason or "error",
            duration_ms=round((time.perf_counter() - self._started) * 1000),
            audio_bytes=self._audio_bytes,
        )
        # ASGI 요청이 취소돼도 공급자와 백그라운드 작업은 끝까지 정리한다.
        with CancelScope(shield=True):
            try:
                tasks = [
                    task
                    for task in (self._sender_task, self._audio_task, self._context_task)
                    if task is not None
                ]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                while not self._audio_queue.empty():
                    self._audio_queue.get_nowait()
                self._pending_audio_bytes = 0
            finally:
                try:
                    await self._stt_client.close()
                finally:
                    await self._session.close()

    async def _enqueue_audio(self, chunk: bytes) -> None:
        if self._pending_audio_bytes + len(chunk) > MAX_PENDING_AUDIO_BYTES:
            raise SessionProtocolError("audio_buffer_full", "STT 오디오 대기열이 가득 찼습니다.")
        self._pending_audio_bytes += len(chunk)
        self._audio_queue.put_nowait(chunk)

    async def _forward_audio(self, event: SessionStart) -> None:
        try:
            context = "\n".join(
                f"{label}: {value}"
                for label, value in (
                    ("회의 제목", event.title),
                    ("목적", event.purpose),
                    ("비고", event.notes),
                )
                if value
            )
            await self._stt_client.start(
                meeting_id=event.meeting_id,
                recording_session_id=event.recording_session_id,
                context=context,
                profiles=[
                    SpeakerProfile(
                        team_member_id=participant.team_member_id,
                        speaker_embedding=participant.speaker_embedding,
                    )
                    for participant in event.participants
                    if participant.speaker_embedding is not None
                ],
            )
            logger.info("stt_ready", buffered_audio_bytes=self._pending_audio_bytes)
            self._stt_ready.set()
            while (chunk := await self._audio_queue.get()) is not None:
                try:
                    async with asyncio.timeout(STT_SEND_TIMEOUT):
                        await self._stt_client.send_audio(chunk)
                finally:
                    self._pending_audio_bytes -= len(chunk)
        except Exception as error:
            self._on_error(error)

    async def _apply_context(self, event: SessionStart) -> None:
        """생성한 맥락을 STT 준비 후 전달하고, 생성 실패 시 기본 맥락을 유지한다."""
        try:
            async with asyncio.timeout(self._context_timeout):
                context = await self._generate_context(event)
        except Exception as error:
            # 조회 거부·장애를 자료 없음으로 바꾸지 않고 생성 자체를 중단한다.
            logger.warning("stt_context_failed", error_type=type(error).__name__)
            return
        if not context:
            logger.info("stt_context_skipped")
            return
        await self._stt_ready.wait()
        try:
            async with asyncio.timeout(STT_SEND_TIMEOUT):
                await self._stt_client.update_context(context)
        except Exception as error:
            self._on_error(error)
            return
        logger.info("stt_context_applied", context_length=len(context))

    async def _forward_transcripts(self, event: SessionStart) -> None:
        try:
            await send_transcripts(
                self._websocket,
                self._transcript_queue,
                event.meeting_id,
                event.recording_session_id,
                start_sequence=event.start_sequence,
                transcript_sequence_start=event.transcript_sequence_start,
            )
        except Exception as error:
            self._on_error(error)

    def _on_transcript(self, message: STTTranscript | STTSpeakerMapping) -> None:
        if self._resumed:
            # shortcut: cache 복구 전에는 재연결 후 화자를 null로 전달하고 매핑은 보내지 않는다.
            if isinstance(message, STTSpeakerMapping):
                return
            message = message.model_copy(update={"speaker_id": None})
        self._transcript_queue.put_nowait(message)

    def _on_provider_error(self, error: Exception) -> None:
        if self._background_error.done():
            return
        sentry_sdk.metrics.count("meety.stt.errors", 1, attributes={"service": "live"})
        sentry_sdk.capture_exception(error)
        # 백엔드에는 일반화한 메시지만 보내므로 원인 예외는 로그에만 남긴다.
        logger.error("stt_provider_failed", exc_info=error)
        self._on_error(STTProviderError("음성 전사 공급자 연결 또는 처리에 실패했습니다."))

    def _on_error(self, error: Exception) -> None:
        if not self._background_error.done():
            self._background_error.set_result(error)

    async def _raise_background_error(self) -> None:
        """백그라운드 실패를 기다렸다가 연결 실행 작업에 예외를 전달한다."""
        error = await self._background_error
        raise error
