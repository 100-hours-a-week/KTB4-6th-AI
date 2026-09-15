"""Backend와 주고받는 제어 메시지의 데이터 규약을 정의한다.

구현할 내용:
- session.start, audio.meta, session.pause/resume/stop의 Pydantic 모델을 정의한다.
- session.ready/paused/resumed/ended와 error 응답 모델을 정의한다.
- 식별자, 입력 형식, sequence, 시간 정보의 타입과 허용 범위를 검증한다.
- WebM+Opus와 MP4+AAC 입력에 필요한 필드의 의미를 명확히 한다.

Wiki의 기존 PCM 입력 규약과 압축 입력의 차이는 별도 계약 문서에 정리한다.
현재 상태에서 허용되는 이벤트인지, 이전 sequence와 이어지는지는 session.py에서 판단한다.
"""

from typing_extensions import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, TypeAdapter, ConfigDict
from pydantic.alias_generators import to_camel

class Message(BaseModel):
  model_config = ConfigDict(
    alias_generator=to_camel,
    validate_by_name=True,
    extra='forbid',
    strict=True
  )

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    ),
]

class SessionStartPayload(Message):
  audio_format: Literal["webm_opus", "mp4_aac"]


class SessionStart(Message):
  type: Literal['session.start']
  request_id: Identifier
  meeting_id: Identifier
  recording_session_id: Identifier
  payload: SessionStartPayload


class SessionReadyPayload(Message):
  status: Literal['READY']
  input_audio_format: Literal["webm_opus", "mp4_aac"]
  output_audio_format: Literal['pcm_s16le']
  output_sample_rate_hz: Literal[16000]
  output_channels: Literal[1]


class SessionReady(Message):
  type: Literal['session.ready']
  request_id: Identifier
  meeting_id: Identifier
  recording_session_id: Identifier
  payload: SessionReadyPayload


class AudioMetaPayload(Message):
  sequence: Annotated[int, Field(ge=0)]


class AudioMeta(Message):
  type: Literal['audio.meta']
  payload: AudioMetaPayload


class SessionPause(Message):
  type: Literal['session.pause']
  request_id: Identifier


class SessionPausedPayload(Message):
  status: Literal['PAUSED']


class SessionPaused(Message):
  type: Literal['session.paused']
  request_id: Identifier
  payload: SessionPausedPayload


class SessionResume(Message):
  type: Literal['session.resume']
  request_id: Identifier


class SessionResumedPayload(Message):
  status: Literal['READY']


class SessionResumed(Message):
  type: Literal['session.resumed']
  request_id: Identifier
  payload: SessionResumedPayload


class SessionStop(Message):
  type: Literal['session.stop']
  request_id: Identifier


class SessionEndedPayload(Message):
  status: Literal['ENDED']
  last_sequence: Annotated[int, Field(ge=0)] | None
  audio_duration_ms: Annotated[int, Field(ge=0)]


class SessionEnded(Message):
  type: Literal['session.ended']
  request_id: Identifier
  meeting_id: Identifier
  recording_session_id: Identifier
  payload: SessionEndedPayload


class SessionErrorPayload(Message):
  code: Literal['invalid_message', 'invalid_state', 'invalid_sequence',
                'unexpected_binary', 'expected_binary', 'message_too_large',
                'audio_decode_failed', 'processing_timeout']
  message: str
  retryable: Literal[True, False]


class SessionError(Message):
  type: Literal['error']
  request_id: Identifier | None
  payload: SessionErrorPayload


ClientEvent = Annotated[
      SessionStart
      | AudioMeta
      | SessionPause
      | SessionResume
      | SessionStop,
      Field(discriminator="type"),
  ]

client_event_adapter = TypeAdapter(ClientEvent)