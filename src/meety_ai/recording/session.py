"""연결별 녹음 상태와 오디오 처리 순서를 관리한다."""

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum

import structlog

from meety_ai.core.schemas import MeetingId, RecordingSessionId
from meety_ai.recording.decoder import AudioDecodeError, AudioDecoder
from meety_ai.recording.schemas import (
    AudioMeta,
    ClientEvent,
    DecoderReady,
    DecoderReadyPayload,
    DecoderReset,
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

logger = structlog.stdlib.get_logger(__name__)

MAX_AUDIO_CHUNK_SIZE = 256 * 1024
DECODER_FEED_TIMEOUT = 10
DECODER_FINISH_TIMEOUT = 10
# 스트림 헤더 판별에 필요한 앞부분 길이. Chrome webm은 첫 청크가 1바이트라 여러 청크를 모은다.
STREAM_HEAD_SIZE = 8


def _starts_with_header(audio_format: str, head: bytes) -> bool:
    if audio_format == "webm_opus":
        return head.startswith(b"\x1a\x45\xdf\xa3")  # EBML magic
    return head[4:8] == b"ftyp"  # MP4 첫 box


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
        self._meeting_id: MeetingId | None = None
        self._recording_session_id: RecordingSessionId | None = None
        self._audio_format: str | None = None

        self._last_sequence: int | None = None
        self._pending_sequence: int | None = None
        self._pcm_bytes = 0

        self._on_pcm = on_pcm
        # 녹음 스트림마다 decoder.reset으로 교체하며, 첫 reset 전에는 없다.
        self._decoder: AudioDecoder | None = None
        self._stream_head = b""

    async def _consume_pcm(self, chunk: bytes) -> None:
        await self._on_pcm(chunk)
        self._pcm_bytes += len(chunk)

    async def _start(self, event: SessionStart) -> SessionReady:
        if self._state is not SessionState.NEW:
            raise SessionProtocolError("invalid_state", "이미 시작한 세션입니다.")
        self._meeting_id = event.meeting_id
        self._recording_session_id = event.recording_session_id
        self._state = SessionState.READY

        return SessionReady(
            type="session.ready",
            request_id=event.request_id,
            meeting_id=self._meeting_id,
            recording_session_id=self._recording_session_id,
            payload=SessionReadyPayload(
                status="READY",
                output_audio_format="pcm_s16le",
                output_sample_rate_hz=16000,
                output_channels=1,
            ),
        )

    async def _finish_decoder(self) -> None:
        """현재 스트림에 남은 음성을 STT로 비운다."""
        # 입력이 없는 디코더는 비울 PCM이 없고 finish()가 실패로 판정한다.
        if self._decoder is None or not self._stream_head:
            return
        try:
            async with asyncio.timeout(DECODER_FINISH_TIMEOUT):
                await self._decoder.finish()
        except TimeoutError as error:
            raise SessionProtocolError(
                "processing_timeout", "디코더 종료 시간이 초과됐습니다."
            ) from error

    async def _reset(self, event: DecoderReset) -> DecoderReady:
        if self._state is not SessionState.READY:
            raise SessionProtocolError(
                "invalid_state", "세션이 준비된 상태에서만 디코더를 교체할 수 있습니다."
            )

        if self._decoder is not None:
            # STT 세션은 유지되므로 이전 스트림 정리 실패로 녹음을 끊지 않는다.
            try:
                await self._finish_decoder()
            except (SessionProtocolError, AudioDecodeError) as error:
                logger.warning("decoder_finish_failed", error_type=type(error).__name__)
            await self._decoder.close()
            self._decoder = None

        decoder = AudioDecoder(self._consume_pcm)
        try:
            await decoder.start()
        except OSError as error:
            await decoder.close()
            raise AudioDecodeError("디코더를 시작하지 못했습니다.") from error
        self._decoder = decoder
        self._audio_format = event.payload.audio_format
        self._stream_head = b""

        return DecoderReady(
            type="decoder.ready",
            request_id=event.request_id,
            payload=DecoderReadyPayload(status="READY", input_audio_format=self._audio_format),
        )

    def _accept_meta(self, event: AudioMeta) -> None:
        if self._state is not SessionState.READY:
            raise SessionProtocolError("invalid_state", "세션이 준비되지 않았습니다.")
        if self._decoder is None:
            raise SessionProtocolError(
                "invalid_state", "decoder.reset 전에는 오디오를 받을 수 없습니다."
            )

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
            await self._finish_decoder()
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
        if isinstance(event, DecoderReset):
            return await self._reset(event)
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

        if len(self._stream_head) < STREAM_HEAD_SIZE:
            self._stream_head += chunk[: STREAM_HEAD_SIZE - len(self._stream_head)]
            if len(self._stream_head) == STREAM_HEAD_SIZE and not _starts_with_header(
                self._audio_format, self._stream_head
            ):
                raise SessionProtocolError(
                    "invalid_message", "스트림이 audioFormat의 헤더로 시작하지 않습니다."
                )

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
        if self._decoder is not None:
            await self._decoder.close()
        self._state = SessionState.ENDED
