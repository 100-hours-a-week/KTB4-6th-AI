"""연결별 녹음 상태와 오디오 처리 순서를 관리한다."""

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum

from meety_ai.recording.decoder import AudioDecoder
from meety_ai.recording.schemas import (
    AudioMeta,
    ClientEvent,
    Message,
    SessionEnded,
    SessionEndedPayload,
    SessionPause,
    SessionPaused,
    SessionPausedPayload,
    SessionReady,
    SessionReadyPayload,
    SessionResume,
    SessionResumed,
    SessionResumedPayload,
    SessionStart,
    SessionStop,
)

MAX_AUDIO_CHUNK_SIZE = 256 * 1024
DECODER_FEED_TIMEOUT = 10
DECODER_FINISH_TIMEOUT = 10


class SessionState(StrEnum):
    NEW = "NEW"
    READY = "READY"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


class SessionProtocolError(Exception):
    """현재 세션 상태에서 처리할 수 없는 입력을 나타낸다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RecordingSession:
    def __init__(self, on_pcm: Callable[[bytes], Awaitable[None]]):
        self._state = SessionState.NEW
        self._meeting_id: str | None = None
        self._recording_session_id: str | None = None
        self._input_audio_format: str | None = None

        self._last_sequence: int | None = None
        self._pending_sequence: int | None = None
        self._pcm_bytes = 0

        self._on_pcm = on_pcm
        self._decoder = AudioDecoder(self._consume_pcm)

    async def _consume_pcm(self, chunk: bytes) -> None:
        await self._on_pcm(chunk)
        self._pcm_bytes += len(chunk)

    async def _start(self, event: SessionStart) -> SessionReady:
        if self._state is not SessionState.NEW:
            raise SessionProtocolError("invalid_state", "이미 시작한 세션입니다.")
        self._meeting_id = event.meeting_id
        self._recording_session_id = event.recording_session_id
        self._input_audio_format = event.payload.audio_format

        await self._decoder.start()
        self._state = SessionState.READY

        return SessionReady(
            type="session.ready",
            request_id=event.request_id,
            meeting_id=self._meeting_id,
            recording_session_id=self._recording_session_id,
            payload=SessionReadyPayload(
                status="READY",
                input_audio_format=self._input_audio_format,
                output_audio_format="pcm_s16le",
                output_sample_rate_hz=16000,
                output_channels=1,
            ),
        )

    def _accept_meta(self, event: AudioMeta) -> None:
        if self._state is not SessionState.READY:
            raise SessionProtocolError("invalid_state", "세션이 준비되지 않았습니다.")

        expected_sequence = 0 if self._last_sequence is None else self._last_sequence + 1
        if event.payload.sequence != expected_sequence:
            raise SessionProtocolError("invalid_sequence", "sequence가 연속되지 않습니다.")

        self._pending_sequence = event.payload.sequence

    def _pause(self, event: SessionPause) -> SessionPaused:
        if self._state is not SessionState.READY:
            raise SessionProtocolError(
                "invalid_state", "세션이 준비된 상태에서만 정지할 수 있습니다."
            )

        self._state = SessionState.PAUSED

        return SessionPaused(
            type="session.paused",
            request_id=event.request_id,
            payload=SessionPausedPayload(status="PAUSED"),
        )

    def _resume(self, event: SessionResume) -> SessionResumed:
        if self._state is not SessionState.PAUSED:
            raise SessionProtocolError("invalid_state", "세션이 정지 상태가 아닙니다.")

        self._state = SessionState.READY

        return SessionResumed(
            type="session.resumed",
            request_id=event.request_id,
            payload=SessionResumedPayload(status="READY"),
        )

    async def _stop(self, event: SessionStop) -> SessionEnded:
        if self._state not in (SessionState.READY, SessionState.PAUSED):
            raise SessionProtocolError("invalid_state", "종료할 수 없는 세션 상태입니다.")

        try:
            async with asyncio.timeout(DECODER_FINISH_TIMEOUT):
                if self._last_sequence is not None:
                    await self._decoder.finish()
        except TimeoutError as error:
            raise SessionProtocolError(
                "processing_timeout", "디코더 종료 시간이 초과됐습니다."
            ) from error
        finally:
            await self.close()

        return SessionEnded(
            type="session.ended",
            request_id=event.request_id,
            meeting_id=self._meeting_id,
            recording_session_id=self._recording_session_id,
            payload=SessionEndedPayload(
                status="ENDED",
                last_sequence=self._last_sequence,
                audio_duration_ms=self._pcm_bytes * 1000 // 32000,
            ),
        )

    @property
    def expects_binary(self) -> bool:
        return self._pending_sequence is not None

    async def handle_event(self, event: ClientEvent) -> Message | None:
        if self.expects_binary:
            raise SessionProtocolError(
                "expected_binary", "audio.meta 다음에는 binary가 전송되어야 합니다."
            )
        if isinstance(event, SessionStart):
            return await self._start(event)
        if isinstance(event, AudioMeta):
            return self._accept_meta(event)
        if isinstance(event, SessionPause):
            return self._pause(event)
        if isinstance(event, SessionResume):
            return self._resume(event)
        if isinstance(event, SessionStop):
            return await self._stop(event)

    async def handle_binary(self, chunk: bytes) -> None:
        if self._state is not SessionState.READY:
            raise SessionProtocolError("invalid_state", "오디오를 받을 수 없는 상태입니다.")
        if not self.expects_binary:
            raise SessionProtocolError(
                "unexpected_binary", "binary 전송 이전에 audio.meta가 전송되어야 합니다."
            )
        if not chunk:
            raise SessionProtocolError("invalid_message", "빈 오디오 청크입니다.")
        if len(chunk) > MAX_AUDIO_CHUNK_SIZE:
            raise SessionProtocolError("message_too_large", "오디오 청크가 너무 큽니다.")

        try:
            async with asyncio.timeout(DECODER_FEED_TIMEOUT):
                await self._decoder.feed(chunk)
        except TimeoutError as error:
            raise SessionProtocolError(
                "processing_timeout", "디코더 입력 시간이 초과됐습니다."
            ) from error

        self._last_sequence = self._pending_sequence
        self._pending_sequence = None

    async def close(self) -> None:
        await self._decoder.close()
        self._state = SessionState.ENDED
