"""녹음 상태, STT와 전사 전달을 하나의 수명주기로 관리한다."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog
from pydantic import BaseModel
from structlog.contextvars import bind_contextvars

from meety_ai.recording.schemas import ClientEvent, DecoderReset, SessionStart, SessionStop
from meety_ai.recording.session import RecordingSession
from meety_ai.recording.stt_client import SpeechmaticsClient, STTProviderError
from meety_ai.recording.transcript import TranscriptQueue, send_transcripts

logger = structlog.stdlib.get_logger(__name__)


class LiveRecording:
    def __init__(
        self,
        api_key: str,
        send_event: Callable[[BaseModel], Awaitable[None]],
        on_error: Callable[[Exception], None],
        on_provider_error: Callable[[Exception], None],
        *,
        provider_factory: Callable[..., SpeechmaticsClient] = SpeechmaticsClient,
        transcript_sender: Callable[..., Awaitable[None]] = send_transcripts,
        stop_timeout: float = 30,
    ) -> None:
        self._queue: TranscriptQueue = asyncio.Queue()
        self._send_event = send_event
        self._on_error = on_error
        self._on_provider_error = on_provider_error
        self._transcript_sender = transcript_sender
        self._stop_timeout = stop_timeout
        self._error: Exception | None = None
        self._provider_error_reported = False
        self._sender_task: asyncio.Task[None] | None = None
        self._meeting_id: str | None = None
        self._provider = provider_factory(
            api_key=api_key,
            on_transcript=self._queue.put_nowait,
            on_error=self._provider_failed,
        )
        self._session = RecordingSession(self._provider.send_audio)

    @property
    def expects_binary(self) -> bool:
        return self._session.expects_binary

    @property
    def chat_meeting_id(self) -> str | None:
        """binary 대기 중이거나 녹음이 종료된 경우 질문 접수를 막는다."""
        return None if self.expects_binary else self._meeting_id

    def _failed(self, error: Exception) -> None:
        if self._error is None:
            self._error = error
            self._on_error(error)

    def _provider_failed(self, error: Exception) -> None:
        if self._provider_error_reported:
            return
        self._provider_error_reported = True
        self._on_provider_error(error)
        failure = STTProviderError("음성 전사 공급자 연결 또는 처리에 실패했습니다.")
        failure.__cause__ = error
        self._failed(failure)

    def _raise_error(self) -> None:
        if self._error is not None:
            raise self._error

    async def _forward_transcripts(self, event: SessionStart) -> None:
        try:
            await self._transcript_sender(
                self._queue, event.meeting_id, event.recording_session_id, self._send_event
            )
        except Exception as error:
            self._failed(error)

    async def handle_event(self, event: ClientEvent) -> BaseModel | None:
        """종료 응답은 디코더·STT·마지막 전사 처리가 끝난 뒤 반환한다."""
        self._raise_error()
        response = await self._session.handle_event(event)
        if isinstance(event, SessionStart):
            bind_contextvars(
                meeting_id=event.meeting_id, recording_session_id=event.recording_session_id
            )
            logger.info("session_started")
            await self._provider.start()
            self._meeting_id = event.meeting_id
            self._sender_task = asyncio.create_task(self._forward_transcripts(event))
        elif isinstance(event, DecoderReset):
            logger.info("decoder_reset", audio_format=event.payload.audio_format)
        elif isinstance(event, SessionStop):
            self._meeting_id = None
            async with asyncio.timeout(self._stop_timeout):
                await self._provider.finish()
                self._raise_error()
                await self._queue.put(None)
                await self._sender_task
                self._raise_error()
        return response

    async def handle_binary(self, chunk: bytes) -> None:
        self._raise_error()
        await self._session.handle_binary(chunk)

    async def close(self) -> None:
        """전사 작업, 공급자와 디코더를 오류 여부와 관계없이 정리한다."""
        self._meeting_id = None
        try:
            if self._sender_task is not None:
                self._sender_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._sender_task
        finally:
            try:
                await self._provider.close()
            finally:
                await self._session.close()
