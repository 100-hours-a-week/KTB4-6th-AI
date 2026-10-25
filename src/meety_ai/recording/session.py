"""연결별 녹음 상태와 오디오 처리 순서를 관리한다."""

from collections.abc import Awaitable, Callable
from enum import StrEnum

from meety_ai.core.schemas import MeetingId, RecordingSessionId
from meety_ai.recording.schemas import (
    AudioAck,
    AudioMeta,
    AudioMetaPayload,
    ClientEvent,
    HeartbeatPing,
    HeartbeatPong,
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

        self._last_sequence: int | None = None
        self._next_sequence = 0
        self._pending_sequence: int | None = None
        self._pcm_bytes = 0

        self._on_pcm = on_pcm

    async def _start(self, event: SessionStart) -> SessionReady:
        if self._state is not SessionState.NEW:
            raise SessionProtocolError("invalid_state", "이미 시작한 세션입니다.")
        self._meeting_id = event.meeting_id
        self._recording_session_id = event.recording_session_id
        # shortcut: 재연결 중복 판정은 BE checkpoint를 신뢰하며, 영속 복구가 필요하면 재설계한다.
        self._next_sequence = event.start_sequence
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

    def _accept_meta(self, event: AudioMeta) -> None:
        if self._state is not SessionState.READY:
            raise SessionProtocolError("invalid_state", "세션이 준비되지 않았습니다.")

        if event.payload.sequence > self._next_sequence:
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
        if isinstance(event, HeartbeatPing):
            if self._state not in (SessionState.READY, SessionState.PAUSED):
                raise SessionProtocolError("invalid_state", "세션이 시작되지 않았습니다.")
            if event.recording_session_id != self._recording_session_id:
                raise SessionProtocolError("invalid_message", "녹음 세션 ID가 일치하지 않습니다.")
            return HeartbeatPong(
                type="heartbeat.pong",
                request_id=event.request_id,
                recording_session_id=self._recording_session_id,
            )
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

    async def handle_binary(self, chunk: bytes) -> AudioAck:
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

        if len(chunk) % 2:
            raise SessionProtocolError("invalid_message", "PCM 청크는 16비트 샘플 단위여야 합니다.")

        if self._pending_sequence == self._next_sequence:
            await self._on_pcm(chunk)
            self._pcm_bytes += len(chunk)
            self._last_sequence = self._pending_sequence
            self._next_sequence += 1
        self._pending_sequence = None
        return AudioAck(
            type="audio.ack",
            recording_session_id=self._recording_session_id,
            payload=AudioMetaPayload(sequence=self._next_sequence - 1),
        )

    async def close(self) -> None:
        self._state = SessionState.ENDED
